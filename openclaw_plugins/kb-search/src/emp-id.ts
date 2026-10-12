/**
 * 從 openclaw runtime 給的 agentId 解出「這次要用誰的權限查知識庫」。
 *
 * 本部署裡 agentId 就是工號：kiki-chat-openclaw/backend 用
 * model="openclaw/<工號>" 轉發給 gateway，gateway 再把它變成 agentId
 * （見該 repo 的 app/router/chat.py 與 openclaw 的 gateway/http-utils.ts）。
 * agentId 是 runtime 灌進 toolContext 的，不在 tool schema 裡，模型看不到、
 * 也改不掉——這就是它可以拿來當權限身分的原因。
 */

/**
 * openclaw 的預設 agent。backend 在「沒指定工號」時打的就是 openclaw/main，
 * 它不對應任何員工，當成沒有身分處理（mcp_server 會退回環境變數 RAG_FILTER）。
 */
export const DEFAULT_ANONYMOUS_AGENT_IDS = ["main"] as const;

export function resolveEmpId(
  agentId: string | undefined,
  anonymousAgentIds: readonly string[] = DEFAULT_ANONYMOUS_AGENT_IDS,
): string | undefined {
  const id = agentId?.trim();
  if (!id) {
    return undefined;
  }
  // openclaw 在解析 agentId 時會轉小寫（agent-scope.ts 的 resolveSessionAgentIds），
  // 所以這裡比對也一律轉小寫。本部署的工號是純數字，轉小寫沒有影響；日後工號若
  // 含英文字母，db_api 的 by-emp-id 查詢必須是 case-insensitive，不然會查不到。
  const normalized = id.toLowerCase();
  if (anonymousAgentIds.some((candidate) => candidate.trim().toLowerCase() === normalized)) {
    return undefined;
  }
  return id;
}
