import type { AgentActivity } from "@/lib/demo-analysis-schema";
import type { ComputerUseReplay } from "@/lib/portal-handoff-schema";
import { PORTAL_FIELDS } from "@/lib/portal-field-values";

export const demoActivity: AgentActivity = {
  events: [
    {
      detail: "Three photos and a written description were received.",
      phase: "evidence",
      sequence: 0,
      source: { kind: "system" },
      status: "complete",
      title: "Evidence received",
    },
    {
      detail: "The intersection and both vehicles are visible.",
      phase: "image_review",
      sequence: 1,
      source: { kind: "photo", photoIndex: 1 },
      status: "complete",
      title: "Photo 1 reviewed",
    },
    {
      detail: "Front-left bumper damage is visible.",
      phase: "image_review",
      sequence: 2,
      source: { kind: "photo", photoIndex: 2 },
      status: "complete",
      title: "Photo 2 reviewed",
    },
    {
      detail: "The description supplies the collision sequence and location.",
      phase: "statement_review",
      sequence: 3,
      source: { kind: "statement", mode: "text" },
      status: "complete",
      title: "Customer statement reviewed",
    },
    {
      detail: "The evidence supports the four required claim details.",
      phase: "completeness",
      sequence: 4,
      source: { kind: "system" },
      status: "complete",
      title: "Claim details confirmed",
    },
    {
      detail: "The structured claim can be prepared for customer review.",
      phase: "decision",
      sequence: 5,
      source: { kind: "system" },
      status: "complete",
      title: "Decision: Prepare the claim",
    },
  ],
};

export function createDemoReplay(
  screenshotDataUrl = "data:image/png;base64,cG5n",
): ComputerUseReplay {
  return {
    finalState: "stopped_before_submission",
    kind: "captured_run",
    steps: [
      {
        kind: "opened",
        screenshotDataUrl,
        sequence: 0,
      },
      {
        destination: "claims",
        kind: "navigated",
        screenshotDataUrl,
        sequence: 1,
      },
      {
        destination: "incident_claim",
        kind: "navigated",
        screenshotDataUrl,
        sequence: 2,
      },
      ...PORTAL_FIELDS.map((field, index) => ({
        field,
        kind: "field_filled" as const,
        screenshotDataUrl,
        sequence: index + 3,
      })),
      {
        kind: "verified",
        screenshotDataUrl,
        sequence: 8,
      },
    ],
  };
}
