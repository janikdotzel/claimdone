import OpenAI from "openai";
import { zodTextFormat } from "openai/helpers/zod";
import { z } from "zod";

import {
  AnalyzeResponseSchema,
  getMissingClaimDetailFields,
  MissingFieldSchema,
  type MissingField,
} from "@/lib/analysis-schema";
import {
  AgentActivitySchema,
  type AgentActivity,
  type AgentActivityEvent,
} from "@/lib/demo-analysis-schema";

import {
  AnalyzerNotConfiguredError,
  type AnalysisInput,
  type ClaimAnalyzer,
} from "./claim-analyzer";

export const ANALYSIS_MODEL = "gpt-5.4-mini";
export const TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe";

const PhotoIndexSchema = z.union([
  z.literal(1),
  z.literal(2),
  z.literal(3),
]);
const ProviderEvidenceSourceSchema = z.enum([
  "photo_1",
  "photo_2",
  "photo_3",
  "statement",
  "follow_up",
  "not_provided",
]);
const ProviderTraceSchema = z
  .object({
    decisionDetail: z.string().trim().min(1).max(300),
    fieldChecks: z
      .array(
        z
          .object({
            detail: z.string().trim().min(1).max(300),
            field: MissingFieldSchema,
            sources: z.array(ProviderEvidenceSourceSchema).min(1).max(4),
            status: z.enum(["confirmed", "missing"]),
          })
          .strict(),
      )
      .length(4),
    photoFindings: z
      .array(
        z
          .object({
            observation: z.string().trim().min(1).max(300),
            photoIndex: PhotoIndexSchema,
            status: z.enum(["useful", "limited"]),
          })
          .strict(),
      )
      .min(1)
      .max(3),
    statementFinding: z.string().trim().min(1).max(300),
  })
  .strict();

const ProviderOutputSchema = z
  .object({
    result: AnalyzeResponseSchema,
    trace: ProviderTraceSchema,
  })
  .strict();

const systemPrompt = `You prepare a minimal accident insurance claim from user-provided photos and a short statement.

Rules:
- Use only facts visible in the photos or explicitly provided by the user.
- Never invent a date, time, location, event, person, vehicle detail, or damage detail.
- Use "Not provided" for non-critical facts that were not supplied.
- Return "needs_information" only when exactly one critical detail is required to make the claim meaningful.
- A needs-information result contains exactly one short, plain-English question.
- If a follow-up answer is present, return "ready" and never ask another question.
- Keep every field concise, neutral, and understandable to an insurance customer.
- Set photoCount to the exact number of supplied images and status to "ready" for a ready claim.

Also return a structured trace of observable checks and decisions. This is not private reasoning:
- Return exactly one photoFinding for every supplied photo, in the same 1-based order.
- Mark a photo "limited" when it does not provide useful accident or damage evidence. Never claim damage is visible when it is not.
- Summarize what the statement contributes in statementFinding.
- Return exactly one fieldCheck for each of damage, date_time, location, and what_happened.
- A fieldCheck must say whether the detail is confirmed or missing and cite only its actual sources.
- Use not_provided only when no supplied source supports that field.
- decisionDetail must briefly explain the observable next step, without hidden thoughts, chain-of-thought, confidence percentages, technical logs, or provider details.`;

function buildStatement(input: AnalysisInput): string {
  const followUp = input.followUp
    ? `\nFollow-up field: ${input.followUp.field}\nFollow-up answer: ${input.followUp.answer}`
    : "";

  return `Statement mode: ${input.statementMode}\nAccident description:\n${input.statement}${followUp}`;
}

async function toDataUrl(photo: File): Promise<string> {
  const bytes = Buffer.from(await photo.arrayBuffer());
  return `data:${photo.type};base64,${bytes.toString("base64")}`;
}

const fieldLabels: Record<MissingField, string> = {
  damage: "Damage",
  date_time: "Date and time",
  location: "Location",
  what_happened: "What happened",
};

const canonicalFields = [
  "damage",
  "date_time",
  "location",
  "what_happened",
] as const satisfies readonly MissingField[];

function sourceFromProvider(
  source: z.infer<typeof ProviderEvidenceSourceSchema>,
  input: AnalysisInput,
): AgentActivityEvent["source"] {
  if (source.startsWith("photo_")) {
    const photoIndex = Number(source.slice("photo_".length));

    if (photoIndex === 1 || photoIndex === 2 || photoIndex === 3) {
      return { kind: "photo", photoIndex };
    }
  }

  if (source === "statement") {
    return { kind: "statement", mode: input.statementMode };
  }

  if (source === "follow_up" && input.followUp) {
    return { field: input.followUp.field, kind: "follow_up" };
  }

  return { kind: "system" };
}

