"""Tool：呼叫公司 RAG 服務，檢索知識庫中相關的文件片段。

對接的是內部 RAG Local Service 的 POST /api/v1/query，
它會做「檢索 → 重排序」並回傳一組節點（text / score / metadata）。
這個 tool 只負責取回相關片段；答案由 backend 那邊的模型根據片段生成。

emp_id 從 HTTP header（X-Emp-Id）取得，不是 tool 的參數——backend 呼叫這個
tool 時會依「目前登入者」把 emp_id 放進 header；這個 header 不會出現在
LLM 看到的 tool schema 裡（見 Context.headers 的用法），確保「用誰的權限
查」這件事只能由後端依登入者身分決定，模型看不到、也改不動。

── local / azure 來源切換（KB_BACKEND）──
KB_BACKEND=local（預設）：走上面說的 RAG Local Service，回傳原始節點清單，
由 _format() 組成帶 [FileID] 引用指示的文字。
KB_BACKEND=azure：改打公司 KBApi/AskQuestionStream（SSE），那支本身就是
「檢索＋生成答案」一次做完，回傳的已經是完整答案，不是原始節點——所以這條
路徑不會再套用 _format() 的引用範本，直接把它吐出來的文字當作 content。

每次檢索都會另外寫一筆結構化紀錄到 KB_LOG_DIR（見 app/module/kb_log.py）：
帶進來的 filter 參數（含 knowledge_search_plain 那條「模型自己填的」
filter_criteria 原樣）＋完整結果，用來事後追查權限範圍對不對、撈到什麼。
"""
import asyncio
import json
import os
import re
import time

import httpx
from mcp.server.mcpserver.context import Context
from mcp_types import CallToolResult, TextContent

from app.module import db_client as db
from app.module import kb_log
from app.module import kb_scope
from app.module.logs import get as get_logger

log = get_logger("knowledge_search")

KB_BACKEND = os.environ.get("KB_BACKEND", "local").strip().lower()

RAG_BASE_URL = os.environ.get("RAG_BASE_URL", "http://172.16.174.116:8000")
RAG_TOPK = int(os.environ.get("RAG_TOPK", "5"))
RAG_TIMEOUT = float(os.environ.get("RAG_TIMEOUT", "30"))
RAG_VERIFY_SSL = os.environ.get("RAG_VERIFY_SSL", "true").lower() != "false"

AZURE_KB_URL = os.environ.get(
    "AZURE_KB_URL",
    "https://deveip3.avc.co/DesktopModules/AiLibrary/API/KBApi/AskQuestionStream",
)
AZURE_KB_TIMEOUT = float(os.environ.get("AZURE_KB_TIMEOUT", "30"))
AZURE_KB_VERIFY_SSL = os.environ.get("AZURE_KB_VERIFY_SSL", "true").lower() != "false"

# ── 擴大搜尋範圍 ──
# 料號查詢常常對應到數十甚至數百筆資料，預設 topk 撈不完；
# 命中「料號格式」或下列關鍵字時，改用較大的 RAG_TOPK_EXPANDED。
RAG_TOPK_EXPANDED = int(os.environ.get("RAG_TOPK_EXPANDED", "50"))
# 保險：若第一次查詢剛好撈滿 topk 筆（代表可能還有更多、被截斷），
# 且目前用的 topk 還沒到這個上限，就自動用 RAG_TOPK_MAX 再查一次。
RAG_TOPK_MAX = int(os.environ.get("RAG_TOPK_MAX", "100"))
RAG_EXPAND_KEYWORDS = {kw.strip() for kw in
                       os.environ.get("RAG_EXPAND_KEYWORDS", "CAR,LL,MRB,報廢").split(",") if kw.strip()}
# 料號格式：英數混合、6 碼以上，可能帶 -數字 後綴（例如 DFPK0456B2GY0P3-1）
_PART_NUMBER_RE = re.compile(
    r"\b(?=[A-Za-z0-9-]{6,}\b)(?=[A-Za-z0-9-]*[A-Za-z])(?=[A-Za-z0-9-]*[0-9])[A-Za-z0-9]+(?:-[A-Za-z0-9]+)?\b"
)


