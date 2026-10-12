# kb-search —— openclaw 知識庫檢索 plugin

把公司內部知識庫檢索做成 openclaw 的**原生 tool plugin**，取代原本走 MCP 的
`knowledge_search_plain`。

## 為什麼要有這包

`mcp_server` 的 `knowledge_search` 本來靠 `X-Emp-Id` 這個 HTTP header 決定
「用誰的權限查」——header 不在 tool schema 裡，模型看不到也改不掉。

但 openclaw 的 MCP client，每條連線的 header 是設定檔裡的固定值，沒辦法依每次
呼叫動態帶入呼叫者身分。所以在 openclaw 底下只好退而求其次，另外開一支
`knowledge_search_plain`，把權限範圍 `filter_criteria` 變成模型看得到的 tool
參數——代價是模型可能填錯、漏填、甚至亂填。

做成原生 plugin 之後，身分改從 openclaw runtime 給的 `toolContext.agentId` 拿：

```
前端(DNN) ──► kiki-chat backend ──► openclaw gateway ──► kb-search plugin ──► mcp_server
             model="openclaw/<工號>"   agentId=<工號>      X-Emp-Id: <工號>     POST /kb/search
                                      (runtime 灌進來)                         → db_api 查權限 → RAG
```

`agentId` 由 runtime 灌進 tool context，不在 tool schema 裡，模型沒有任何方式
影響它——回到跟原本 header 版本同一個保證等級。

## 自訂檢索範圍（前端勾選）

前端讓使用者勾「只查 SOP」「只在這幾份文件裡問」這類範圍。這個範圍**不能**跟著
問題一起傳——openclaw 的 `/v1/chat/completions` 對 body 欄位與 header 都是固定
白名單（body 只認 `model`/`user`/`stream`/`temperature`… 沒有 `metadata`；header
只認 `x-openclaw-*`），沒有任何 per-request 的任意資料通道。所以範圍走側路：

```
前端勾選範圍
    │
    ├─► kiki-chat backend ──PUT /kb/scope──► mcp_server（以 session key 暫存）
    │        │
    │        └─ 轉發問題 ─► openclaw ─► 這支 plugin ─POST /kb/search─► mcp_server
    │                                   （帶 X-Openclaw-Session-Key）  └─ 取出範圍套用
```

用 session key 當 key，是因為那是三邊唯一都拿得到、而且一定對得起來的識別：
backend 自己算出 `agent:<工號>:<key>` 送給 gateway，plugin 從 `toolContext.sessionKey`
拿到同一個字串。

### ⚠️ 範圍只能縮小，不能放大

RAG 的 filter 是「組間 OR、組內 AND」的扁平陣列，能不能安全交集要看欄位：

| 欄位 | 支援 | 原因 |
| --- | --- | --- |
| `docType` | ✅ | `DocType == value` 嚴格等值，沒有逃生門 |
| `metadataIds` | ✅ | `MetadataID IN [...]`，沒有逃生門 |
| `allowDirect` | ✅ | 只在 `=="Y"` 時多加條件，加上去只會更嚴格 |
| `compCode` / `depCode` / `empID` | ⛔ 拒收 | 條件是 `(IN 值) OR =='ALL' OR ==''`，**帶逃生門** |

那三個被拒收的欄位不是偷懶。反例：某權限組 `compCode=""`（只看得到全公司文件），
為了限縮而填入 `"AVC"` 會變成 `IN[AVC] OR ALL OR ''`——**比原本更寬**，AVC 的文件
本來看不到現在看得到了。這是實打實的權限放大。送這些欄位會收到 400，而不是被
默默忽略（默默忽略更糟：前端會以為範圍有生效）。

要正確限縮這三個欄位，得等 RAG API 支援巢狀 filter（`(權限) AND (範圍)`）。

### backend 要做的事

轉發問題給 openclaw **之前**，先寫一次範圍（沒選範圍就送 `null` 清掉）：

```python
PUT http://harness-mcp_server:9200/kb/scope
{"session_key": "agent:2011994:<前端的 session_id>", "scope": {"docType": "SOP"}}
```

`session_key` 用 backend 已經在算的 `config.session_key(agent_id, session_id)`，
不要另外發明格式。**每次 chat request 都寫**，不要只在「使用者改變範圍時」寫——
範圍存在 mcp_server 記憶體裡（TTL 見 `KB_SCOPE_TTL`），mcp_server 一重啟就沒了，
每次都寫才是冪等、重啟也自動修復的做法。

## 這包不做什麼

檢索邏輯（RAG 呼叫、料號擴大搜尋、Azure SSE 解析、權限查表、kb_log）全部留在
`mcp_server/app/tools/knowledge_search.py`，這裡只負責「解出身分 → 轉發 → 回傳」。
不要在這裡長出第二套檢索行為。

## 幾個實作上的約束

- **身分只能綁在 `factory` 這層。** `AgentTool.execute` 的簽章是
  `(toolCallId, params, signal, onUpdate)`，拿不到 `toolContext`，所以 `agentId`
  必須在 factory 建立 tool 時就綁進 closure。
