import { readdirSync, readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import SandboxIndexPage from "../src/app/sandbox/page";
import {
  PortalStateView,
  SandboxPortalClient,
} from "../src/features/sandbox/portal-client";
import { COMPLETE_PORTAL_FIELDS } from "../src/features/sandbox/fixtures";

const capabilityPattern = /\bcdcap_(?:a_[A-Za-z0-9_-]{43}|h_[ab]_[A-Za-z0-9_-]{43})\b/;

function browserShippedSandboxSource(): string {
  const featureDirectory = new URL("../src/features/sandbox/", import.meta.url);
  const featureSources = readdirSync(featureDirectory, { withFileTypes: true })
    .filter(
      (entry) =>
        entry.isFile() && (entry.name.endsWith(".ts") || entry.name.endsWith(".tsx")),
    )
    .map((entry) => readFileSync(new URL(entry.name, featureDirectory), "utf8"));
  const routeSource = readFileSync(
    new URL("../src/app/sandbox/[variant]/cases/[caseId]/page.tsx", import.meta.url),
    "utf8",
  );
  return [...featureSources, routeSource].join("\n");
}

describe("sandbox main landmarks", () => {
  it("gives the sandbox index an unambiguous skip-link target", () => {
    const html = renderToStaticMarkup(<SandboxIndexPage />);

    expect(html.match(/id="main-content"/g)).toHaveLength(1);
    expect(html).toContain('<main class=');
  });

  it("gives a sandbox case an unambiguous skip-link target", () => {
    const html = renderToStaticMarkup(
      <SandboxPortalClient caseId="case-a11y-001" variant="A" />,
    );

    expect(html.match(/id="main-content"/g)).toHaveLength(1);
    expect(html).toContain('<main class=');
  });

  it("keeps human approval credentials and actions out of the agent review DOM", () => {
    const html = renderToStaticMarkup(
      <PortalStateView fields={COMPLETE_PORTAL_FIELDS} state="review" />,
    );

    expect(html).not.toMatch(capabilityPattern);
    expect(html).not.toContain("human-approve");
    expect(html.toLowerCase()).not.toContain("authorization");
    expect(html).not.toContain("<form");
    expect(html).toContain("Human approval required in a separate context");
    expect(html).toContain("disabled");
  });

  it("keeps approval transport out of browser-shipped sandbox source", () => {
    const source = browserShippedSandboxSource();

    expect(source).not.toMatch(capabilityPattern);
    expect(source).not.toContain("human-approve");
    expect(source).not.toMatch(/\bauthorization\b/i);
  });
});
