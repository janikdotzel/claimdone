import { afterEach, describe, expect, it, vi } from "vitest";

import type { Claim } from "./analysis-schema";
import {
  PortalHandoffRequestError,
  requestDemoPortalHandoff,
  requestPortalHandoff,
} from "./portal-handoff-client";
import { createDemoReplay } from "@/test/demo-fixtures";

const claim: Claim = {
  damage: "Front-left bumper dent and scratches",
  dateTime: "July 16, 2026 · 8:42 AM",
  location: "Alexanderplatz, Berlin",
  photoCount: 2,
  status: "ready",
  whatHappened: "Another car hit my front-left bumper.",
};

const preparedPayload = {
  screenshotDataUrl: "data:image/png;base64,cG5n",
  status: "prepared",
  submitted: false,
} as const;

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    headers: { "content-type": "application/json" },
    status,
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("requestPortalHandoff", () => {
  it("posts the reviewed claim and returns the strict prepared result", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse(preparedPayload));

    await expect(requestPortalHandoff(claim)).resolves.toEqual(preparedPayload);
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock).toHaveBeenCalledWith("/api/portal-handoff", {
      body: JSON.stringify({ claim }),
      cache: "no-store",
      headers: { "content-type": "application/json" },
      method: "POST",
    });
  });

  it("maps a safe API error without exposing its message", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(
        {
          error: {
            code: "safety_blocked",
            message: "Portal preparation stopped for a safety check.",
          },
        },
        409,
      ),
    );

    await expect(requestPortalHandoff(claim)).rejects.toMatchObject({
      code: "safety_blocked",
    } satisfies Partial<PortalHandoffRequestError>);
  });

  it("rejects additional fields in a success payload", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ ...preparedPayload, submittedAt: "never" }),
    );

    await expect(requestPortalHandoff(claim)).rejects.toMatchObject({
      code: "invalid_response",
    } satisfies Partial<PortalHandoffRequestError>);
  });

  it("requests captured replay only from the presenter endpoint", async () => {
    const replay = createDemoReplay(preparedPayload.screenshotDataUrl);
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ ...preparedPayload, replay }));

    await expect(requestDemoPortalHandoff(claim)).resolves.toEqual({
      ...preparedPayload,
      replay,
    });
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/demo/portal-handoff");
  });
});
