import { z } from "zod";

export const StatementModeSchema = z.enum(["text", "voice"]);

export const MissingFieldSchema = z.enum([
  "damage",
  "date_time",
  "location",
  "what_happened",
]);

export const ClaimSchema = z
  .object({
    damage: z.string().trim().min(1).max(500),
    dateTime: z.string().trim().min(1).max(200),
    location: z.string().trim().min(1).max(500),
    whatHappened: z.string().trim().min(1).max(1500),
    photoCount: z.union([z.literal(1), z.literal(2), z.literal(3)]),
    status: z.literal("ready"),
  })
  .strict();

const ReadyResponseSchema = z
  .object({
    status: z.literal("ready"),
    claim: ClaimSchema,
  })
  .strict();

const NeedsInformationResponseSchema = z
  .object({
    status: z.literal("needs_information"),
    question: z
      .object({
        field: MissingFieldSchema,
        prompt: z.string().trim().min(1).max(300),
      })
      .strict(),
  })
  .strict();

export const AnalyzeResponseSchema = z.discriminatedUnion("status", [
  ReadyResponseSchema,
  NeedsInformationResponseSchema,
]);

export const AnalyzeErrorCodeSchema = z.enum([
  "invalid_input",
  "unsupported_media",
  "payload_too_large",
  "not_configured",
  "analysis_failed",
]);

export const AnalyzeErrorSchema = z
  .object({
    error: z
      .object({
        code: AnalyzeErrorCodeSchema,
        message: z.string().trim().min(1),
      })
      .strict(),
  })
  .strict();

export type AnalyzeError = z.infer<typeof AnalyzeErrorSchema>;
export type AnalyzeErrorCode = z.infer<typeof AnalyzeErrorCodeSchema>;
export type AnalyzeResponse = z.infer<typeof AnalyzeResponseSchema>;
export type Claim = z.infer<typeof ClaimSchema>;
export const CLAIM_DETAIL_FIELDS = [
  "damage",
  "dateTime",
  "location",
  "whatHappened",
] as const;
export type ClaimDetailField = (typeof CLAIM_DETAIL_FIELDS)[number];
export type MissingField = z.infer<typeof MissingFieldSchema>;
export type StatementMode = z.infer<typeof StatementModeSchema>;

export function isClaimDetailMissing(value: string): boolean {
  const normalizedValue = value.trim().toLowerCase();

  return normalizedValue.length === 0 || normalizedValue === "not provided";
}

export function getMissingClaimDetailFields(
  claim: Pick<Claim, ClaimDetailField>,
): ClaimDetailField[] {
  return CLAIM_DETAIL_FIELDS.filter((field) =>
    isClaimDetailMissing(claim[field]),
  );
}
