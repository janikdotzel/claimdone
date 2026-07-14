import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import SandboxIndexPage from "../src/app/sandbox/page";
import { SandboxPortalClient } from "../src/features/sandbox/portal-client";

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
});
