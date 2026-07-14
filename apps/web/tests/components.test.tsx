import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import {
  Alert,
  CheckboxField,
  GateBadge,
  ProductChrome,
  ProvenanceChip,
  StateView,
  Stepper,
  TextInput,
} from "../src/components/ui";

describe("accessible product primitives", () => {
  it("exposes the sandbox and human boundary as semantic chrome", () => {
    const html = renderToStaticMarkup(
      <ProductChrome>
        <main id="main-content">Content</main>
      </ProductChrome>,
    );

    expect(html).toContain("<header");
    expect(html).toContain('aria-label="Primary navigation"');
    expect(html).toContain('role="note"');
    expect(html).toContain("Sandbox only");
    expect(html).toContain("Human approval required");
    expect(html).toContain("Nothing is sent to an insurer");
  });

  it("associates field labels, descriptions, errors, and controls", () => {
    const html = renderToStaticMarkup(
      <>
        <TextInput
          description="Use staged details only."
          error="A reference is required."
          id="reference"
          label="Demo reference"
        />
        <CheckboxField
          description="Required before analysis."
          error="Consent is required."
          id="consent"
          label="I understand"
        />
      </>,
    );

    expect(html).toContain('for="reference"');
    expect(html).toContain('id="reference"');
    expect(html).toContain(
      'aria-describedby="reference-description reference-error"',
    );
    expect(html).toContain('aria-invalid="true"');
    expect(html).toContain('for="consent"');
    expect(html).toContain('type="checkbox"');
    expect(html.match(/role="alert"/g)).toHaveLength(2);
  });

  it("announces gate, provenance, progress, and failure semantics", () => {
    const html = renderToStaticMarkup(
      <>
        <Alert title="Review blocked" tone="blocked">
          Repair the mismatch.
        </Alert>
        <GateBadge
          gateId="G8"
          label="Verification"
          reason="Rendered value differs"
          status="blocked"
        />
        <ProvenanceChip confidence={0.94} source="Image 2" status="observed" />
        <Stepper
          currentIndex={1}
          steps={[
            { id: "one", label: "Disclosure" },
            { id: "two", label: "Intake" },
          ]}
        />
        <StateView
          description="A deterministic mismatch prevents review."
          title="Review blocked"
          variant="blocked"
        />
      </>,
    );

    expect(html).toContain('role="alert"');
    expect(html).toContain('aria-label="G8 Verification: Blocked. Rendered value differs"');
    expect(html).toContain("High confidence · 94%");
    expect(html).toContain('aria-current="step"');
    expect(html).toContain("A deterministic mismatch prevents review.");
  });
});
