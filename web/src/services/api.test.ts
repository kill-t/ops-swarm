import { afterEach, describe, expect, it, vi } from "vitest";

import { listIncidents } from "./api";

describe("控制面 API 客户端", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("读取事故列表并保持服务端游标结构", async () => {
    const body = { items: [], next_before_display_number: null };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(listIncidents()).resolves.toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/incidents?limit=100",
      expect.objectContaining({ headers: { Accept: "application/json" } }),
    );
  });

  it("把结构化后端错误转换为稳定客户端错误", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ error_code: "incident_not_found", message: "Incident 不存在" }),
          { status: 404, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    const request = listIncidents();
    await expect(request).rejects.toMatchObject({
      status: 404,
      errorCode: "incident_not_found",
      message: "Incident 不存在",
    });
  });
});
