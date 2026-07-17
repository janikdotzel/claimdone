import type OpenAI from "openai";
import { describe, expect, it, vi } from "vitest";

import type { AnalyzeResponse } from "@/lib/analysis-schema";

import {
  ANALYSIS_MODEL,
  OpenAIClaimAnalyzer,
  TRANSCRIPTION_MODEL,
} from "./openai-claim-analyzer";

const readyResult: AnalyzeResponse = {
  claim: {
    damage: "Front-left bumper dent and scratches",
    dateTime: "Not provided",
    location: "Alexanderplatz, Berlin",
    photoCount: 2,
    status: "ready",
    whatHappened: "Another car hit my front-left bumper.",
  },
  status: "ready",
};

const trace = {
  decisionDetail: "The claim can be prepared for customer review.",
  fieldChecks: [
    {
      detail: "Front-left bumper damage is supported by the photos and statement.",
      field: "damage",
      sources: ["photo_2", "statement"],
      status: "confirmed",
    },
    {
      detail: "No date or time was provided.",
      field: "date_time",
      sources: ["not_provided"],
      status: "missing",
    },
    {
      detail: "The location was supplied by the customer.",
      field: "location",
      sources: ["follow_up"],
      status: "confirmed",
    },
    {
      detail: "The collision description is supported by the statement.",
      field: "what_happened",
      sources: ["statement"],
      status: "confirmed",
    },
  ],
  photoFindings: [
    {
      observation: "The photo shows both vehicles at an intersection.",
      photoIndex: 1,
      status: "useful",
    },
    {
      observation: "The photo shows damage on the front-left bumper.",
      photoIndex: 2,
      status: "useful",
    },
  ],
  statementFinding: "The customer describes a front-left collision while stopped.",
} as const;

function createClient(outputParsed: unknown = { result: readyResult, trace }) {
  const parse = vi.fn(async (request: unknown) => {
    void request;
    return { output_parsed: outputParsed };
  });
  const transcribe = vi.fn(async (request: unknown) => {
    void request;
    return { text: "  Synthetic transcript.  " };
  });
  const client = {
    audio: { transcriptions: { create: transcribe } },
    responses: { parse },
  } as unknown as OpenAI;

  return { client, parse, transcribe };
}

describe("OpenAIClaimAnalyzer", () => {
  it("sends every photo as high-detail image input and includes the follow-up", async () => {
    const { client, parse } = createClient();
    const analyzer = new OpenAIClaimAnalyzer(client);

    await expect(
      analyzer.analyze({
        followUp: {
          answer: "Alexanderplatz, Berlin",
          field: "location",
        },
        photos: [
          new File(["photo-one"], "overview.jpg", { type: "image/jpeg" }),
          new File(["photo-two"], "damage.png", { type: "image/png" }),
        ],
        statement: "Another car hit my bumper.",
        statementMode: "text",
      }),
    ).resolves.toMatchObject({
      activity: {
        events: expect.arrayContaining([
          expect.objectContaining({ title: "Photo 1 reviewed" }),
          expect.objectContaining({ title: "Damage: confirmed" }),
        ]),
      },
      result: readyResult,
    });

    expect(parse).toHaveBeenCalledOnce();
    expect(parse).toHaveBeenCalledWith(
      expect.objectContaining({ model: ANALYSIS_MODEL }),
    );
    const request = JSON.stringify(parse.mock.calls[0]?.[0]);
    expect(request).toContain("Another car hit my bumper.");
    expect(request).toContain("Follow-up field: location");
    expect(request).toContain("Follow-up answer: Alexanderplatz, Berlin");
    expect(request).toContain("data:image/jpeg;base64,cGhvdG8tb25l");
    expect(request).toContain("data:image/png;base64,cGhvdG8tdHdv");
    expect(request.match(/\"detail\":\"high\"/g)).toHaveLength(2);
  });

  it("rejects undeclared provider output", async () => {
    const { client } = createClient({
      result: { ...readyResult, technicalLog: "must not reach the client" },
    });
    const analyzer = new OpenAIClaimAnalyzer(client);

    await expect(
      analyzer.analyze({
        photos: [new File(["photo"], "damage.jpg", { type: "image/jpeg" })],
        statement: "Another car hit my bumper.",
        statementMode: "text",
      }),
    ).rejects.toThrow("invalid claim result");
  });

  it("uses the transcription model and trims the returned text", async () => {
    const { client, transcribe } = createClient();
    const analyzer = new OpenAIClaimAnalyzer(client);
    const memo = new File(["audio"], "memo.webm", { type: "audio/webm" });

    await expect(analyzer.transcribe(memo)).resolves.toBe("Synthetic transcript.");
    expect(transcribe).toHaveBeenCalledWith({
      file: memo,
      model: TRANSCRIPTION_MODEL,
    });
  });
});
