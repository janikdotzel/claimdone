import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import {
  clarificationIdentityKey,
  confidenceLabel,
  WorkflowExperience,
} from "../src/features/workflow/components";
import {
  BLOCKED_SNAPSHOT,
  CLARIFICATION_SNAPSHOT,
  EMERGENCY_SNAPSHOT,
  G8_BLOCKED_SNAPSHOT,
  RECEIPT_SNAPSHOT,
  REPAIR_SNAPSHOT,
  REVIEW_SNAPSHOT,
  SHOWCASE_EVENTS,
  VERIFYING_SNAPSHOT,
} from "../src/features/workflow/fixtures";
import {
  INITIAL_WORKFLOW_EVENT_STORE,
  reduceWorkflowEventStore,
} from "../src/features/workflow/store";

const EVENT_SUMMARIES = SHOWCASE_EVENTS.reduce(
  (store, envelope) =>
    reduceWorkflowEventStore(store, { envelope, type: "EVENT_RECEIVED" }),
  INITIAL_WORKFLOW_EVENT_STORE,
).events;

describe("accessible workflow experience", () => {
  it("keeps verifying distinct from canonical human review", () => {
    const verifying = renderToStaticMarkup(
      <WorkflowExperience
        events={EVENT_SUMMARIES}
        mode="ready"
        snapshot={VERIFYING_SNAPSHOT}
      />,
    );
    expect(verifying).toContain("Verification in progress");
    expect(verifying).not.toContain("Verified review ready");
    expect(verifying).not.toContain("Ready for human review");

    const review = renderToStaticMarkup(
      <WorkflowExperience
        events={EVENT_SUMMARIES}
        mode="ready"
        snapshot={REVIEW_SNAPSHOT}
      />,
    );
    expect(review).toContain("Verified review ready");
  });

  it("renders the permanent sandbox and human boundary without an approval button", () => {
    for (const snapshot of [VERIFYING_SNAPSHOT, BLOCKED_SNAPSHOT, EMERGENCY_SNAPSHOT]) {
      const html = renderToStaticMarkup(
        <WorkflowExperience mode="ready" snapshot={snapshot} />,
      );
      expect(html).toContain('role="note"');
      expect(html).toContain("Sandbox only");
      expect(html).toContain("Not submitted / human approval required");
      expect(html).not.toContain("Approve claim");
      expect(html).not.toContain("Submit claim");
      if (snapshot.case.state === "blocked") {
        expect(html).toContain("G5: A required claim field is missing");
        expect(html).toContain("Evidence board");
      }
      if (snapshot.case.state === "emergency_stopped") {
        expect(html).toContain("Emergency stop activated");
        expect(html).toContain("G3: An injury or emergency is outside this demo scope");
      }
    }
  });

  it("shows exactly one accessible clarification and keeps the transport disconnected", () => {
    const html = renderToStaticMarkup(
      <WorkflowExperience mode="ready" snapshot={CLARIFICATION_SNAPSHOT} />,
    );
    expect(html.match(/One active clarification/g)).toHaveLength(1);
    expect(html).toContain('<label for="clarification-001"');
    expect(html).toContain('id="clarification-001"');
    expect(html).toContain("Whitespace is preserved exactly");
    expect(html).toContain("Command transport is not connected");
    expect(html).toContain("disabled");
  });

  it("derives a new remount key for every clarification command identity", () => {
    const clarification = CLARIFICATION_SNAPSHOT.clarification;
    if (clarification === null) throw new Error("Clarification fixture is missing");
    const variants = [
      { ...clarification, caseId: "case-happy-002" },
      { ...clarification, clarificationId: "clarification-002" },
      { ...clarification, expectedVersion: clarification.expectedVersion + 1 },
      { ...clarification, field: "location" as const },
      { ...clarification, round: 2 as const },
    ];
    const keys = [
      clarificationIdentityKey(clarification),
      ...variants.map((variant) => clarificationIdentityKey(variant)),
    ];
    expect(new Set(keys).size).toBe(keys.length);
  });

  it("renders safe evidence labels and a structural read-only form without raw values", () => {
    const html = renderToStaticMarkup(
      <WorkflowExperience
        events={EVENT_SUMMARIES}
        mode="ready"
        snapshot={REVIEW_SNAPSHOT}
      />,
    );
    expect(html).toContain("Evidence board");
    expect(html).toContain("Visible agent plan");
    expect(html).toContain("The plan stops at review");
    expect(html).toContain("Staged image 2");
    expect(html).toContain("User statement");
    expect(html).toContain("Read-only sandbox form structure");
    expect(html).toContain("Value withheld");
    expect(html).toContain('aria-label="Field verification table"');
    expect(html).not.toContain("prov-image-2");
    expect(html).not.toContain("Demo Claimant");
    expect(html).not.toContain("DEMO-42");
    expect(html).not.toContain("DEMO-CD-1");
    expect(html).not.toContain("Inspect approved staged evidence");
  });

  it("does not claim that an unverified portal draft is populated or verified", () => {
    const html = renderToStaticMarkup(
      <WorkflowExperience mode="ready" snapshot={VERIFYING_SNAPSHOT} />,
    );
    expect(html).toContain("8 scalar slots · up to 3 staged attachments");
    expect(html).toContain("Value withheld · verification status shown below");
    expect(html).toContain("Up to 3 staged attachments · identifiers withheld");
    expect(html).not.toContain("8 required fields + 3 staged attachments");
    expect(html).not.toContain("Populated from verified packet");
    expect(html).not.toContain("<output>3 staged attachments · identifiers withheld</output>");
  });

  it("explains one bounded repair without exposing identifiers or field values", () => {
    const repair = renderToStaticMarkup(
      <WorkflowExperience mode="ready" snapshot={REPAIR_SNAPSHOT} />,
    );
    expect(repair).toContain("Attempt 1");
    expect(repair).toContain("Attempt 2");
    expect(repair).toContain("One narrow repair authorized: Incident location");
    expect(repair).toContain("Authorized repair used: Incident location");
    expect(repair).toContain("User statement");
    expect(repair).not.toContain("verification-repairable");
    expect(repair).not.toContain("Different staged location");
    expect(repair).not.toContain("prov-statement");
  });

  it("shows a final deterministic G8 mismatch without inventing repair authority", () => {
    const blocked = renderToStaticMarkup(
      <WorkflowExperience mode="ready" snapshot={G8_BLOCKED_SNAPSHOT} />,
    );
    expect(blocked).toContain("G8: A rendered field differs from the claim packet");
    expect(blocked).toContain("Affected fields: Incident location (mismatch)");
    expect(blocked).toContain("No repair was authorized");
    expect(blocked).toContain('aria-label="Field verification table"');
    expect(blocked).not.toContain("Different staged location");
    expect(blocked).not.toContain("prov-statement");
  });

  it("labels 0.79 as uncertain and 0.80 as meeting the deterministic threshold", () => {
    expect(confidenceLabel(0.79, "observed")).toBe(
      "Uncertain · 79% · below deterministic 80% threshold",
    );
    expect(confidenceLabel(0.8, "observed")).toBe(
      "Meets deterministic threshold · 80%",
    );
  });

  it("renders a redacted-only receipt and all generic UI states", () => {
    const receipt = renderToStaticMarkup(
      <WorkflowExperience mode="ready" snapshot={RECEIPT_SNAPSHOT} />,
    );
    expect(receipt).toContain("Redacted sandbox receipt");
    expect(receipt).toContain("Real insurer submission");
    expect(receipt).not.toContain("Demo Claimant");
    expect(receipt).not.toContain("Berlin");
    expect(receipt).not.toContain("DEMO-42");

    const modes = [
      renderToStaticMarkup(<WorkflowExperience mode="loading" />),
      renderToStaticMarkup(<WorkflowExperience mode="empty" />),
      renderToStaticMarkup(
        <WorkflowExperience errorMessage="Safe connection error" mode="error" />,
      ),
      renderToStaticMarkup(
        <WorkflowExperience mode="ready" snapshot={BLOCKED_SNAPSHOT} />,
      ),
    ];
    expect(modes[0]).toContain('role="status"');
    expect(modes[1]).toContain("No sandbox case selected");
    expect(modes[2]).toContain('role="alert"');
    expect(modes[3]).toContain("Workflow blocked");
  });
});
