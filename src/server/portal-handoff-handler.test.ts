import { describe, expect, it, vi } from "vitest";

import type { Claim } from "@/lib/analysis-schema";
import {
  DemoPortalHandoffSuccessSchema,
  PortalHandoffErrorSchema,
  PortalHandoffSuccessSchema,
  type PortalHandoffErrorCode,
} from "@/lib/portal-handoff-schema";
import { createDemoReplay } from "@/test/demo-fixtures";

import {
  PortalAutomationNotConfiguredError,
  PortalAutomationSafetyError,
  type PortalAutomationResult,
  type PortalAutomator,
} from "./computer-use-portal";
import { createPortalHandoffHandler } from "./portal-handoff-handler";

const claim: Claim = {
  damage: "Front-left bumper dent and scratches",
  dateTime: "July 16, 2026 · 8:42 AM",
  location: "Alexanderplatz, Berlin",
  photoCount: 2,
  status: "ready",
  whatHappened: "Another car hit my front-left bumper.",
};

const prepared = {
  screenshotDataUrl: "data:image/png;base64,cG5n",
  status: "prepared",
  submitted: false,
} as const;

function requestWith(payload: unknown, contentType = "application/json"): Request {
  return new Request("http://localhost/api/portal-handoff", {
    body: JSON.stringify(payload),
    headers: { "content-type": contentType },
    method: "POST",
  });
}

function automator(result: PortalAutomationResult = prepared): PortalAutomator {
  return { prepare: vi.fn(async () => result) };
}

async function expectError(
  response: Response,
  status: number,
  code: PortalHandoffErrorCode,
): Promise<void> {
  expect(response.status).toBe(status);
  expect(response.headers.get("cache-control")).toBe("no-store");
  const payload = PortalHandoffErrorSchema.parse(await response.json());
  expect(payload.error.code).toBe(code);
  expect(payload.error.message).not.toHaveLength(0);
}

describe("createPortalHandoffHandler", () => {
  it("validates and passes the reviewed claim to the automator", async () => {
    const instance = automator();
    const handler = createPortalHandoffHandler(() => instance);

    const response = await handler(requestWith({ claim }));

    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(PortalHandoffSuccessSchema.parse(await response.json())).toEqual(
      prepared,
    );
    expect(instance.prepare).toHaveBeenCalledWith(claim, {
      captureReplay: false,
    });
  });

  it("returns captured replay only from the presenter handler", async () => {
    const replay = createDemoReplay(prepared.screenshotDataUrl);
    const instance = automator({ ...prepared, replay });
    const handler = createPortalHandoffHandler(() => instance, {
      includeReplay: true,
    });

    const response = await handler(requestWith({ claim }));
    const payload = DemoPortalHandoffSuccessSchema.parse(await response.json());

    expect(payload.replay).toEqual(replay);
    expect(instance.prepare).toHaveBeenCalledWith(claim, {
      captureReplay: true,
    });
  });

  it("strips internal replay data from the normal portal response", async () => {
    const replay = createDemoReplay(prepared.screenshotDataUrl);
    const handler = createPortalHandoffHandler(() =>
      automator({ ...prepared, replay }),
    );

    const response = await handler(requestWith({ claim }));

    expect(await response.json()).toEqual(prepared);
  });

  it.each([
    [requestWith({ claim, targetUrl: "https://insurer.example" }), "extra field"],
    [requestWith({ claim: { ...claim, status: "draft" } }), "invalid claim"],
    [requestWith({ claim }, "text/plain"), "wrong content type"],
  ])("returns invalid_input for %s", async (request) => {
    const instance = automator();
    const handler = createPortalHandoffHandler(() => instance);

    await expectError(await handler(request), 400, "invalid_input");
    expect(instance.prepare).not.toHaveBeenCalled();
  });

  it.each([
    ["damage", "Not provided"],
    ["dateTime", "  NoT PrOvIdEd  "],
    ["location", "Not provided"],
    ["whatHappened", "Not provided"],
  ] as const)(
    "blocks a claim whose required %s detail is not provided",
    async (field, value) => {
      const instance = automator();
      const handler = createPortalHandoffHandler(() => instance);

      await expectError(
        await handler(
          requestWith({ claim: { ...claim, [field]: value } }),
        ),
        400,
        "invalid_input",
      );
      expect(instance.prepare).not.toHaveBeenCalled();
    },
  );

  it("returns invalid_input for malformed JSON", async () => {
    const handler = createPortalHandoffHandler(() => automator());
    const request = new Request("http://localhost/api/portal-handoff", {
      body: "{",
      headers: { "content-type": "application/json" },
      method: "POST",
    });

    await expectError(await handler(request), 400, "invalid_input");
  });

  it("returns not_configured when no API key is available", async () => {
    const handler = createPortalHandoffHandler(() => {
      throw new PortalAutomationNotConfiguredError();
    });

    await expectError(
      await handler(requestWith({ claim })),
      503,
      "not_configured",
    );
  });

  it("returns safety_blocked without acknowledging a safety stop", async () => {
    const handler = createPortalHandoffHandler(() => ({
      prepare: vi.fn(async () => {
        throw new PortalAutomationSafetyError();
      }),
    }));

    await expectError(
      await handler(requestWith({ claim })),
      409,
      "safety_blocked",
    );
  });

  it("returns a safe automation_failed response for provider failures", async () => {
    const handler = createPortalHandoffHandler(() => ({
      prepare: vi.fn(async () => {
        throw new Error("sensitive provider detail");
      }),
    }));

    await expectError(
      await handler(requestWith({ claim })),
      502,
      "automation_failed",
    );
  });

  it("rejects an invalid automator success payload", async () => {
    const handler = createPortalHandoffHandler(
      () =>
        ({
          prepare: vi.fn(async () => ({
            ...prepared,
            submitted: true,
          })),
        }) as unknown as PortalAutomator,
    );

    await expectError(
      await handler(requestWith({ claim })),
      502,
      "automation_failed",
    );
  });
});