def _needs_expanded_search(query: str) -> bool:
    if _PART_NUMBER_RE.search(query):
        return True
    return any(kw.lower() in query.lower() for kw in RAG_EXPAND_KEYWORDS)


# 權限過濾。RAG 服務的 filter 欄位為必填。
# 優先順序：呼叫端帶的 emp_id（依 emp_id 查 db_api 的 user_permissions 表）
# → 沒有 emp_id 或查詢失敗 → 退回環境變數 RAG_FILTER（沒設就是空陣列，不加限制）。
# Fail-open：DB 查詢失敗絕不能讓檢索整個掛掉，退回環境變數繼續查。
def _env_filter() -> list:
    raw = os.environ.get("RAG_FILTER")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def _resolve_filter(emp_id: str | None) -> tuple[list, str]:
    """回傳（實際要用的 filter, 這份 filter 從哪來）。

    第二個值只給 log 用（見 kb_log）——事後看紀錄時要能一眼分辨「這次查詢
    真的套到使用者權限」還是「fail-open 退回環境變數了」，光看 filter 內容
    分不出來（DB 權限剛好是空的、跟 DB 查失敗退回空環境變數，長得一樣）。
    """
    if not emp_id:
        return _env_filter(), "env:no_emp_id"
    try:
        perm = db.get_permission_by_emp_id(emp_id)
    except Exception as e:  # noqa: BLE001
        log.warning("查詢使用者權限失敗(emp_id=%s): %s，退回環境變數 RAG_FILTER", emp_id, e)
        return _env_filter(), "env:db_error"
    if perm and perm.get("filter_criteria"):
        return perm["filter_criteria"], "db"
    log.info("emp_id=%s 沒有 DB 權限紀錄，退回環境變數 RAG_FILTER", emp_id)
    return _env_filter(), "env:no_permission_row"


def _query_rag(query: str, topk: int, filter_list: list):
    payload = {
        "search": query,
        "filter": filter_list,
        "topk": topk,
        # RAG API 現在分兩階段：r_topk 是檢索階段的候選數（預設 30），
        # topk 是 rerank 後篩到的輸出數。若 topk 超過 r_topk 預設值，
        # 結果會被攔腰砍掉，所以兩個要帶一樣的值，維持「topk 就是總筆數」的語意。
        "r_topk": topk,
    }
    resp = httpx.post(
        f"{RAG_BASE_URL}/api/v1/query",
        json=payload,
        timeout=RAG_TIMEOUT,
        verify=RAG_VERIFY_SSL,
    )
    resp.raise_for_status()
    # 回應現在包成 {"results": [...], "timing": {...}}，不再是裸陣列。
    return resp.json().get("results", [])


