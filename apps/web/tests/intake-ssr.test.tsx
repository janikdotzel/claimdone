import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import {
  ClarificationCard,
  DemoAnalysisNotice,
  IntakeFlow,
} from "../src/features/intake/intake-flow";

describe("intake evidence SSR surface", () => {
  it("starts directly with evidence and keeps the demo acknowledgement on the claim action", () => {
    const html = renderToStaticMarkup(<IntakeFlow />);

    expect(html).toContain("Add three photos of the accident");
    expect(html).toContain("Add a short text or voice memo");
    expect(html).toContain("Your evidence stays traceable");
    expect(html).toContain("keeps each detail connected");
    expect(html).toContain("does not call an external provider");
    expect(html).toContain(
      "By selecting Create my claim, you confirm that you may use these staged photos",
    );
    expect(html).toContain("Nothing is submitted to an insurer");
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>.*Create my claim/s);
    expect(html).toContain('aria-current="step"');
    expect(html).toContain("Approval stays with you");
    expect(html).not.toContain("Before you begin");
    expect(html).not.toContain("Confirm the evidence is ready to check");
    expect(html).not.toContain('type="checkbox"');

    const ids = [...html.matchAll(/\sid="([^"]+)"/g)].map((match) => match[1]);
    expect(new Set(ids).size).toBe(ids.length);
    const labelTargets = [...html.matchAll(/\sfor="([^"]+)"/g)].map(
      (match) => match[1],
    );
    expect(labelTargets.every((target) => ids.includes(target))).toBe(true);
  });

  it("renders the demo-analysis notice as an accessible non-urgent note", () => {
    const html = renderToStaticMarkup(<DemoAnalysisNotice />);

    expect(html).toContain('role="note"');
    expect(html).toContain("keeps each detail connected");
    expect(html).toContain("photo or statement");
    expect(html).toContain("does not call an external provider");
  });

  it("keeps an authoritative consent error visible without restoring the checkbox card", () => {
    const html = renderToStaticMarkup(
      <IntakeFlow
        backendErrors={[
          {
            field: "consents.dataProcessing",
            message: "The server could not confirm local demo processing.",
          },
        ]}
      />,
    );

    expect(html).toContain("ClaimDone could not confirm the demo permissions:");
    expect(html).toContain("The server could not confirm local demo processing.");
    expect(html).not.toContain("Confirm the evidence is ready to check");
  });

  it("renders exactly one keyboard-native, labelled clarification question", () => {
    const question = "What time did the staged incident happen?";
    const html = renderToStaticMarkup(
      <ClarificationCard
        busy={false}
        clarification={{
          clarificationId: "clarification-001",
          expectedVersion: 4,
          field: "incident_time",
          question,
        }}
        error="Enter a valid time."
        onAnswerChange={() => undefined}
        onReset={() => undefined}
        onSubmit={(event) => event.preventDefault()}
        resetting={false}
        value=""
      />,
    );

    expect(html.match(new RegExp(question, "g"))).toHaveLength(1);
    expect(html).toContain("<form");
    expect(html).toContain('type="time"');
    expect(html).toContain('id="clarification-incident-time"');
    expect(html).toContain('for="clarification-incident-time"');
    expect(html).toContain('aria-invalid="true"');
    expect(html).toContain('type="submit"');
    expect(html).toContain("24-hour time including seconds");
    expect(html).toContain("14:30:00");
    expect(html).toContain('step="1"');
    expect(html).not.toContain("full deterministic G0–G5 rerun");
  });
});
