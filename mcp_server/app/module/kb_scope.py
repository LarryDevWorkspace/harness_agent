"""前端指定的「自訂檢索範圍」：暫存 + 跟使用者權限取交集。

## 為什麼範圍要走這條側路，而不是跟著問題一起傳

openclaw 的 /v1/chat/completions 對 body 欄位與 header 都是固定白名單
（body 只認 model/user/stream/temperature… 沒有 metadata；header 只認
x-openclaw-*），沒有任何 per-request 的任意資料通道。所以前端勾的範圍沒辦法
跟著那個請求流進 plugin，只能繞過 openclaw：

    前端勾選範圍
        │
        ├─► kiki-chat backend ──PUT /kb/scope──► 這裡（以 session_key 為 key 暫存）
        │        │
        │        └─ 轉發問題 ─► openclaw ─► kb-search plugin ─POST /kb/search─► 這裡
        │                                    （帶 X-Openclaw-Session-Key）  └─ 取出範圍

用 session_key 當 key，是因為那是前端、backend、plugin 三邊唯一都拿得到、而且
一定對得起來的識別：backend 自己算出 "agent:<工號>:<key>" 送給 gateway，
plugin 那端從 toolContext.sessionKey 拿到同一個字串。

## ⚠️ 範圍只能縮小，不能放大

範圍是「在使用者權限之上再加限制」，永遠不能讓人看到原本看不到的東西。
RAG 服務的 filter 是「組間 OR、組內 AND」的扁平陣列，能不能安全交集要看欄位：

- docType     ── DocType == value 嚴格等值，沒有逃生門 → 可以安全交集
- metadataIds ── MetadataID IN [...]，沒有逃生門       → 可以安全交集
- allowDirect ── 只在 =="Y" 時多加一個條件            → 加上去只會更嚴格

- compCode / depCode / empID ── ⛔ 不支援。它們的條件是
  「(IN 值) OR =='ALL' OR ==''」，帶逃生門。反例：某權限組 compCode=""（只看得到
  全公司文件），為了限縮而填入 "AVC" 會變成 IN[AVC] OR ALL OR ''——比原本更寬，
  AVC 的文件本來看不到現在看得到了。這是實打實的權限放大，所以直接拒收，
  不做「盡力而為」的處理（默默忽略比擋下來更糟：前端會以為有生效）。
  要正確限縮這三個欄位，得等 RAG API 支援巢狀 filter（(權限) AND (範圍)）。
"""
from __future__ import annotations

import os
import threading
import time

from app.module.logs import get as get_logger

log = get_logger("kb_scope")

# 範圍是「這串對話目前選的」，backend 每次轉發問題前都會重寫一次，所以 TTL 只是
# 用來回收沒人再用的舊 session，不是功能的一部分。
KB_SCOPE_TTL = int(os.environ.get("KB_SCOPE_TTL", "86400"))

#: 只有這三個欄位可以安全地跟權限取交集，理由見模組開頭。
ALLOWED_SCOPE_FIELDS = ("docType", "metadataIds", "allowDirect")

_store: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


class ScopeError(ValueError):
    """前端送來的範圍不合法（欄位不支援、型別不對）。"""


def _split(value) -> list[str]:
    """RAG 的多值欄位是逗號分隔字串，統一拆成 list 再處理。"""
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def validate(scope: dict | None) -> dict | None:
    """檢查並正規化前端送來的範圍。不合法直接丟 ScopeError，不要默默吞掉。"""
    if scope is None:
        return None
    if not isinstance(scope, dict):
        raise ScopeError("scope 必須是物件")
    unknown = sorted(set(scope) - set(ALLOWED_SCOPE_FIELDS))
    if unknown:
        raise ScopeError(
            f"不支援的範圍欄位 {unknown}；只接受 {list(ALLOWED_SCOPE_FIELDS)}。"
            "compCode/depCode/empID 因為帶有 'ALL'/'' 逃生門，拿來限縮會造成權限放大，"
            "所以一律拒收（見 app/module/kb_scope.py 開頭）。"
        )
    normalized = {}
    for field in ALLOWED_SCOPE_FIELDS:
        raw = scope.get(field)
        if raw is None:
            continue
        if not isinstance(raw, str):
            raise ScopeError(f"範圍欄位 {field} 必須是字串（多值用逗號分隔）")
        value = raw.strip()
        if value:
            normalized[field] = value
    return normalized or None


def put(session_key: str, scope: dict | None) -> None:
    """設定（或用 None 清除）這串對話的範圍。scope 應該先過 validate()。"""
    now = time.time()
    with _lock:
        # 順手回收過期的，不另外開一條清理執行緒。
        for key in [k for k, (exp, _) in _store.items() if exp <= now]:
            _store.pop(key, None)
        if scope:
            _store[session_key] = (now + KB_SCOPE_TTL, scope)
        else:
            _store.pop(session_key, None)


def get(session_key: str | None) -> dict | None:
    if not session_key:
        return None
    with _lock:
        entry = _store.get(session_key)
        if not entry:
            return None
        expires_at, scope = entry
        if expires_at <= time.time():
            _store.pop(session_key, None)
            return None
        return scope


def narrow(filter_list: list, scope: dict | None) -> list | None:
    """把權限 filter 跟範圍取交集。

    回傳 None 代表「交集為空」——呼叫端必須直接回查無資料，**絕對不可以**改送空
    陣列給 RAG：RAG 的 build_llama_filters 對空陣列回傳 None，等於完全不過濾，
    會把整個知識庫開出去。

    權限本身是空陣列時（RAG_FILTER 沒設、DB 查詢失敗等 fail-open 路徑），原本是
    不過濾；套上範圍之後會多出 compCode/depCode/empID 的「只看 ALL/空」條件，
    比原本更嚴格。方向是安全的，但那本來就是降級模式，值得知道。
    """
    if not scope:
        return filter_list

    doc_type = scope.get("docType", "").strip()
    scope_ids = _split(scope.get("metadataIds"))
    allow_direct = scope.get("allowDirect", "").strip().upper() == "Y"

    groups = filter_list or [{}]
    narrowed: list[dict] = []
    for group in groups:
        merged = dict(group)

        if doc_type:
            current = (group.get("docType") or "").strip()
            if current and current != doc_type:
                continue  # 這組權限跟範圍互斥，整組丟掉（不是放寬成其中一邊）
            merged["docType"] = doc_type

        if scope_ids:
            current_ids = _split(group.get("metadataIds"))
            # 這組原本沒限定文件 → 範圍就是交集；有限定 → 取交集，空了就丟掉這組。
            ids = [i for i in scope_ids if i in current_ids] if current_ids else list(scope_ids)
            if not ids:
                continue
            merged["metadataIds"] = ",".join(ids)

        if allow_direct:
            merged["allowDirect"] = "Y"

        narrowed.append(merged)

    if not narrowed:
        log.info("範圍與權限交集為空，這次不打 RAG（scope=%s）", scope)
        return None
    return narrowed
