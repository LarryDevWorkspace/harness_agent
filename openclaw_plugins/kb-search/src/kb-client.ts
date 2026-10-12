/**
 * mcp_server 的 POST /kb/search 客戶端。
 *
 * 這裡刻意只做「把身分與查詢送出去、把結果拿回來」——檢索邏輯（RAG 呼叫、
 * 料號擴大搜尋、Azure SSE 解析、權限查表、kb_log）全部留在 mcp_server 的
 * Python 那一份，不要在這裡長出第二套。
 */

/** mcp_server 回來的來源清單欄位，原樣對齊前端 Vue 樣板既有的方法名，不要改名。 */
export type KbSource = {
  n?: string;
  name?: string;
  url?: string;
  fileid?: string;
  filename?: string;
  sasurl?: string;
  actionType?: string;
  citationIndex?: number;
};

export type KbSearchResult = {
  /** 給模型讀的整份文字（Azure 路徑會內嵌 KB_FILES_JSON 註解，原樣保留）。 */
  content: string;
  sources: KbSource[];
};

export type KbSearchRequest = {
  baseUrl: string;
  query: string;
  timeoutMs: number;
  /** 解析出來的工號；undefined 代表「這次沒有登入者身分」，由 mcp_server 退回環境變數。 */
  empId?: string;
  /** 只給 mcp_server 寫 log 用，不參與權限決策。 */
  agentId?: string;
  /**
   * 這串對話的 session key。mcp_server 用它取出前端勾選的自訂檢索範圍
   * （backend 在轉發問題前先 PUT /kb/scope 寫進去的）。範圍只會縮小檢索結果，
   * 不會放大權限——權限一律來自 empId。
   */
  sessionKey?: string;
  signal?: AbortSignal;
};

function joinUrl(baseUrl: string, path: string): string {
  return `${baseUrl.replace(/\/+$/, "")}${path}`;
}

/**
 * 逾時與「使用者中止這次 tool call」是兩個獨立的中止來源，兩個都要能停掉請求，
 * 所以合成一個 signal 丟給 fetch。
 */
function buildSignal(timeoutMs: number, signal?: AbortSignal): AbortSignal {
  const timeout = AbortSignal.timeout(timeoutMs);
  return signal ? AbortSignal.any([signal, timeout]) : timeout;
}

/**
 * 只有「打不到 mcp_server / 它回非 2xx」才會 throw。檢索本身失敗（RAG 掛掉、
 * 查無資料）mcp_server 會吞成一段給模型看的文字並回 200——那是刻意的，維持跟
 * 原本 MCP tool 一樣的行為，不要在這裡改成丟例外。
 */
export async function searchKnowledgeBase(req: KbSearchRequest): Promise<KbSearchResult> {
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (req.empId) {
    headers["x-emp-id"] = req.empId;
  }
  if (req.agentId) {
    headers["x-openclaw-agent-id"] = req.agentId;
  }
  if (req.sessionKey) {
    headers["x-openclaw-session-key"] = req.sessionKey;
  }

  const resp = await fetch(joinUrl(req.baseUrl, "/kb/search"), {
    method: "POST",
    headers,
    body: JSON.stringify({ query: req.query }),
    signal: buildSignal(req.timeoutMs, req.signal),
  });

  if (!resp.ok) {
    const detail = (await resp.text().catch(() => "")).slice(0, 500);
    throw new Error(`knowledge_search 呼叫 mcp_server 失敗（HTTP ${resp.status}）：${detail}`);
  }

  const payload = (await resp.json()) as Partial<KbSearchResult>;
  if (typeof payload?.content !== "string") {
    throw new Error("knowledge_search 收到非預期的回應格式：缺少 content 欄位");
  }
  return { content: payload.content, sources: payload.sources ?? [] };
}
