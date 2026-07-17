import { afterEach, describe, expect, it, vi } from "vitest";

import {
  AnalysisRequestError,
  requestAnalysis,
  requestDemoAnalysis,
} from "./analysis-client";
import { demoActivity } from "@/test/demo-fixtures";

const readyPayload = {
  claim: {
    damage: "Visible front-left bumper damage",
    dateTime: "Not provided",
    location: "Alexanderplatz, Berlin",
    photoCount: 1,
    status: "ready",
    whatHappened: "Another car hit my front-left bumper.",
  },
  status: "ready",
} as const;

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    headers: { "content-type": "application/json" },
    status,
  });
}

function localPhoto(): File {
  return new File(["synthetic image"], "damage.jpg", {
    type: "image/jpeg",
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("requestAnalysis", () => {
  it("sends local evidence as multipart data without overriding its content type", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse(readyPayload));

    const result = await requestAnalysis({
      photos: [{ file: localPhoto(), src: "blob:synthetic" }],
      statementMode: "text",
      statementText: "Another car hit my front-left bumper.",
      voiceFile: null,
    });

    expect(result).toEqual(readyPayload);
    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, options] = fetchMock.mock.calls[0] ?? [];
    expect(url).toBe("/api/analyze");
    expect(options?.cache).toBe("no-store");
    expect(options?.method).toBe("POST");
    expect(options?.headers).toBeUndefined();
    expect(options?.body).toBeInstanceOf(FormData);

    const formData = options?.body as FormData;
    expect(formData.getAll("photos")).toHaveLength(1);
    expect(formData.get("statementMode")).toBe("text");
    expect(formData.get("statementText")).toBe(
      "Another car hit my front-left bumper.",
    );
    expect(formData.has("voiceMemo")).toBe(false);
  });

  it("turns an approved same-origin sample into a File before analysis", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(new Blob(["sample"], { type: "image/jpeg" }), {
          headers: { "content-type": "image/jpeg" },
          status: 200,
        }),
      )
      .mockResolvedValueOnce(jsonResponse(readyPayload));

    await requestAnalysis({
      photos: [{ src: "/images/claim-flow/accident-damage.jpg" }],
      statementMode: "text",
      statementText: "Another car hit my front-left bumper.",
      voiceFile: null,
    });

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/images/claim-flow/accident-damage.jpg",
    );
    const secondCall = fetchMock.mock.calls[1];
    const body = secondCall?.[1]?.body as FormData;
    const uploaded = body.get("photos");
    expect(uploaded).toBeInstanceOf(File);
    expect((uploaded as File).name).toBe("sample-photo-1.jpg");
  });

  it("sends the one approved follow-up field and answer", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse(readyPayload));

    await requestAnalysis({
      photos: [{ file: localPhoto(), src: "blob:synthetic" }],
      questionAnswer: "Alexanderplatz, Berlin",
      questionField: "location",
      statementMode: "text",
      statementText: "Another car hit my front-left bumper.",
      voiceFile: null,
    });

    const body = fetchMock.mock.calls[0]?.[1]?.body as FormData;
    expect(body.get("questionField")).toBe("location");
    expect(body.get("questionAnswer")).toBe("Alexanderplatz, Berlin");
  });

  it("maps a safe API error without exposing provider details", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(
        {
          error: {
            code: "analysis_failed",
            message: "We couldn’t analyze these photos.",
          },
        },
        502,
      ),
    );

    const request = requestAnalysis({
      photos: [{ file: localPhoto(), src: "blob:synthetic" }],
      statementMode: "text",
      statementText: "Another car hit my front-left bumper.",
      voiceFile: null,
    });

    await expect(request).rejects.toMatchObject({
      code: "analysis_failed",
    } satisfies Partial<AnalysisRequestError>);
  });

  it("rejects an invalid success payload", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ status: "ready", technicalLog: "not allowed" }),
    );

    const request = requestAnalysis({
      photos: [{ file: localPhoto(), src: "blob:synthetic" }],
      statementMode: "text",
      statementText: "Another car hit my front-left bumper.",
      voiceFile: null,
    });

    await expect(request).rejects.toMatchObject({
      code: "invalid_response",
    } satisfies Partial<AnalysisRequestError>);
  });

  it("uses the isolated presenter endpoint for validated activity", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        jsonResponse({ activity: demoActivity, result: readyPayload }),
      );

    await expect(
      requestDemoAnalysis({
        photos: [{ file: localPhoto(), src: "blob:synthetic" }],
        statementMode: "text",
        statementText: "Another car hit my front-left bumper.",
        voiceFile: null,
      }),
    ).resolves.toEqual({ activity: demoActivity, result: readyPayload });
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/demo/analyze");
  });
});