def _query_azure(query: str, filter_list: list) -> tuple[str, list]:
    """呼叫 KBApi/AskQuestionStream，回傳（完整答案文字, 來源清單）。

    實測過的真實 SSE 格式（deveip3.avc.co，2026-08-19 手動打過確認，不是猜的）：
      data: {"type":"text_stream","data":"<逐段文字片段>"}
        → 累積成答案本文；文字裡會內嵌 [1][2] 這種數字引用索引。
      data: {"type":"reference_files","data":[{"fileid":...,"filename":...,
             "sasurl":...,"actionType":...,"citationIndex":...}, ...]}
        → 來源清單，citationIndex 對應本文裡的 [1][2] 數字。
      data: {"type":"end","data":"[DONE]"}
        → 串流結束。
    """
    payload = {
        "messages": [{"role": "user", "content": query}],
        "RewrittenQuery": query,
        "stats": {"RetryCount": 0},
        "filterCriteria": filter_list,
    }
    parts: list[str] = []
    sources: list[dict] = []
    with httpx.stream(
        "POST",
        AZURE_KB_URL,
        json=payload,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        timeout=AZURE_KB_TIMEOUT,
        verify=AZURE_KB_VERIFY_SSL,
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            raw = line[len("data:"):].strip()
            if not raw:
                continue
            try:
                chunk = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg_type = chunk.get("type")
            if msg_type == "text_stream":
                text = chunk.get("data")
                if text:
                    parts.append(text)
            elif msg_type == "reference_files":
                # 這裡的欄位名稱直接照抄 Azure 原始回應，不要改名——前端 Vue 樣板的
                # refDisplayName/refIcon/refActionType/handleReferenceFile 這幾個
                # 既有方法認的就是 fileid/filename/sasurl/actionType/citationIndex
                # 這幾個名字，改了名字前端就要多一層轉換，沒有必要。
                for ref in chunk.get("data") or []:
                    sources.append({
                        "fileid": ref.get("fileid") or "",
                        "filename": ref.get("filename") or "",
                        "sasurl": ref.get("sasurl") or "",
                        "actionType": ref.get("actionType") or "",
                        "citationIndex": ref.get("citationIndex"),
                    })
            elif msg_type == "end":
                break
    return "".join(parts), sources


def _format(nodes: list):
    """把節點轉成給模型看的文字＋來源清單。

    引用用 RAG 服務 metadata 裡的 FileID（該檔案的唯一 UUID/hex ID）標註，
    不是自己編號的 [1][2][3]——避免筆數一多（甚至上百筆）時，模型引用的序號
    跟實際來源對不上；FileID 直接對應回真正的檔案，來源絕對正確。
    """
    parts = []
    sources = []
    for i, node in enumerate(nodes, 1):
        text = (node.get("text") or "").strip()
        meta = node.get("metadata") or {}
        file_id = meta.get("FileID") or meta.get("file_id") or f"src-{i}"
        name = meta.get("FileName") or meta.get("file_name") or file_id
        url = meta.get("ReferenceURL") or meta.get("reference_url") or ""
        if url.strip().upper() in ("", "N/A"):  # RAG 服務沒有連結時填 "N/A"，不是有效網址
            url = ""
        parts.append(f"[{file_id}] 來源：{name}\n{text}")
        sources.append({"n": file_id, "name": name, "url": url})

    content = (
        "以下是從公司知識庫檢索到的資料。回答時請只根據這些內容，"
        "並在每個句子後面、句號之前，原樣照抄該段落開頭方括號內的 FileID"
        "（例如 [5d73300befa4450ea08808ece5a49d38]）標註它依據的來源；"
        "不要自己編號、不要省略字元、不要修改 FileID 內容：\n\n" + "\n\n".join(parts)
    )
    return content, sources


def _search(query: str, filter_list: list, trace: dict | None = None):
    """命中料號格式或 CAR/LL/MRB/報廢 等關鍵字時，直接用擴大過的 topk 查詢；
    若這類查詢結果剛好撈滿 topk（代表可能還有更多筆被截斷），且尚未到 RAG_TOPK_MAX，
    會自動再用 RAG_TOPK_MAX 查一次以擴大搜尋範圍。

    一般語意問題不吃「撈滿就升級」這條：rerank 幾乎必然把 topk 填滿，撈滿不代表
    被截斷，只是正常結果——直接升到 MAX 只會把不相關的段落也塞進 context。

    KB_BACKEND=azure 時完全走另一條路：AskQuestionStream 自己做完檢索＋生成，
    答案本文裡已經內嵌 [1][2] 這種數字引用索引，不套用下面 local 這條路的
    [FileID] 引用範本。來源清單附兩份：一份是給人看的「[索引] 檔名」文字，
    接在答案後面；另一份是包在 HTML 註解裡的 KB_FILES_JSON 結構化資料，給
    kiki-chat-openclaw/backend 從 session history 撈出來直接轉發給前端
    （見 KB_SOURCE_PASSTHROUGH_ENABLED），不是給模型讀的——HTML 註解在
    markdown 轉譯後本來就不會顯示，模型抄不抄都不影響 backend 撈得到資料。

    trace 是給 log 用的旁路：把「這次實際用了多大的 topk、有沒有走擴大搜尋、
    撈到幾筆、有沒有出錯」填進去。錯誤在這裡本來就被吞成回給模型的文字
    （檢索失敗不該讓整個 tool call 炸掉），不填 trace 的話 log 只會看到一句
    「知識庫檢索失敗」而查不出原因。
    """
    if trace is None:
        trace = {}
    if KB_BACKEND == "azure":
        try:
            answer, sources = _query_azure(query, filter_list)
        except Exception as e:  # noqa: BLE001
            trace["error"] = repr(e)
            return f"知識庫檢索失敗：{e}", []
        trace["hit_count"] = len(sources)
        if not answer:
            return "知識庫中查無相關資料。", []
        if sources:
            # 這裡故意不加一段給人看的「來源：[1] 檔名...」文字——模型會把它原樣
            # 抄進自己最終的回覆，跟真正走 kb_files passthrough 出來的結構化
            # 清單同時出現在畫面上，看起來像重複兩份來源清單。答案本文（Azure
            # 自己的 text_stream）已經內嵌 [1][2] 這種引用，模型不需要這段提示
            # 也知道怎麼標註；HTML 註解才是唯一給 backend 撈的來源，不給模型看。
            files_json = json.dumps(sources, ensure_ascii=False)
            answer = f"{answer}\n\n<!--KB_FILES_JSON:{files_json}-->"
        return answer, sources

    expanded = _needs_expanded_search(query)
    topk = RAG_TOPK_EXPANDED if expanded else RAG_TOPK
    trace["expanded"] = expanded
    trace["topk"] = topk
    try:
        nodes = _query_rag(query, topk, filter_list)
        if expanded and len(nodes) >= topk and topk < RAG_TOPK_MAX:
            trace["topk"] = RAG_TOPK_MAX
            trace["retried_with_max_topk"] = True
            nodes = _query_rag(query, RAG_TOPK_MAX, filter_list)
    except Exception as e:  # noqa: BLE001
        trace["error"] = repr(e)
        return f"知識庫檢索失敗：{e}", []

    trace["hit_count"] = len(nodes)
    # 分數只在原始節點上，_format() 之後就沒了；留著才看得出「撈到的東西
    # 到底沾不沾邊」，是判斷 filter 帶對沒帶對的主要依據之一。
    trace["scores"] = [node.get("score") for node in nodes]
    if not nodes:
        return "知識庫中查無相關資料。", []

    return _format(nodes)


def _search_and_log(query: str, filter_list: list, **fields):
    """_search 的外皮：跑完把「進來的 filter 參數＋完整結果」寫一筆到 log 檔。

    fields 由呼叫端補上「這次是哪支 tool、filter 是誰給的」——兩支 tool 的
    filter 來源天差地遠（一支是 header 的 emp_id 查 DB，一支是模型自己填），
    log 裡分不出來的話這份紀錄就沒有意義。
    """
    trace: dict = {}
    started = time.perf_counter()
    record = {**fields, "kb_backend": KB_BACKEND, "query": query, "filter_used": filter_list}
    try:
        content, sources = _search(query, filter_list, trace)
    except Exception as e:  # noqa: BLE001
        # _search 已經把已知錯誤吞成錯誤字串了，這裡純粹是保險：真的漏出來的
        # 例外也要留下紀錄，寫完原樣往上丟，不改變原本的行為。
        record["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        record["search"] = trace
        record["error"] = repr(e)
        kb_log.write(record)
        raise
    record["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
    record["search"] = trace
    record["result"] = {
        # content 是「模型當時實際讀到的整份文字」，完整留著才能事後重現
        "content": content,
        "sources": sources,
        "source_count": len(sources),
    }
    kb_log.write(record)
    return content, sources


def register(server) -> None:
    @server.tool(
        name="knowledge_search",
        description="在公司內部知識庫中檢索與問題相關的文件內容。"
                    "何時使用：使用者的問題涉及公司文件、內部規範、產品或流程等需要查資料才能回答時。",
    )
    def knowledge_search(query: str, ctx: Context) -> CallToolResult:
        """要檢索的問題或關鍵字（用自然語言即可）"""
        emp_id = (ctx.headers or {}).get("x-emp-id")
        filter_list, filter_source = _resolve_filter(emp_id)
        content, sources = _search_and_log(
            query,
            filter_list,
            tool="knowledge_search",
            emp_id=emp_id,
            filter_from_model=None,  # 這條路的 filter 模型碰不到，是 header 的 emp_id 查出來的
            filter_source=filter_source,
        )
        # content 給模型讀；sources 走 structured_content，給 backend 組「參考資料」用
        # （對齊原本 in-process 版本的 content_and_artifact 設計）。
        return CallToolResult(
            content=[TextContent(type="text", text=content)],
            structured_content={"sources": sources},
        )

    @server.tool(
        name="knowledge_search_plain",
        description="在公司內部知識庫中檢索與問題相關的文件內容。"
                    "何時使用：使用者的問題涉及公司文件、內部規範、產品或流程等需要查資料才能回答時。"
                    "filter_criteria 必填：由前端依目前登入者權限組好的過濾條件陣列，原樣照抄，不要自己編造或省略。",
    )
    def knowledge_search_plain(query: str, filter_criteria: list[dict], ctx: Context) -> CallToolResult:
        """query: 要檢索的問題或關鍵字（用自然語言即可）。
        filter_criteria: 前端（DNN 權限系統）已經解析好的權限過濾條件陣列，每筆
        物件帶 docType/compCode/depCode/empID/allowDirect/metadataIds。這個
        client（openclaw）不支援用 HTTP header 傳遞呼叫端身分（每個 MCP 連線
        的 header 是設定檔裡的固定值，沒辦法依每次呼叫動態帶入），所以權限範圍
        改成明確的 tool 參數直接傳整包 filter_criteria，不再由這支 tool 自己
        依 emp_id 查表——換來的代價是模型看得到、也可能填錯或亂填，不像原本
        knowledge_search 的 header 版本那麼硬；filter_criteria 若沒填或給空
        陣列一律視為未提供權限範圍，退回環境變數 RAG_FILTER。
        """
        # 給 openclaw 這類不支援 content_and_artifact 分工的 MCP client 用：
        # openclaw 只要偵測到 structured_content 存在，就會整個蓋掉 content，
        # 模型永遠看不到帶全文的那份——所以這裡完全不回 structured_content，
        # 逼它退回去用 content（sources 已經內嵌在文字裡的 [FileID] 標註了）。
        content, _sources = _search_and_log(
            query,
            filter_criteria or _env_filter(),
            tool="knowledge_search_plain",
            emp_id=None,
            # 這才是這份 log 最主要的目的：模型自己填的 filter_criteria 原樣留存
            filter_from_model=filter_criteria,
            filter_source="model" if filter_criteria else "env:empty_filter_criteria",
        )
        return CallToolResult(content=[TextContent(type="text", text=content)])


def register_http_routes(server) -> None:
    """給 openclaw 的 kb-search plugin 用的純 HTTP 入口（見 openclaw_plugins/kb-search/）。

    為什麼需要這條路：openclaw 的 MCP client 每條連線的 header 是設定檔裡的固定
    值，沒辦法依每次呼叫動態帶入呼叫者身分——這正是 knowledge_search_plain 只好
    把權限範圍開成模型看得到的 tool 參數的原因。改由 openclaw 這端的原生 plugin
    從 runtime 的 toolContext.agentId（本部署就是工號，模型看不到也改不掉）解出
    身分，再用 X-Emp-Id header 打進這裡，權限範圍就回到跟 knowledge_search 同一
    個保證等級：只由呼叫端身分決定。

    另外掛一支 PUT /kb/scope 收「前端勾選的自訂檢索範圍」——openclaw 沒有
    per-request 的任意資料通道，範圍只能繞過它從 backend 直接寫進來，理由與
    「範圍只能縮小不能放大」的推導見 app/module/kb_scope.py 開頭。

    檢索邏輯完全共用 knowledge_search 那條路（_resolve_filter + _search_and_log），
    這裡只負責「HTTP 進、JSON 出」，不要在這裡長出第二套檢索行為。
    """
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    async def _json_body(request: Request):
        try:
            return await request.json()
        except Exception:  # noqa: BLE001
            return None

    @server.custom_route("/kb/scope", methods=["PUT"])
    async def _kb_scope(request: Request) -> JSONResponse:
        body = await _json_body(request)
        if body is None:
            return JSONResponse({"error": "request body 必須是 JSON"}, status_code=400)
        session_key = (body.get("session_key") or "").strip()
        if not session_key:
            return JSONResponse({"error": "session_key 必填"}, status_code=400)
        try:
            scope = kb_scope.validate(body.get("scope"))
        except kb_scope.ScopeError as e:
            # 不合法就擋在寫入這一刻，讓 backend 立刻知道；默默忽略的話前端會
            # 以為範圍有生效，實際上查的是全部。
            return JSONResponse({"error": str(e)}, status_code=400)
        kb_scope.put(session_key, scope)
        return JSONResponse({"ok": True, "scope": scope})

    @server.custom_route("/kb/search", methods=["POST"])
    async def _kb_search(request: Request) -> JSONResponse:
        body = await _json_body(request)
        if body is None:
            return JSONResponse({"error": "request body 必須是 JSON"}, status_code=400)
        query = (body or {}).get("query")
        if not isinstance(query, str) or not query.strip():
            return JSONResponse({"error": "query 必填，且必須是非空字串"}, status_code=400)

        # 沒帶 X-Emp-Id 不是錯誤：openclaw 的預設 agent（main）本來就不對應任何
        # 工號，這時跟「查不到權限」一樣退回環境變數 RAG_FILTER，不要擋下整個檢索。
        emp_id = (request.headers.get("x-emp-id") or "").strip() or None
        # 只為了 log：出事時要能分辨「plugin 解出來的 agentId」跟「真的拿去查表的
        # emp_id」是不是同一個（例如 agentId 被正規化過）。
        agent_id = (request.headers.get("x-openclaw-agent-id") or "").strip() or None
        session_key = (request.headers.get("x-openclaw-session-key") or "").strip() or None

        filter_list, filter_source = _resolve_filter(emp_id)
        scope = kb_scope.get(session_key)
        narrowed = kb_scope.narrow(filter_list, scope)

        record_fields = dict(
            tool="knowledge_search_plugin",
            emp_id=emp_id,
            filter_from_model=None,  # 這條路的 filter 模型碰不到，跟 knowledge_search 一樣
            filter_source=filter_source,
            caller_agent_id=agent_id,
            session_key=session_key,
            # 範圍是前端給的，一樣要留證：事後才分得出「查不到」是權限不夠還是範圍太窄。
            scope=scope,
            filter_before_scope=filter_list,
        )

        if narrowed is None:
            # 交集為空。絕對不能改送空陣列給 RAG——那等於完全不過濾（見 kb_scope.narrow）。
            content = "指定範圍內查無相關資料。"
            kb_log.write({**record_fields, "kb_backend": KB_BACKEND, "query": query,
                          "filter_used": None, "scope_empty_intersection": True,
                          "result": {"content": content, "sources": [], "source_count": 0}})
            return JSONResponse({"content": content, "sources": []})

        # _search_and_log 整條是同步的（httpx 同步 client + 寫檔），直接在這裡跑會
        # 卡住 server 的 event loop，丟到 thread 去。
        content, sources = await asyncio.to_thread(
            _search_and_log, query, narrowed, **record_fields
        )
        return JSONResponse({"content": content, "sources": sources})
