// @vitest-environment node

import { describe, expect, it, vi } from "vitest";

import {
  AnalyzeErrorSchema,
  AnalyzeResponseSchema,
  type AnalyzeResponse,
} from "@/lib/analysis-schema";
import { DemoAnalyzeResponseSchema } from "@/lib/demo-analysis-schema";
import { demoActivity } from "@/test/demo-fixtures";

import { createAnalyzeHandler } from "./analyze-handler";
import {
  AnalyzerNotConfiguredError,
  type ClaimAnalyzer,
} from "./claim-analyzer";

const SYNTHETIC_STATEMENT =
  "I was stopped at a red light when another car hit the front-left side of my car.";

function readyResponse(): AnalyzeResponse {
  return {
    claim: {
      damage: "Front-left bumper dent and scratches",
      dateTime: "Not provided",
      location: "Not provided",
      photoCount: 1,
      status: "ready",
      whatHappened: SYNTHETIC_STATEMENT,
    },
    status: "ready",
  };
}

function needsInformationResponse(): AnalyzeResponse {
  return {
    question: {
      field: "location",
      prompt: "Where did the accident happen?",
    },
    status: "needs_information",
  };
}

function createAnalyzer(
  result: AnalyzeResponse = readyResponse(),
  transcript = "The other car hit my front-left bumper.",
) {
  const analyze = vi.fn<ClaimAnalyzer["analyze"]>(async () => result);
  const transcribe = vi.fn<ClaimAnalyzer["transcribe"]>(
    async () => transcript,
  );
  const analyzer: ClaimAnalyzer = { analyze, transcribe };

  return { analyze, analyzer, transcribe };
}

function photo(
  name = "accident.jpg",
  type = "image/jpeg",
  size = 4,
): File {
  return new File([new Uint8Array(size)], name, { type });
}

function voiceMemo(
  name = "memo.m4a",
  type = "audio/mp4",
  size = 4,
): File {
  return new File([new Uint8Array(size)], name, { type });
}

function textForm(photoCount = 1): FormData {
  const formData = new FormData();

  for (let index = 0; index < photoCount; index += 1) {
    formData.append("photos", photo(`accident-${index + 1}.jpg`));
  }

  formData.set("statementMode", "text");
  formData.set("statementText", SYNTHETIC_STATEMENT);
  return formData;
}

function requestWith(formData: FormData): Request {
  return new Request("http://localhost/api/analyze", {
    body: formData,
    method: "POST",
  });
}

async function expectError(
  response: Response,
  status: number,
  code:
    | "invalid_input"
    | "unsupported_media"
    | "payload_too_large"
    | "not_configured"
    | "analysis_failed",
) {
  expect(response.status).toBe(status);
  expect(response.headers.get("cache-control")).toBe("no-store");
  const payload = AnalyzeErrorSchema.parse(await response.json());
  expect(payload.error.code).toBe(code);
  expect(payload.error.message).not.toHaveLength(0);
}

