import type { Metadata } from "next";

import {
  ArrowRightIcon,
  ButtonLink,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  GateBadge,
  HumanBoundaryCard,
  PageShell,
  ProvenanceChip,
} from "../../components/ui";

export const metadata: Metadata = {
  title: "Prepare a safer claim draft",
};

const stages = [
  {
    number: "01",
    title: "Share evidence",
    description: "Add exactly three staged images and a written or audio statement.",
  },
  {
    number: "02",
    title: "Keep facts traceable",
    description: "Every usable detail stays linked to the source that supports it.",
  },
  {
    number: "03",
    title: "Stop at verified review",
    description: "Deterministic checks must pass before a human can review the draft.",
  },
] as const;

export default function HomePage() {
  return (
    <PageShell
      aside={
        <div className="stack stack--medium">
          <HumanBoundaryCard />
          <Card tone="soft">
            <CardHeader>
              <CardTitle>Visible safety gates</CardTitle>
              <CardDescription>Deterministic checks stay in charge.</CardDescription>
            </CardHeader>
            <CardContent className="badge-stack">
              <GateBadge gateId="G0" label="Intake" status="pending" />
              <GateBadge gateId="G1" label="Privacy" status="pending" />
              <GateBadge gateId="G8" label="Verification" status="pending" />
            </CardContent>
          </Card>
        </div>
      }
      description="Turn staged evidence into a traceable, review-ready sandbox draft—without giving the agent submission authority."
      eyebrow="Evidence to review, with a hard human boundary"
      title="A calmer path through claim preparation"
    >
      <div className="hero-panel">
        <div>
          <p className="hero-panel__kicker">Built for clarity under pressure</p>
          <h2>Know what the system used, what it could not confirm, and why it stopped.</h2>
          <p>
            ClaimDone separates evidence, model assistance, deterministic gates, and human approval so the next step always stays understandable.
          </p>
        </div>
        <div className="hero-panel__actions">
          <ButtonLink href="/claim/new" leadingIcon={<ArrowRightIcon />}>
            Start sandbox intake
          </ButtonLink>
          <ButtonLink href="/components" variant="secondary">
            View UI states
          </ButtonLink>
        </div>
      </div>

      <section aria-labelledby="how-it-works" className="section-block">
        <div className="section-heading">
          <p className="section-heading__eyebrow">How it works</p>
          <h2 id="how-it-works">Three deliberate stages, no hidden submission</h2>
        </div>
        <div className="stage-grid">
          {stages.map((stage) => (
            <Card key={stage.number}>
              <CardHeader>
                <span className="stage-number">{stage.number}</span>
                <CardTitle>{stage.title}</CardTitle>
                <CardDescription>{stage.description}</CardDescription>
              </CardHeader>
            </Card>
          ))}
        </div>
      </section>

      <section aria-labelledby="traceability" className="section-block">
        <div className="section-heading">
          <p className="section-heading__eyebrow">Traceability at a glance</p>
          <h2 id="traceability">Confidence is shown in words and numbers</h2>
        </div>
        <Card tone="soft">
          <CardContent className="provenance-demo">
            <div>
              <p className="demo-value">Rear bumper damage visible</p>
              <p className="demo-label">Synthetic example—no customer or insurer data</p>
            </div>
            <ProvenanceChip confidence={0.94} source="Image 2" status="observed" />
          </CardContent>
        </Card>
      </section>
    </PageShell>
  );
}
