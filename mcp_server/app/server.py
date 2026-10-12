"""獨立 MCP tool server（Streamable HTTP）——把原本 backend 內建的 skill
（knowledge_search 等）搬出來變成獨立服務，另外加了 create_excel/create_word/
create_ppt 三個文件產生 tool、web_search/read_url 與 structured_db 那包查詢。

當初一起搬過來的 calculator / text_stats（以及更早的 weather）已經移除：
模型自己就會算術和數字統計，多一支 tool 只是佔 tool schema 的位置、還多一次
往返，實際上沒人選它。

backend 用 MCP client 連這裡取得 tool、呼叫執行；knowledge_search 額外靠
X-Emp-Id 這個 HTTP header（不是 tool 參數）帶「目前登入者」的 emp_id，
確保「用誰的權限查」這件事只能由後端依登入者身分決定，模型看不到、也改不動
（見 app/tools/knowledge_search.py 的說明）。

structured_db 那包除了 CAR/PDP/員工的固定查詢，另外有 query_kb_attachments
（表單號碼 → 知識庫的 MetadataID/FileID）；拿到 ID 之後用 kb_chunks 的
get_kb_chunks 去 Azure AI Search 撈那份檔案的 chunk 全文。⚠️ get_kb_chunks
沒有辦法知道呼叫者是誰，所以預設只放行「全公司可見」的文件，原因與開關見
app/tools/kb_chunks.py 開頭的權限說明。

ui_action 那包（open_translate_modal / open_summary_modal / open_sign_modal）
是特例：伺服器端什麼都不做，只為了讓「要開哪個視窗」這個決定落進 openclaw 的
session history，由 router 轉成 SSE 事件叫前端開視窗，詳見該檔案開頭。

create_excel/create_word/create_ppt 都是用結構化參數（表格資料、公式字串、
圖表定義、段落/投影片內容）驅動 openpyxl/python-docx/python-pptx，不是讓
模型自己寫程式碼執行，所以不需要額外的 code sandbox。
"""
import asyncio
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

from mcp.server.mcpserver import MCPServer  # noqa: E402

from app.module import dynamic_skills  # noqa: E402
from app.tools import (create_excel, create_ppt, create_word, kb_chunks,  # noqa: E402
                       knowledge_search, read_url, structured_db, ui_action,
                       web_search)


@asynccontextmanager
async def _lifespan(mcp_server: MCPServer):
    # 開機先跟 db_api 拉一次使用者自建的 http/code 技能掛上；ToolManager 是純
    # 記憶體 dict，重啟就清空，db_api 才是唯一真相來源。之後每 60 秒重新整理
    # 一次，當 backend 推播（新增/刪除技能後）失敗時的安全網。
    dynamic_skills.hydrate(mcp_server)
    reconcile_task = asyncio.create_task(dynamic_skills.reconcile_loop(mcp_server))
    try:
        yield {}
    finally:
        reconcile_task.cancel()


server = MCPServer("harness-tools", version="1.0.0", lifespan=_lifespan)

knowledge_search.register(server)
create_excel.register(server)
create_word.register(server)
create_ppt.register(server)
web_search.register(server)
read_url.register(server)
structured_db.register(server)
kb_chunks.register(server)
ui_action.register(server)
dynamic_skills.register_admin_routes(server)
# openclaw 的 kb-search plugin 走這條純 HTTP 入口（不是 MCP tool），
# 才能 per-call 帶 X-Emp-Id；理由見 knowledge_search.register_http_routes。
knowledge_search.register_http_routes(server)


def main() -> None:
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "9200"))
    server.run(transport="streamable-http", host=host, port=port)


if __name__ == "__main__":
    main()
