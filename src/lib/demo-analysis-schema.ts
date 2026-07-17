import { z } from "zod";

import {
  AnalyzeResponseSchema,
  MissingFieldSchema,
  StatementModeSchema,
} from "./analysis-schema";

const PhotoIndexSchema = z.union([
  z.literal(1),
  z.literal(2),
  z.literal(3),
]);

export const AgentActivitySourceSchema = z.discriminatedUnion("kind", [
  z
    .object({
      kind: z.literal("photo"),
      photoIndex: PhotoIndexSchema,
    })
    .strict(),
  z
    .object({
      kind: z.literal("statement"),
      mode: StatementModeSchema,
    })
    .strict(),
  z
    .object({
      field: MissingFieldSchema,
      kind: z.literal("follow_up"),
    })
    .strict(),
  z.object({ kind: z.literal("system") }).strict(),
]);

export const AgentActivityEventSchema = z
  .object({
    detail: z.string().trim().min(1).max(300),
    phase: z.enum([
      "evidence",
      "image_review",
      "statement_review",
      "completeness",
      "decision",
    ]),
    sequence: z.number().int().min(0).max(11),
    source: AgentActivitySourceSchema,
    status: z.enum(["complete", "attention"]),
    title: z.string().trim().min(1).max(80),
  })
  .strict();

export const AgentActivitySchema = z
  .object({
    events: z.array(AgentActivityEventSchema).min(4).max(12),
  })
  .strict()
  .superRefine((activity, context) => {
    activity.events.forEach((event, index) => {
      if (event.sequence !== index) {
        context.addIssue({
          code: "custom",
          message: "Activity event sequences must be contiguous",
          path: ["events", index, "sequence"],
        });
      }
    });
  });

export const DemoAnalyzeResponseSchema = z
  .object({
    activity: AgentActivitySchema,
    result: AnalyzeResponseSchema,
  })
  .strict();

export type AgentActivity = z.infer<typeof AgentActivitySchema>;
export type AgentActivityEvent = z.infer<typeof AgentActivityEventSchema>;
export type DemoAnalyzeResponse = z.infer<typeof DemoAnalyzeResponseSchema>;
