import type { Metadata } from "next";

import {
  Alert,
  Button,
  ButtonLink,
  Card,
  CardContent,
  CheckboxField,
  GateBadge,
  HumanBoundaryCard,
  MismatchBoundaryNotice,
  PageShell,
  ProvenanceChip,
  StateView,
  Stepper,
  TextArea,
  TextInput,
} from "../../../components/ui";

export const metadata: Metadata = {
  robots: { index: false },
  title: "Component showcase",
};

const showcaseSteps = [
  { id: "disclosure", label: "Disclosure", description: "Understand the boundary" },
  { id: "intake", label: "Intake", description: "Add staged evidence" },
  { id: "review", label: "Review", description: "Human action only" },
] as const;

export default function ComponentsPage() {
  return (
    <PageShell
      aside={
        <div className="stack stack--medium">
          <HumanBoundaryCard />
          <MismatchBoundaryNotice />
        </div>
      }
      description="A practical reference for accessible states, evidence semantics, and product controls."
      eyebrow="FE-001 · Design system"
      title="ClaimDone component showcase"
    >
      <section aria-labelledby="tokens" className="showcase-section">
        <div className="section-heading">
          <p className="section-heading__eyebrow">Foundations</p>
          <h2 id="tokens">Color and shape tokens</h2>
        </div>
        <div className="token-grid">
          <div className="token-card">
            <span className="token-swatch token-swatch--navy" />
            <strong>Navy</strong>
            <code>#071A2B</code>
          </div>
          <div className="token-card">
            <span className="token-swatch token-swatch--teal" />
            <strong>Dark teal</strong>
            <code>#0F766E</code>
          </div>
          <div className="token-card">
            <span className="token-swatch token-swatch--accent" />
            <strong>Accent</strong>
            <code>#14B8A6</code>
          </div>
          <div className="token-card">
            <span className="token-swatch token-swatch--surface" />
            <strong>Surface</strong>
            <code>#F6F8F8</code>
          </div>
        </div>
      </section>

      <section aria-labelledby="controls" className="showcase-section">
        <div className="section-heading">
          <p className="section-heading__eyebrow">Controls</p>
          <h2 id="controls">Buttons and form fields</h2>
        </div>
        <Card>
          <CardContent className="stack stack--large">
            <div className="button-row">
              <Button>Primary action</Button>
              <Button variant="secondary">Secondary</Button>
              <Button variant="ghost">Quiet action</Button>
              <Button disabled>Unavailable</Button>
              <Button isLoading>Continue</Button>
            </div>
            <div className="form-grid">
              <TextInput
                description="Use staged details only."
                id="demo-reference"
                label="Demo reference"
                placeholder="DEMO-42"
              />
              <TextInput
                defaultValue="Mismatch"
                error="This value could not be verified."
                id="demo-error"
                label="Rendered value"
              />
              <TextArea
                defaultValue="I was stopped when the other vehicle made contact."
                description="Input is preserved exactly as entered."
                id="demo-statement"
                label="Statement"
                rows={4}
              />
              <CheckboxField
                description="Required before local processing begins."
                id="demo-consent"
                label="I understand this is a sandbox"
              />
            </div>
          </CardContent>
        </Card>
      </section>

      <section aria-labelledby="feedback" className="showcase-section">
        <div className="section-heading">
          <p className="section-heading__eyebrow">System feedback</p>
          <h2 id="feedback">Alerts, gates, and provenance</h2>
        </div>
        <div className="stack stack--medium">
          <Alert title="Evidence stays local until G0 and G1 pass" tone="info">
            Image type, count, consent, and privacy choices are checked before analysis.
          </Alert>
          <Alert title="Local checks passed" tone="success">
            The staged intake can move to analysis. Nothing has been submitted.
          </Alert>
          <MismatchBoundaryNotice />
          <div className="badge-row">
            <GateBadge gateId="G0" label="Intake" status="passed" />
            <GateBadge gateId="G1" label="Privacy" status="pending" />
            <GateBadge
              gateId="G8"
              label="Verification"
              reason="Rendered location differs"
              status="blocked"
            />
          </div>
          <div className="badge-row">
            <ProvenanceChip confidence={0.94} source="Image 2" status="observed" />
            <ProvenanceChip source="Written statement" status="user_stated" />
            <ProvenanceChip source="No supporting evidence" status="unknown" />
          </div>
        </div>
      </section>

      <section aria-labelledby="progress" className="showcase-section">
        <div className="section-heading">
          <p className="section-heading__eyebrow">Progress</p>
          <h2 id="progress">Stepper</h2>
        </div>
        <Card>
          <CardContent>
            <Stepper currentIndex={1} steps={showcaseSteps} />
          </CardContent>
        </Card>
      </section>

      <section aria-labelledby="states" className="showcase-section">
        <div className="section-heading">
          <p className="section-heading__eyebrow">Core state views</p>
          <h2 id="states">Empty, loading, error, blocked, and success</h2>
        </div>
        <div className="state-grid">
          <StateView
            description="Add staged evidence to begin."
            title="No evidence yet"
            variant="empty"
          />
          <StateView
            description="Checking image signatures and privacy choices."
            title="Checking intake"
            variant="loading"
          />
          <StateView
            action={<Button variant="secondary">Try again</Button>}
            description="The local check could not finish. Your files were not sent."
            title="Check failed"
            variant="error"
          />
          <StateView
            description="A deterministic mismatch prevents review from opening."
            title="Review blocked"
            variant="blocked"
          />
          <StateView
            action={
              <ButtonLink href="/claim/new" variant="secondary">
                Open intake
              </ButtonLink>
            }
            description="All required local checks passed. Human approval is still required later."
            title="Ready for analysis"
            variant="success"
          />
        </div>
      </section>
    </PageShell>
  );
}
