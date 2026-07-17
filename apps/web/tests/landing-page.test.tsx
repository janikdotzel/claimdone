import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { LandingClaimDemo } from "../src/app/landing-claim-demo";
import HomePage from "../src/app/page";

describe("ClaimDone landing page", () => {
  it("presents the evidence-to-claim promise without the shared product chrome", () => {
    const html = renderToStaticMarkup(<HomePage />);

    expect(html).toContain("Three photos. One short statement.");
    expect(html).toContain("Your claim is ready to review.");
    expect(html).toContain("Overview");
    expect(html).toContain("Damage");
    expect(html).toContain("Context");
    expect(html).toContain("Read photos");
    expect(html).toContain("Complete claim");
    expect(html).toContain("Ready to review");
    expect(html).toContain("Create insurance claim");
    expect(html).toContain('id="how-it-works"');
    expect(html).not.toContain('href="#safety"');
    expect(html).not.toContain('id="safety"');
    expect(html).not.toContain('href="#questions"');
    expect(html).not.toContain('id="questions"');
    expect(html).not.toContain("Only the essentials in");
    expect(html).not.toContain("Prepared automatically");
    expect(html).not.toContain("The questions that matter after an accident");
    expect(html).not.toContain("Sandbox only");
    expect(html).not.toContain("Start sandbox claim");
  });

  it("renders accessible Memo/Text controls and a complete initial claim", () => {
    const html = renderToStaticMarkup(<LandingClaimDemo />);

    expect(html).toContain('aria-label="Statement format"');
    expect(html).toMatch(/<button[^>]*aria-pressed="true"[^>]*>.*Memo/s);
    expect(html).toMatch(/<button[^>]*aria-pressed="false"[^>]*>.*Text/s);
    expect(html).toContain('for="landing-claim-statement"');
    expect(html).toContain('id="landing-claim-statement"');
    expect(html).toContain('role="progressbar"');
    expect(html).toContain('aria-valuenow="100"');
    expect(html).toMatch(
      /aria-label="Statement format".*Create insurance claim.*INSURANCE CLAIM/s,
    );
    expect(html).toContain("Created from 3 photos +");
    expect(html).toContain("voice memo");
    expect(html).toContain("No data is submitted.");
  });
});
