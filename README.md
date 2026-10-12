# Agent Harness

前後分離、多使用者、有長短期記憶的對話式 agent。服務拆成五塊：

```
frontend (Vite+Vue) ── 對話式深色 UI（登入 + workspace + 對話列表 + 記憶面板）
      │  同源反代
backend           ── 主邏輯：認證流程、harness、兩層記憶編排
      │  HTTP                              │  MCP（Streamable HTTP）
db_api            ── 唯一連 DB 的服務   mcp_server ── tool-calling（knowledge_search/
      │                                              structured_db/get_kb_chunks/
db (pgvector) + pgadmin   ── 獨立一包           create_excel…）
```

- **db_api** 是唯一碰 Postgres 的服務；backend 完全不裝 DB driver。
- **mcp_server** 是獨立的 MCP tool server（Streamable HTTP）：backend 當 MCP client 連過去取得 tool、呼叫執行，tool 本身不在 backend process 裡跑。knowledge_search 需要「依登入者身分」查 RAG 權限（db_api 的 user_permissions 表），backend 用 `X-Emp-Id` 這個 HTTP header 帶 emp_id 過去——header 不在 tool 的 JSON schema 裡，LLM 看不到、也改不動要用誰的權限查。
- **openclaw 那條路**（kiki-chat-openclaw）：它的 MCP client 每條連線的 header 是設定檔裡的固定值，沒辦法 per-call 帶身分，所以 knowledge_search 在那邊改用 `openclaw_plugins/kb-search/` 這支原生 plugin——身分取自 openclaw runtime 給的 `agentId`（本部署就是工號），一樣不在 tool schema 裡，再用 `X-Emp-Id` 打 mcp_server 的 `POST /kb/search`。
- **自訂檢索範圍**：openclaw 對 body 欄位與 header 都是固定白名單，沒有 per-request 的任意資料通道，所以前端勾選的範圍不能跟著問題傳——由 backend 在轉發前先 `PUT /kb/scope`（以 session key 為 key）寫進 mcp_server，plugin 查詢時帶 session key 取回套用。範圍只能**縮小**：只接受 `docType`/`metadataIds`/`allowDirect` 三個沒有 `ALL`/`''` 逃生門的欄位，`compCode`/`depCode`/`empID` 一律拒收（拿它們限縮會造成權限放大）。
- **記憶兩層**：短期＝單一對話的滾動摘要 + 最近幾輪；長期＝綁 user 的事實，用 pgvector 語意召回。
- **workspace** 為租戶邊界，可多人共用；長期記憶綁 user。

## 啟動（Docker）

先起 DB 這包（會建立共用網路 harness-net）：
```bash
docker compose -f db/docker-compose.yml up -d
```
填好 backend / mcp_server 設定後起應用這包：
```bash
cp backend/.env.example backend/.env    # 填入 LLM_MODEL / LLM_API_KEY 等
cp mcp_server/.env.example mcp_server/.env    # 填入 RAG_BASE_URL 等
docker compose up -d --build
```
- 前端： http://localhost:8080
- pgAdmin： http://localhost:5050 （admin@harness.local / admin；連線 host=db、port=5432、db/user/pw=harness）

停止 / 清資料：
```bash
docker compose down                         # 停應用
docker compose -f db/docker-compose.yml down        # 停 DB（保留資料）
docker compose -f db/docker-compose.yml down -v     # 連資料一起清
```

## 服務埠

| 服務 | 埠 | 說明 |
|---|---|---|
| frontend | 8080 | nginx，serve 前端 + 反代 API |
| backend | 8000 | 主邏輯 API |
| db_api | (內部) | CRUD，僅在 harness-net 內 |
| mcp_server | (內部) | MCP tool server，僅 backend 會連 |
| db | 5432 | Postgres + pgvector |
| pgadmin | 5050 | DB 管理介面 |

## 記憶如何運作

- 每輪對話：短期記憶提供「摘要 + 最近幾輪」當上下文；長期記憶用當前問題做向量召回，注入最相關的幾條。
- 回覆後：把超出視窗的舊訊息折疊進滾動摘要；並萃取新的長期事實（embedding 後存入，去重）。

## 新增 tool

tool-calling 在獨立的 `mcp_server/` 服務裡（不在 backend process 內）。在
`mcp_server/app/tools/` 放一個 `.py`、寫一個 `register(server)` 函式用
`@server.tool()` 註冊，並在 `mcp_server/app/server.py` 呼叫它即可；backend
那邊的 `app/module/mcp_client.py` 會自動跟 mcp_server 要最新的 tool 清單，
不用改 backend 的程式碼。

## 前端（Vite + Vue）

開發：`cd frontend && npm install && npm run dev`（vite proxy 會把 API 代到 backend:8000）。
正式：由 `frontend/Dockerfile` 建置後用 nginx serve，`docker compose up` 會自動 build。


## 目錄結構（重整後）

兩個服務都採 `app/module/router/services` 分層：

    db_api/                     # 唯一碰 DB 的服務（SQLAlchemy 2.0 + Alembic）
      app/
        config.py               # 連線字串從 .env 讀（pydantic-settings）
        module/database.py      # engine / SessionLocal / Base / get_db
        module/models.py        # ORM models
        services/*.py           # CRUD 邏輯（用 ORM session）
        router/*.py             # APIRouter（端點，形狀與舊版一致）
        main.py                 # 掛 router
      alembic/                  # migration（alembic upgrade head 建表）
      .env.example              # DATABASE_URL=postgresql+psycopg://...

    backend/                    # 主邏輯（不碰 DB、無 ORM，透過 db_client 打 db_api）
      app/
        config.py               # 模型設定（profile/embedder/gateway）
        module/db_client.py      # HTTP client → db_api
        module/mcp_client.py     # MCP client → mcp_server（取代原本的 module/skills/）
        module/harness.py        # agent harness
        module/workflows/        # 既定流程層
        module/deps.py           # 登入/權限相依
        services/auth.py         # 認證
        services/memory.py       # 兩層記憶
        services/routing.py      # 路由決策（敏感/意圖/複雜度）
        router/*.py              # APIRouter（auth/workspaces/conversations/memories/skills/models/chat）
        main.py                  # 掛 router

    mcp_server/                 # 獨立 MCP tool server（Streamable HTTP）
      app/
        tools/                   # knowledge_search / structured_db（含 query_kb_attachments）
                                 #   / kb_chunks / web_search / read_url / create_excel|word|ppt
        module/db_client.py      # HTTP client → db_api（knowledge_search 查 emp_id 權限用）
        server.py                # MCPServer 實例 + tool 註冊 + 進入點
      .env.example               # RAG_BASE_URL 等（從 backend 搬過來的設定）

    openclaw_plugins/           # 給 kiki-chat-openclaw 那包裝的 openclaw 原生 plugin
      kb-search/                 # 知識庫檢索：權限身分取自 openclaw runtime 的 agentId
                                 #   （＝工號），轉發 mcp_server 的 POST /kb/search

啟動：`db_api` 的 Docker 進入點會先 `alembic upgrade head` 再開服務；
connection string 走各自 `.env` 的 `DATABASE_URL`（compose 已用 `postgresql+psycopg://`）。
