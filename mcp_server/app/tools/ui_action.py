"""Tool：觸發前端 UI 動作（open_translate_modal / open_summary_modal / open_sign_modal）。

跟其他工具不一樣的地方：這幾支**伺服器端不做任何事**，它們的作用是「被呼叫」
這件事本身——呼叫會落進 openclaw 的 session history，router 輪詢時撈到，就轉成
一個自訂 SSE 事件通知前端執行對應的動作（見 kiki-chat-openclaw 的
router/agent_router.py 與 js/personal.js）。

為什麼用 tool 而不是讓模型在回覆文字裡吐一個標記：標記要靠模型原樣輸出，實測
它常常漏掉或改寫（knowledge_search 的來源清單就是因為這樣才改成 router 直接讀
tool result 原文）。走 tool 的話有 schema、有明確的 description，模型判斷「什麼
時候該呼叫」也準得多。

工具名稱刻意跟前端 events 表的 key 同名（singleentry.js 裡的 open_translate_modal
等），router 剝掉 <server>__ 前綴後就是前端要查的 key，中間不用再維護對照表，
也不會有「模型自己填函式名稱」的風險。

⚠️ 一律不要回 structured_content：openclaw 只要偵測到它就會整個蓋掉 content，
模型會看不到這裡回的文字（knowledge_search_plain / create_excel_plain 這兩支
_plain 版本就是為了繞過同一個問題才存在的）。
"""
from mcp_types import CallToolResult, TextContent

from app.module.logs import get as get_logger

log = get_logger("ui_action")


def _ack(action: str, message: str) -> CallToolResult:
    log.info("UI 動作訊號：%s", action)
    return CallToolResult(content=[TextContent(type="text", text=message)])


def register(server) -> None:
    @server.tool(
        name="open_translate_modal",
        description="幫使用者開啟「文件翻譯」視窗，並自動切到翻譯模式。"
                    "何時使用：使用者表達想翻譯文件、或想把文件轉成其他語言"
                    "（支援繁中、簡中、英文、越南文）時。"
                    "注意：這支工具只負責把視窗打開，不會真的執行翻譯，也不需要"
                    "任何參數。呼叫成功後直接告訴使用者視窗已開啟、請在視窗中上傳"
                    "文件即可，不要重複呼叫。",
    )
    def open_translate_modal() -> CallToolResult:
        return _ack("open_translate_modal",
                    "已為使用者開啟文件翻譯視窗（翻譯模式）。請提示使用者在視窗中上傳要翻譯的文件。")

    @server.tool(
        name="open_summary_modal",
        description="幫使用者開啟「文件翻譯」視窗，並自動切到摘要模式。"
                    "何時使用：使用者表達想做文件摘要、想把文件重點整理出來時。"
                    "注意：這支工具只負責把視窗打開，不會真的產生摘要，也不需要"
                    "任何參數。呼叫成功後直接告訴使用者視窗已開啟、請在視窗中上傳"
                    "文件即可，不要重複呼叫。",
    )
    def open_summary_modal() -> CallToolResult:
        return _ack("open_summary_modal",
                    "已為使用者開啟文件摘要視窗（摘要模式）。請提示使用者在視窗中上傳要摘要的文件。")

    @server.tool(
        name="open_sign_modal",
        description="幫使用者開啟「電子簽名」小助手。"
                    "何時使用：使用者表達想在文件上簽名、要做電子簽署時。"
                    "注意：這支工具只負責把工具打開，不會真的簽署，也不需要任何"
                    "參數。呼叫成功後直接告訴使用者工具已開啟、請在視窗中上傳文件"
                    "並簽名即可，不要重複呼叫。",
    )
    def open_sign_modal() -> CallToolResult:
        return _ack("open_sign_modal",
                    "已為使用者開啟電子簽名小助手。請提示使用者在視窗中上傳文件並簽名。")