describe("createAnalyzeHandler", () => {
  it.each([1, 2, 3] as const)(
    "returns a ready claim and owns the photo count for %i photo(s)",
    async (photoCount) => {
      const { analyze, analyzer, transcribe } = createAnalyzer();
      const handler = createAnalyzeHandler(() => analyzer);

      const response = await handler(requestWith(textForm(photoCount)));

      expect(response.status).toBe(200);
      expect(response.headers.get("cache-control")).toBe("no-store");
      const payload = AnalyzeResponseSchema.parse(await response.json());
      expect(payload.status).toBe("ready");
      if (payload.status === "ready") {
        expect(payload.claim.photoCount).toBe(photoCount);
        expect(payload.claim.status).toBe("ready");
      }
      expect(analyze).toHaveBeenCalledOnce();
      expect(analyze).toHaveBeenCalledWith(
        expect.objectContaining({
          photos: expect.arrayContaining([expect.any(File)]),
          statement: SYNTHETIC_STATEMENT,
        }),
      );
      expect(analyze.mock.calls[0]?.[0].photos).toHaveLength(photoCount);
      expect(transcribe).not.toHaveBeenCalled();
    },
  );

  it("returns observable activity only from the presenter handler", async () => {
    const result = readyResponse();
    const analyze = vi.fn<ClaimAnalyzer["analyze"]>(async () => ({
      activity: demoActivity,
      result,
    }));
    const analyzer: ClaimAnalyzer = {
      analyze,
      transcribe: vi.fn(async () => "Synthetic transcript"),
    };
    const presenterHandler = createAnalyzeHandler(() => analyzer, {
      includeActivity: true,
    });

    const presenterResponse = await presenterHandler(requestWith(textForm(3)));
    const presenterPayload = DemoAnalyzeResponseSchema.parse(
      await presenterResponse.json(),
    );

    expect(presenterPayload.activity).toEqual(demoActivity);
    expect(presenterPayload.result.status).toBe("ready");
    if (presenterPayload.result.status === "ready") {
      expect(presenterPayload.result.claim.photoCount).toBe(3);
    }
  });

  it("returns exactly one missing-information question", async () => {
    const { analyzer } = createAnalyzer(needsInformationResponse());
    const handler = createAnalyzeHandler(() => analyzer);

    const response = await handler(requestWith(textForm()));

    expect(response.status).toBe(200);
    expect(AnalyzeResponseSchema.parse(await response.json())).toEqual(
      needsInformationResponse(),
    );
  });

  it("passes the single follow-up to the analyzer and returns a ready claim", async () => {
    const { analyze, analyzer } = createAnalyzer();
    const handler = createAnalyzeHandler(() => analyzer);
    const formData = textForm();
    formData.set("questionField", "location");
    formData.set("questionAnswer", "Alexanderplatz, Berlin");

    const response = await handler(requestWith(formData));

    expect(response.status).toBe(200);
    const payload = AnalyzeResponseSchema.parse(await response.json());
    expect(payload.status).toBe("ready");
    expect(analyze).toHaveBeenCalledWith(
      expect.objectContaining({
        followUp: {
          answer: "Alexanderplatz, Berlin",
          field: "location",
        },
      }),
    );
  });

  it("rejects a second question after the single follow-up", async () => {
    const { analyzer } = createAnalyzer(needsInformationResponse());
    const handler = createAnalyzeHandler(() => analyzer);
    const formData = textForm();
    formData.set("questionField", "location");
    formData.set("questionAnswer", "Alexanderplatz, Berlin");

    await expectError(
      await handler(requestWith(formData)),
      502,
      "analysis_failed",
    );
  });

  it("transcribes a voice memo before analyzing the photos", async () => {
    const transcript = "A car reversed into my front-left bumper.";
    const { analyze, analyzer, transcribe } = createAnalyzer(
      readyResponse(),
      `  ${transcript}  `,
    );
    const handler = createAnalyzeHandler(() => analyzer);
    const formData = new FormData();
    const audio = voiceMemo();
    formData.set("photos", photo());
    formData.set("statementMode", "voice");
    formData.set("voiceMemo", audio);

    const response = await handler(requestWith(formData));

    expect(response.status).toBe(200);
    expect(transcribe).toHaveBeenCalledOnce();
    expect(transcribe.mock.calls[0]?.[0].name).toBe(audio.name);
    expect(analyze).toHaveBeenCalledWith(
      expect.objectContaining({ statement: transcript }),
    );
  });

  it("rejects an empty transcription without calling analysis", async () => {
    const { analyze, analyzer } = createAnalyzer(readyResponse(), "   ");
    const handler = createAnalyzeHandler(() => analyzer);
    const formData = new FormData();
    formData.set("photos", photo());
    formData.set("statementMode", "voice");
    formData.set("voiceMemo", voiceMemo());

    await expectError(
      await handler(requestWith(formData)),
      502,
      "analysis_failed",
    );
    expect(analyze).not.toHaveBeenCalled();
  });

  it("returns invalid_input when no photo is supplied", async () => {
    const { analyzer } = createAnalyzer();
    const handler = createAnalyzeHandler(() => analyzer);
    const formData = textForm(0);

    await expectError(
      await handler(requestWith(formData)),
      400,
      "invalid_input",
    );
  });

  it("returns invalid_input when more than three photos are supplied", async () => {
    const { analyzer } = createAnalyzer();
    const handler = createAnalyzeHandler(() => analyzer);

    await expectError(
      await handler(requestWith(textForm(4))),
      400,
      "invalid_input",
    );
  });

  it("returns unsupported_media for a non-JPG-or-PNG photo", async () => {
    const { analyzer } = createAnalyzer();
    const handler = createAnalyzeHandler(() => analyzer);
    const formData = textForm(0);
    formData.set("photos", photo("accident.gif", "image/gif"));

    await expectError(
      await handler(requestWith(formData)),
      415,
      "unsupported_media",
    );
  });

  it("returns payload_too_large for a photo over 8 MB", async () => {
    const { analyzer } = createAnalyzer();
    const handler = createAnalyzeHandler(() => analyzer);
    const formData = textForm(0);
    formData.set(
      "photos",
      photo("large.jpg", "image/jpeg", 8 * 1024 * 1024 + 1),
    );

    await expectError(
      await handler(requestWith(formData)),
      413,
      "payload_too_large",
    );
  });

  it("enforces text-or-voice XOR", async () => {
    const { analyzer } = createAnalyzer();
    const handler = createAnalyzeHandler(() => analyzer);
    const formData = textForm();
    formData.set("voiceMemo", voiceMemo());

    await expectError(
      await handler(requestWith(formData)),
      400,
      "invalid_input",
    );
  });

  it("returns unsupported_media for an unsupported voice memo", async () => {
    const { analyzer } = createAnalyzer();
    const handler = createAnalyzeHandler(() => analyzer);
    const formData = new FormData();
    formData.set("photos", photo());
    formData.set("statementMode", "voice");
    formData.set("voiceMemo", voiceMemo("memo.aac", "audio/aac"));

    await expectError(
      await handler(requestWith(formData)),
      415,
      "unsupported_media",
    );
  });

  it("returns payload_too_large for a voice memo over 10 MB", async () => {
    const { analyzer } = createAnalyzer();
    const handler = createAnalyzeHandler(() => analyzer);
    const formData = new FormData();
    formData.set("photos", photo());
    formData.set("statementMode", "voice");
    formData.set(
      "voiceMemo",
      voiceMemo("large.m4a", "audio/mp4", 10 * 1024 * 1024 + 1),
    );

    await expectError(
      await handler(requestWith(formData)),
      413,
      "payload_too_large",
    );
  });

  it("returns not_configured when no analyzer can be created", async () => {
    const handler = createAnalyzeHandler(() => {
      throw new AnalyzerNotConfiguredError();
    });

    await expectError(
      await handler(requestWith(textForm())),
      503,
      "not_configured",
    );
  });

  it("returns a safe analysis_failed response when the provider fails", async () => {
    const analyze = vi.fn(async (): Promise<AnalyzeResponse> => {
      throw new Error("sensitive provider detail");
    });
    const analyzer: ClaimAnalyzer = {
      analyze,
      transcribe: vi.fn(async () => "transcript"),
    };
    const handler = createAnalyzeHandler(() => analyzer);

    const response = await handler(requestWith(textForm()));

    await expectError(response, 502, "analysis_failed");
  });

  it("rejects provider output with undeclared fields", async () => {
    const invalidProviderOutput = {
      ...readyResponse(),
      technicalLog: "must not reach the client",
    } as unknown as AnalyzeResponse;
    const { analyzer } = createAnalyzer(invalidProviderOutput);
    const handler = createAnalyzeHandler(() => analyzer);

    await expectError(
      await handler(requestWith(textForm())),
      502,
      "analysis_failed",
    );
  });
});
