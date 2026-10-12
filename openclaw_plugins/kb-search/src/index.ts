/**
 * KB Search —— 把公司知識庫檢索做成 openclaw 原生 tool plugin。
 *
 * ── 為什麼不繼續用 MCP tool ──
 * openclaw 的 MCP client，每條連線的 header 是設定檔裡的固定值，沒辦法依每次
 * 呼叫動態帶入呼叫者身分。原本 mcp_server 的 knowledge_search 是靠 X-Emp-Id
 * header 決定「用誰的權限查」，換到 openclaw 底下就只好退而求其次，開一支
 * knowledge_search_plain 把權限範圍 filter_criteria 變成模型看得到的 tool
 * 參數——代價是模型可能填錯、漏填、甚至亂填。
 *
 * 做成原生 plugin 之後身分改從 toolContext.agentId 拿：本部署的 agentId 就是
 * 工號（backend 用 model="openclaw/<工號>" 轉發），由 runtime 灌進來，不在
 * tool schema 裡，模型看不到也改不掉——回到跟原本 header 版本同一個保證等級。
 *
 * ── ⚠️ 身分只能綁在 factory 這一層 ──
 * AgentTool.execute 的簽章是 (toolCallId, params, signal, onUpdate)，拿不到
 * toolContext，所以 agentId 必須在 factory 建立 tool 時就綁進 closure。
 *
 * 這是安全的：openclaw 的 plugin tool descriptor cache 只快取 metadata
 * （名稱／描述／schema），factory 一律用「當次 run 新建的 ctx」執行——冷路徑
 * 在組 tool list 時跑（src/plugins/tools.ts 的 resolvePluginToolFactory），
 * 快取路徑則是在 execute 裡面才跑（同檔 createCachedDescriptorPluginTool）。
 * 所以 agentId、sessionKey、sessionId 在 factory 裡拿到的都是這一次呼叫的值。
 *
 * 檢索邏輯本身完全不在這裡，全部轉發給 mcp_server 的 POST /kb/search
 * （見 harness_agent/mcp_server/app/tools/knowledge_search.py）。
 */
import { Type } from "typebox";
import type { AnyAgentTool } from "openclaw/plugin-sdk/plugin-entry";
import { defineToolPlugin } from "openclaw/plugin-sdk/tool-plugin";
import { textResult } from "openclaw/plugin-sdk/tool-results";
import { DEFAULT_ANONYMOUS_AGENT_IDS, resolveEmpId } from "./emp-id.js";
import { searchKnowledgeBase, type KbSource } from "./kb-client.js";

/** harness_agent 那包的 mcp_server，在 kiki-chat-openclaw-network 上的位址。 */
const DEFAULT_BASE_URL = "http://harness-mcp_server:9200";
/** Azure 那條路是「檢索＋生成」一次做完，比單純檢索慢得多，逾時抓寬一點。 */
const DEFAULT_TIMEOUT_MS = 120_000;

const configSchema = Type.Object(
  {
    baseUrl: Type.Optional(
      Type.String({
        description: `mcp_server base URL（預設 ${DEFAULT_BASE_URL}）。`,
      }),
    ),
    timeoutMs: Type.Optional(
      Type.Integer({
        minimum: 1_000,
        description: `單次檢索逾時毫秒數（預設 ${DEFAULT_TIMEOUT_MS}）。`,
      }),
    ),
    anonymousAgentIds: Type.Optional(
      Type.Array(Type.String(), {
        description:
          "不對應任何工號的 agentId 清單；命中時當作沒有登入者身分，" +
          `由 mcp_server 退回環境變數 RAG_FILTER（預設 ${JSON.stringify(DEFAULT_ANONYMOUS_AGENT_IDS)}）。`,
      }),
    ),
  },
  { additionalProperties: false },
);

type KbSearchConfig = {
  baseUrl?: string;
  timeoutMs?: number;
  anonymousAgentIds?: string[];
};

const parameters = Type.Object(
  {
    query: Type.String({
      description: "要檢索的問題或關鍵字（用自然語言即可）。",
    }),
  },
  { additionalProperties: false },
);

const DESCRIPTION =
  "在公司內部知識庫中檢索與問題相關的文件內容。" +
  "何時使用：使用者的問題涉及公司文件、內部規範、產品或流程等需要查資料才能回答時。";

/**
 * 把身分綁進 closure 之後的實際 tool。empId 是這支 tool 唯一的權限來源，
 * 而它來自 runtime、不是參數——模型沒有任何方式影響它。
 */
export function createKnowledgeSearchTool(params: {
  config: KbSearchConfig;
  empId?: string;
  agentId?: string;
  sessionKey?: string;
}): AnyAgentTool {
  const baseUrl = params.config.baseUrl?.trim() || DEFAULT_BASE_URL;
  const timeoutMs = params.config.timeoutMs ?? DEFAULT_TIMEOUT_MS;

  return {
    name: "knowledge_search",
    label: "Knowledge Search",
    description: DESCRIPTION,
    parameters,
    execute: async (_toolCallId: string, { query }: { query: string }, signal?: AbortSignal) => {
      const { content, sources } = await searchKnowledgeBase({
        baseUrl,
        query,
        timeoutMs,
        empId: params.empId,
        agentId: params.agentId,
        sessionKey: params.sessionKey,
        signal,
      });
      // content 就是模型該讀到的整份文字（含 [FileID] 引用標註，Azure 路徑另外
      // 內嵌 KB_FILES_JSON 註解給 backend 撈）。sources 只放進 details 供 log／UI
      // 使用，不會蓋掉 content——這正是原本 MCP 版本做不到、只好完全不回
      // structured_content 的那件事。
      return textResult<{ sources: KbSource[]; empId?: string }>(content, {
        sources,
        ...(params.empId ? { empId: params.empId } : {}),
      });
    },
  } as AnyAgentTool;
}

export default defineToolPlugin({
  id: "kb-search",
  name: "KB Search",
  description: "公司內部知識庫檢索（權限依呼叫端工號，不由模型決定）。",
  configSchema,
  tools: (tool) => [
    tool({
      name: "knowledge_search",
      label: "Knowledge Search",
      description: DESCRIPTION,
      parameters,
      // 用 factory 而不是 execute：execute 那層拿不到 toolContext，就拿不到
      // agentId，也就回到「權限只能由模型自己填」的老問題。
      factory: ({ config, toolContext }) =>
        createKnowledgeSearchTool({
          config: config as KbSearchConfig,
          empId: resolveEmpId(toolContext.agentId, config.anonymousAgentIds),
          agentId: toolContext.agentId,
          // 前端勾選的檢索範圍是以 session key 為 key 存在 mcp_server 那邊的；
          // factory 拿到的 ctx 一定是當次 run 的，所以這裡綁是安全的（見檔頭）。
          sessionKey: toolContext.sessionKey,
        }),
    }),
  ],
});