function assertTraceIntegrity(
  output: z.infer<typeof ProviderOutputSchema>,
  input: AnalysisInput,
): void {
  const photoIndices = output.trace.photoFindings.map(
    (finding) => finding.photoIndex,
  );
  const expectedPhotoIndices = input.photos.map((_photo, index) => index + 1);

  if (
    photoIndices.length !== expectedPhotoIndices.length ||
    photoIndices.some((index, position) => index !== expectedPhotoIndices[position])
  ) {
    throw new Error("The provider returned an invalid claim trace");
  }

  const fieldChecks = new Map(
    output.trace.fieldChecks.map((check) => [check.field, check]),
  );

  if (
    fieldChecks.size !== canonicalFields.length ||
    canonicalFields.some((field) => !fieldChecks.has(field))
  ) {
    throw new Error("The provider returned an invalid claim trace");
  }

  for (const check of output.trace.fieldChecks) {
    for (const source of check.sources) {
      if (source.startsWith("photo_")) {
        const photoIndex = Number(source.slice("photo_".length));
        if (photoIndex < 1 || photoIndex > input.photos.length) {
          throw new Error("The provider returned an invalid claim trace");
        }
      }

      if (source === "follow_up" && !input.followUp) {
        throw new Error("The provider returned an invalid claim trace");
      }
    }
  }

  if (output.result.status === "needs_information") {
    const missingCheck = fieldChecks.get(output.result.question.field);
    if (missingCheck?.status !== "missing") {
      throw new Error("The provider returned an invalid claim trace");
    }
  }
}

function buildActivity(
  output: z.infer<typeof ProviderOutputSchema>,
  input: AnalysisInput,
): AgentActivity {
  const events: AgentActivityEvent[] = [];
  const addEvent = (event: Omit<AgentActivityEvent, "sequence">) => {
    events.push({ ...event, sequence: events.length });
  };

  addEvent({
    detail: `${input.photos.length} ${input.photos.length === 1 ? "photo" : "photos"} and a ${input.statementMode === "voice" ? "voice memo" : "written description"} were received.`,
    phase: "evidence",
    source: { kind: "system" },
    status: "complete",
    title: "Evidence received",
  });

  output.trace.photoFindings.forEach((finding) => {
    addEvent({
      detail: finding.observation,
      phase: "image_review",
      source: { kind: "photo", photoIndex: finding.photoIndex },
      status: finding.status === "useful" ? "complete" : "attention",
      title: `Photo ${finding.photoIndex} reviewed`,
    });
  });

  addEvent({
    detail: output.trace.statementFinding,
    phase: "statement_review",
    source: { kind: "statement", mode: input.statementMode },
    status: "complete",
    title:
      input.statementMode === "voice"
        ? "Voice memo transcribed and reviewed"
        : "Customer statement reviewed",
  });

  const checksByField = new Map(
    output.trace.fieldChecks.map((check) => [check.field, check]),
  );

  canonicalFields.forEach((field) => {
    const check = checksByField.get(field);
    if (!check) return;

    addEvent({
      detail: check.detail,
      phase: "completeness",
      source: sourceFromProvider(check.sources[0] ?? "not_provided", input),
      status: check.status === "confirmed" ? "complete" : "attention",
      title: `${fieldLabels[field]}: ${check.status === "confirmed" ? "confirmed" : "needs attention"}`,
    });
  });

  const missingReadyFields =
    output.result.status === "ready"
      ? getMissingClaimDetailFields(output.result.claim)
      : [];
  const decisionTitle =
    output.result.status === "needs_information"
      ? "Decision: Ask for one missing detail"
      : missingReadyFields.length > 0
        ? "Decision: Prepare for customer review"
        : "Decision: Prepare the claim";

  addEvent({
    detail: output.trace.decisionDetail,
    phase: "decision",
    source: { kind: "system" },
    status:
      output.result.status === "needs_information" || missingReadyFields.length > 0
        ? "attention"
        : "complete",
    title: decisionTitle,
  });

  return AgentActivitySchema.parse({ events });
}

export class OpenAIClaimAnalyzer implements ClaimAnalyzer {
  constructor(private readonly client: OpenAI) {}

  async transcribe(audio: File): Promise<string> {
    const transcription = await this.client.audio.transcriptions.create({
      file: audio,
      model: TRANSCRIPTION_MODEL,
    });

    return transcription.text.trim();
  }

  async analyze(input: AnalysisInput) {
    const images = await Promise.all(
      input.photos.map(async (photo) => ({
        detail: "high" as const,
        image_url: await toDataUrl(photo),
        type: "input_image" as const,
      })),
    );
    const response = await this.client.responses.parse({
      input: [
        { content: systemPrompt, role: "system" },
        {
          content: [
            { text: buildStatement(input), type: "input_text" },
            ...images,
          ],
          role: "user",
        },
      ],
      model: ANALYSIS_MODEL,
      text: {
        format: zodTextFormat(ProviderOutputSchema, "claim_analysis"),
      },
    });
    const parsed = ProviderOutputSchema.safeParse(response.output_parsed);

    if (!parsed.success) {
      throw new Error("The provider returned an invalid claim result");
    }

    assertTraceIntegrity(parsed.data, input);

    return {
      activity: buildActivity(parsed.data, input),
      result: parsed.data.result,
    };
  }
}

export function createOpenAIClaimAnalyzer(): ClaimAnalyzer {
  const apiKey = process.env.OPENAI_API_KEY?.trim();

  if (!apiKey) {
    throw new AnalyzerNotConfiguredError();
  }

  return new OpenAIClaimAnalyzer(new OpenAI({ apiKey }));
}
