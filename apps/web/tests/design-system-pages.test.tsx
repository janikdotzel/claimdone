import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import ErrorPage from "../src/app/error";
import Loading from "../src/app/loading";
import NotFound from "../src/app/not-found";
import HomePage from "../src/app/page";
import { ProductChrome } from "../src/components/ui/shell";
import { IntakeFlow } from "../src/features/intake/intake-flow";
import { WorkflowExperience } from "../src/features/workflow/components";
import { REVIEW_SNAPSHOT } from "../src/features/workflow/fixtures";

const germanCoreCopy =
  /Drei Fotos|Schadenmeldung|So funktioniert|Sicherheit|Was ist passiert|Bereit zur Prüfung/;

describe("shared ClaimDone page design", () => {
  it("keeps product navigation hash links connected to real landing sections", () => {
    const chrome = renderToStaticMarkup(
      <ProductChrome>
        <main id="main-content">Claim flow</main>
      </ProductChrome>,
    );
    const landing = renderToStaticMarkup(<HomePage />);
    const hashTargets = [...chrome.matchAll(/href="\/#([^"]+)"/g)].map(
      (match) => match[1],
    );

    expect(hashTargets).toEqual(["how-it-works"]);
    for (const target of hashTargets) {
      expect(landing).toContain(`id="${target}"`);
    }
    expect(chrome).toContain('href="/claim/new"');
  });

  it("renders the landing and intake core journey in English", () => {
    const landing = renderToStaticMarkup(<HomePage />);
    const intake = renderToStaticMarkup(<IntakeFlow />);

    expect(landing).toContain("Three photos. One short statement.");
    expect(landing).toContain("Your claim is ready to review.");
    expect(landing).toContain("Read photos");
    expect(landing).toContain("Complete claim");

    expect(intake).toContain("Three photos + one short statement");
    expect(intake).toContain("Build your claim in a few clear steps");
    expect(intake).toContain("Add evidence");
    expect(intake).toContain("Claim Agent");
    expect(intake).toContain("Review");
    expect(intake).not.toContain("Before you begin");
    expect(intake).not.toContain("Confirm the evidence is ready to check");

    expect(landing).not.toMatch(germanCoreCopy);
    expect(intake).not.toMatch(germanCoreCopy);
  });

  it("uses clear English loading, not-found, and error states with a main landmark", () => {
    const states = [
      {
        heading: "Preparing the next step",
        html: renderToStaticMarkup(<Loading />),
      },
      {
        heading: "This route is not part of your claim",
        html: renderToStaticMarkup(<NotFound />),
      },
      {
        heading: "This page could not finish loading",
        html: renderToStaticMarkup(<ErrorPage reset={() => undefined} />),
      },
    ];

    for (const { heading, html } of states) {
      expect(html.match(/id="main-content"/g)).toHaveLength(1);
      expect(html).toContain(`<h1>${heading}</h1>`);
      expect(html).not.toMatch(germanCoreCopy);
    }

    expect(states[0]?.html).toContain('aria-busy="true"');
    expect(states[1]?.html).toContain("Start a claim");
    expect(states[1]?.html).toContain("Return home");
    expect(states[2]?.html).toContain('role="alert"');
    expect(states[2]?.html).toContain("Try again");
  });

  it("shows a complete claim document and human boundary without a duplicate sandbox banner", () => {
    const withoutBanner = renderToStaticMarkup(
      <WorkflowExperience
        mode="ready"
        showSandboxBanner={false}
        snapshot={REVIEW_SNAPSHOT}
      />,
    );
    const withBanner = renderToStaticMarkup(
      <WorkflowExperience mode="ready" snapshot={REVIEW_SNAPSHOT} />,
    );

    expect(withoutBanner).toContain('id="prepared-claim-title"');
    expect(withoutBanner).toContain("Insurance claim");
    expect(withoutBanner).toContain("Your complete claim");
    expect(withoutBanner).toContain("Ready to review");
    expect(withoutBanner).toContain('aria-label="Claim completeness 100 percent"');
    expect(withoutBanner).toContain("Created from 3 photos + your statement");
    expect(withoutBanner).toMatch(/\d+ checks passed/);

    expect(withoutBanner).toContain('aria-label="Human approval boundary"');
    expect(withoutBanner).toContain("Not submitted / human approval required");
    expect(withoutBanner).not.toContain(
      "Sandbox only · Nothing is submitted to a real insurer",
    );
    expect(withBanner).toContain(
      "Sandbox only · Nothing is submitted to a real insurer",
    );

    for (const html of [withoutBanner, withBanner]) {
      expect(html).not.toContain("Approve claim");
      expect(html).not.toContain("Submit claim");
    }
  });
});
