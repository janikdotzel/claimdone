import { z } from "zod";

import { ClaimSchema } from "./analysis-schema";
import { PORTAL_FIELDS } from "./portal-field-values";

const PngDataUrlSchema = z
  .string()
  .max(8_000_000)
  .regex(/^data:image\/png;base64,[A-Za-z0-9+/]+={0,2}$/);

export const PortalHandoffRequestSchema = z
  .object({
    claim: ClaimSchema,
  })
  .strict();

export const PortalHandoffSuccessSchema = z
  .object({
    screenshotDataUrl: PngDataUrlSchema,
    status: z.literal("prepared"),
    submitted: z.literal(false),
  })
  .strict();

const ReplaySequenceSchema = z.number().int().min(0).max(8);
const PortalFieldSchema = z.enum(PORTAL_FIELDS);

export const ComputerUseReplayStepSchema = z.discriminatedUnion("kind", [
  z
    .object({
      kind: z.literal("opened"),
      screenshotDataUrl: PngDataUrlSchema,
      sequence: ReplaySequenceSchema,
    })
    .strict(),
  z
    .object({
      destination: z.enum(["claims", "incident_claim"]),
      kind: z.literal("navigated"),
      screenshotDataUrl: PngDataUrlSchema,
      sequence: ReplaySequenceSchema,
    })
    .strict(),
  z
    .object({
      field: PortalFieldSchema,
      kind: z.literal("field_filled"),
      screenshotDataUrl: PngDataUrlSchema,
      sequence: ReplaySequenceSchema,
    })
    .strict(),
  z
    .object({
      kind: z.literal("verified"),
      screenshotDataUrl: PngDataUrlSchema,
      sequence: ReplaySequenceSchema,
    })
    .strict(),
]);

export const ComputerUseReplaySchema = z
  .object({
    finalState: z.literal("stopped_before_submission"),
    kind: z.literal("captured_run"),
    steps: z.array(ComputerUseReplayStepSchema).min(9).max(9),
  })
  .strict()
  .superRefine((replay, context) => {
    replay.steps.forEach((step, index) => {
      if (step.sequence !== index) {
        context.addIssue({
          code: "custom",
          message: "Replay sequences must be contiguous",
          path: ["steps", index, "sequence"],
        });
      }
    });

    if (replay.steps[0]?.kind !== "opened") {
      context.addIssue({
        code: "custom",
        message: "Replay must start with the opened sandbox",
        path: ["steps", 0],
      });
    }

    const claimsNavigation = replay.steps[1];
    if (
      claimsNavigation?.kind !== "navigated" ||
      claimsNavigation.destination !== "claims"
    ) {
      context.addIssue({
        code: "custom",
        message: "Replay must navigate from the portal home page to Claims",
        path: ["steps", 1],
      });
    }

    const incidentClaimNavigation = replay.steps[2];
    if (
      incidentClaimNavigation?.kind !== "navigated" ||
      incidentClaimNavigation.destination !== "incident_claim"
    ) {
      context.addIssue({
        code: "custom",
        message: "Replay must navigate from Claims to the incident claim form",
        path: ["steps", 2],
      });
    }

    if (replay.steps.at(-1)?.kind !== "verified") {
      context.addIssue({
        code: "custom",
        message: "Replay must end with verification",
        path: ["steps", replay.steps.length - 1],
      });
    }

    const filledFields = replay.steps.slice(3, -1).flatMap((step) =>
      step.kind === "field_filled" ? [step.field] : [],
    );
    const uniqueFields = new Set(filledFields);

    if (
      filledFields.length !== PORTAL_FIELDS.length ||
      uniqueFields.size !== PORTAL_FIELDS.length ||
      PORTAL_FIELDS.some((field) => !uniqueFields.has(field))
    ) {
      context.addIssue({
        code: "custom",
        message: "Replay must contain each approved field exactly once",
        path: ["steps"],
      });
    }
  });

export const DemoPortalHandoffSuccessSchema = z
  .object({
    replay: ComputerUseReplaySchema,
    screenshotDataUrl: PngDataUrlSchema,
    status: z.literal("prepared"),
    submitted: z.literal(false),
  })
  .strict()
  .superRefine((result, context) => {
    const verified = result.replay.steps.at(-1);

    if (
      verified?.kind === "verified" &&
      verified.screenshotDataUrl !== result.screenshotDataUrl
    ) {
      context.addIssue({
        code: "custom",
        message: "The final screenshot must match the verified replay frame",
        path: ["screenshotDataUrl"],
      });
    }
  });

export const PortalHandoffErrorCodeSchema = z.enum([
  "invalid_input",
  "not_configured",
  "safety_blocked",
  "automation_failed",
]);

export const PortalHandoffErrorSchema = z
  .object({
    error: z
      .object({
        code: PortalHandoffErrorCodeSchema,
        message: z.string().trim().min(1),
      })
      .strict(),
  })
  .strict();

export type PortalHandoffError = z.infer<typeof PortalHandoffErrorSchema>;
export type PortalHandoffErrorCode = z.infer<
  typeof PortalHandoffErrorCodeSchema
>;
export type PortalHandoffRequest = z.infer<typeof PortalHandoffRequestSchema>;
export type PortalHandoffSuccess = z.infer<typeof PortalHandoffSuccessSchema>;
export type ComputerUseReplay = z.infer<typeof ComputerUseReplaySchema>;
export type ComputerUseReplayStep = z.infer<
  typeof ComputerUseReplayStepSchema
>;
export type DemoPortalHandoffSuccess = z.infer<
  typeof DemoPortalHandoffSuccessSchema
>;
