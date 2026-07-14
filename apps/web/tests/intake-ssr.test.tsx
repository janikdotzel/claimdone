import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { IntakeFlow } from "../src/features/intake/intake-flow";

describe("intake disclosure SSR surface", () => {
  it("renders a separate disclosure with a keyboard-operable gated continuation", () => {
    const html = renderToStaticMarkup(<IntakeFlow />);

    expect(html).toContain("Step 1 · Disclosure");
    expect(html).toContain("Before you add any evidence");
    expect(html).toContain("This is a local sandbox");
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
});
