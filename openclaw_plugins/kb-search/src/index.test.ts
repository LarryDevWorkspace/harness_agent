import { afterEach, describe, expect, it, vi } from "vitest";
import { getToolPluginMetadata } from "openclaw/plugin-sdk/tool-plugin";
import plugin, { createKnowledgeSearchTool } from "./index.js";
import { resolveEmpId } from "./emp-id.js";

afterEach(() => {
  vi.unstubAllGlobals();
});

/** 攔下 fetch，回一份 mcp_server 形狀的回應，並把收到的 request 交還給測試檢查。 */
function stubFetch(body: unknown = { content: "檢索結果", sources: [] }) {
  const calls: { url: string; init: RequestInit }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit) => {
      calls.push({ url, init });
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
  return calls;
}

function headersOf(init: RequestInit): Record<string, string> {
  return init.headers as Record<string, string>;
}

describe("plugin metadata", () => {
  it("只宣告 knowledge_search 一支 tool", () => {
    const metadata = getToolPluginMetadata(plugin);
    expect(metadata?.id).toBe("kb-search");
    expect(metadata?.tools.map((tool) => tool.name)).toEqual(["knowledge_search"]);
  });

  it("模型看得到的參數只有 query —— 權限範圍絕不能出現在 schema 裡", () => {
    const metadata = getToolPluginMetadata(plugin);
    const properties = metadata?.tools[0]?.parameters?.properties ?? {};
    expect(Object.keys(properties)).toEqual(["query"]);
  });
});

describe("resolveEmpId", () => {
  it("一般 agentId 就是工號", () => {
    expect(resolveEmpId("2011994")).toBe("2011994");
  });

  it("預設 agent（main）不對應任何工號", () => {
    expect(resolveEmpId("main")).toBeUndefined();
    expect(resolveEmpId("MAIN")).toBeUndefined();
  });

  it("沒有 agentId 時視為沒有身分", () => {
    expect(resolveEmpId(undefined)).toBeUndefined();
    expect(resolveEmpId("   ")).toBeUndefined();
  });

  it("可以自訂哪些 agentId 算匿名", () => {
    expect(resolveEmpId("shared", ["main", "shared"])).toBeUndefined();
    expect(resolveEmpId("2011994", ["main", "shared"])).toBe("2011994");
  });
});

describe("knowledge_search", () => {
  it("把工號放進 X-Emp-Id header，body 只帶 query", async () => {
    const calls = stubFetch();
    const tool = createKnowledgeSearchTool({
      config: { baseUrl: "http://mcp:9200" },
      empId: "2011994",
      agentId: "2011994",
    });

    await tool.execute("call-1", { query: "報廢流程" });

    expect(calls).toHaveLength(1);
    expect(calls[0]!.url).toBe("http://mcp:9200/kb/search");
    expect(headersOf(calls[0]!.init)["x-emp-id"]).toBe("2011994");
    expect(JSON.parse(calls[0]!.init.body as string)).toEqual({ query: "報廢流程" });
  });

  it("沒有工號時不帶 X-Emp-Id，交給 mcp_server 退回環境變數", async () => {
    const calls = stubFetch();
    const tool = createKnowledgeSearchTool({ config: {}, agentId: "main" });

    await tool.execute("call-1", { query: "報廢流程" });

    expect(headersOf(calls[0]!.init)["x-emp-id"]).toBeUndefined();
  });

  it("帶上 session key，讓 mcp_server 取得前端勾選的範圍", async () => {
    const calls = stubFetch();
    const tool = createKnowledgeSearchTool({
      config: {},
      empId: "2011994",
      sessionKey: "agent:2011994:abc",
    });

    await tool.execute("call-1", { query: "報廢流程" });

    expect(headersOf(calls[0]!.init)["x-openclaw-session-key"]).toBe("agent:2011994:abc");
  });

  it("沒有 session key 時不帶該 header（等同沒選範圍）", async () => {
    const calls = stubFetch();
    const tool = createKnowledgeSearchTool({ config: {}, empId: "2011994" });

    await tool.execute("call-1", { query: "報廢流程" });

    expect(headersOf(calls[0]!.init)["x-openclaw-session-key"]).toBeUndefined();
  });

  it("content 原樣給模型，sources 只進 details 不蓋掉 content", async () => {
    stubFetch({ content: "答案本文", sources: [{ fileid: "abc", filename: "規範.pdf" }] });
    const tool = createKnowledgeSearchTool({ config: {}, empId: "2011994" });

    const result = await tool.execute("call-1", { query: "報廢流程" });

    expect(result.content).toEqual([{ type: "text", text: "答案本文" }]);
    expect(result.details).toMatchObject({
      sources: [{ fileid: "abc", filename: "規範.pdf" }],
      empId: "2011994",
    });
  });

  it("mcp_server 回非 2xx 時丟例外，不把錯誤偽裝成檢索結果", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("boom", { status: 500 })),
    );
    const tool = createKnowledgeSearchTool({ config: {}, empId: "2011994" });

    await expect(tool.execute("call-1", { query: "報廢流程" })).rejects.toThrow(/HTTP 500/);
  });
});
