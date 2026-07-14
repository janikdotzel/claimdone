import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import {
  ClarificationCard,
  IntakeFlow,
  MockDraftNotice,
} from "../src/features/intake/intake-flow";

describe("intake disclosure SSR surface", () => {
  it("renders a separate disclosure with a keyboard-operable gated continuation", () => {
    const html = renderToStaticMarkup(<IntakeFlow />);

    expect(html).toContain("Step 1 · Disclosure");
    expect(html).toContain("Before you add any evidence");
    expect(html).toContain("This is a local sandbox");
    expect(html).toContain("No live AI extraction in this walking skeleton");
    expect(html).toContain("fixed, versioned synthetic fixture draft");
    expect(html).toContain("not claim facts inferred from your text");
    expect(html).toContain('id="disclosure-acknowledgement"');
    expect(html).toContain('for="disclosure-acknowledgement"');
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>.*Continue to intake/s);
    expect(html).toContain('aria-current="step"');
    expect(html).toContain("Approval stays with you");

    const ids = [...html.matchAll(/\sid="([^"]+)"/g)].map((match) => match[1]);
    expect(new Set(ids).size).toBe(ids.length);
    const labelTargets = [...html.matchAll(/\sfor="([^"]+)"/g)].map(
      (match) => match[1],
    );
    expect(labelTargets.every((target) => ids.includes(target))).toBe(true);
  });

  it("renders the mock-draft provenance notice as an accessible non-urgent note", () => {
    const html = renderToStaticMarkup(<MockDraftNotice />);

    expect(html).toContain('role="note"');
    expect(html).toContain("retained as evidence and safety-checked");
    expect(html).toContain("fixed, versioned synthetic fixture draft");
    expect(html).toContain("not claim facts inferred from your text");
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
    expect(html).toContain("Press Enter to continue");
  });
});
