import { describe, expect, it } from "vitest";

import { GET } from "../src/app/health/route";

describe("GET /health", () => {
  it("reports a healthy web service", async () => {
    const response = GET();

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({
      service: "web",
      status: "ok",
    });
  });
});