- **factory 拿到的 ctx 一定是這一次呼叫的。** plugin tool descriptor cache 只快取
  metadata（名稱／描述／schema），factory 在冷路徑是組 tool list 時跑、在快取路徑
  是 `execute` 裡面才跑，兩條都用當次 run 新建的 ctx（openclaw `src/plugins/tools.ts`）。
  所以 `agentId`、`sessionKey`、`sessionId` 都可以安全地綁進 closure。
- **openclaw 會把 `agentId` 轉小寫**（`src/agents/agent-scope.ts`）。本部署的工號
  是純數字，沒有影響；日後工號若含英文字母，`db_api` 的 by-emp-id 查詢必須是
  case-insensitive，不然會查不到。
- **`main` 不對應任何工號。** backend 沒帶工號時打的是 `openclaw/main`，這時
  當作沒有登入者身分，由 mcp_server 退回環境變數 `RAG_FILTER`——跟原本行為一致。

## 建置

host 上沒有 node，用容器建：

```bash
cd openclaw_plugins/kb-search
D="docker run --rm -u $(id -u):$(id -g) -e HOME=/work -v $PWD:/work -w /work node:24-slim"
$D npm install
$D npm run build
$D npx openclaw plugins build --entry ./dist/index.js      # 重新產生 openclaw.plugin.json
$D npx openclaw plugins validate --entry ./dist/index.js
$D npm test
```

改過 plugin id / name / description / config schema / tool 名稱之後，一定要重跑
`plugins build`，不然 manifest 的 `contracts.tools` 會過期，openclaw discovery
會看不到這支 tool。

## 安裝

兩個 openclaw 節點各有獨立的狀態目錄（`.openclaw` 與 `.openclaw-b`），**兩邊都要裝**：

```bash
cd /home/larry/workspace/kiki-chat-openclaw
docker exec openclaw   openclaw plugins install /path/to/kb-search
docker exec openclaw-b openclaw plugins install /path/to/kb-search
```

（plugin 目錄要先掛得進容器，或先複製進各自的 `.openclaw/extensions/`。）

然後在**兩個節點**的 `openclaw.json` 加上：

```json
{
  "plugins": {
    "entries": {
      "kb-search": {
        "enabled": true,
        "config": {
          "baseUrl": "http://harness-mcp_server:9200"
        }
      }
    }
  }
}
```

`baseUrl` 可省略，預設就是 `http://harness-mcp_server:9200`。裝完重啟或 reload
gateway，用 `openclaw plugins inspect kb-search --runtime` 確認 tool 有掛上。

## ⚠️ 上線時必須配套改的兩件事

這支 plugin 自己裝好還不夠，以下兩項沒改的話會出問題：

1. **把舊的 MCP tool 藏起來**，否則模型會同時看到兩支功能重複的檢索 tool。
   在兩個節點的 `openclaw.json` 把 `knowledge_search_plain` 加進排除清單：

   ```json
   "mcp": {
     "servers": {
       "harness-tools": {
         "toolFilter": { "exclude": ["create_excel", "knowledge_search", "knowledge_search_plain"] }
       }
     }
   }
   ```

   `mcp_server` 那邊的 `knowledge_search_plain` 先留著不要刪——確認 plugin 在兩個
   節點都正常之後再移除，才有回頭路。

2. **改 backend 的 `KB_TOOL_NAME`**，否則 KB 來源 passthrough（前端的參考資料
   清單）會失效。原生 plugin 的 tool 不像 MCP 會被加上 `<server>__` 前綴，所以
   在 session history 裡的 `toolName` 就是 `knowledge_search`：

   ```python
   # kiki-chat-openclaw/backend/app/router/chat.py
   KB_TOOL_NAME = "knowledge_search"   # 原本是 "harness-tools__knowledge_search_plain"
   ```

## 疑難排解

| 症狀 | 檢查 |
| --- | --- |
| tool 沒出現 | `openclaw plugins inspect kb-search --runtime`；`openclaw.plugin.json` 的 `contracts.tools`；gateway 有沒有重啟 |
| 查到的東西不對／權限範圍不對 | `mcp_server/log/kb_search-*.jsonl` 的 `emp_id`、`caller_agent_id`、`filter_source`（`db` 才是真的套到使用者權限，`env:*` 都是退回環境變數） |
| 範圍沒生效 | log 的 `session_key` 有沒有值、`scope` 是不是 null；backend 有沒有在每次 chat request 前 `PUT /kb/scope`；兩邊 session key 字串要完全一致 |
| 選了範圍就查無資料 | log 的 `scope_empty_intersection`：true 代表範圍落在使用者權限之外，交集為空（這時刻意不打 RAG——送空陣列給 RAG 等於完全不過濾） |
| 一直沒有權限 | log 的 `caller_agent_id` 是不是 `main`；或 `filter_source` 是 `env:db_error`（db_api 掛了，fail-open） |
| 呼叫直接失敗 | plugin 只有在打不到 mcp_server 或它回非 2xx 時才丟例外；檢索本身失敗會回一段文字並回 200 |
