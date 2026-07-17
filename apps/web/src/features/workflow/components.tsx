"use client";

import type { FormEvent, ReactNode } from "react";
import { useId, useState } from "react";

import type {
  ClarificationAnswerRequest,
  ClaimPacket,
  RequiredClaimField,
  WorkflowSnapshot,
} from "../../../../../contracts/generated/claimdone";

import { buildClarificationAnswerRequest } from "./api";
import { gateReasonLabel, type WorkflowEventSummary } from "./store";
import styles from "./workflow.module.css";

export interface WorkflowExperienceProps {
  readonly errorMessage?: string;
  readonly events?: readonly WorkflowEventSummary[];
  readonly mode: "loading" | "empty" | "error" | "ready";
  readonly onClarificationAnswer?: (
    request: ClarificationAnswerRequest,
  ) => void;
  readonly portalSurface?: ReactNode;
  readonly showSandboxBanner?: boolean;
  readonly snapshot?: WorkflowSnapshot;
}

export function WorkflowExperience({
  errorMessage,
  events = [],
  mode,
  onClarificationAnswer,
  portalSurface,
  showSandboxBanner = true,
  snapshot,
}: WorkflowExperienceProps) {
  let content: ReactNode;
  if (mode === "loading") {
    content = (
      <WorkflowState title="Loading workflow" tone="loading">
        The authoritative sandbox snapshot is being loaded.
      </WorkflowState>
    );
  } else if (mode === "empty") {
    content = (
      <WorkflowState title="No sandbox case selected" tone="empty">
        Start a staged claim to see its evidence and agent activity.
      </WorkflowState>
    );
  } else if (mode === "error" || snapshot === undefined) {
    content = (
      <WorkflowState title="Workflow unavailable" tone="error">
        {errorMessage ?? "The authoritative snapshot could not be verified."}
      </WorkflowState>
    );
  } else if (isStopped(snapshot.case.state)) {
    const failedGate = snapshot.claimPacket?.gateDecisions.findLast(
      (decision) => !decision.passed,
    );
    content = (
      <>
        <HumanApprovalBoundary state={snapshot.case.state} />
        <WorkflowState title={stoppedTitle(snapshot.case.state)} tone="blocked">
          <p>
            {failedGate === undefined
              ? "A redacted operational boundary stopped this sandbox run."
              : `${failedGate.gateId}: ${failedGate.reasonCodes
                  .map((reason) => gateReasonLabel(reason))
                  .join("; ")}`}
          </p>
        </WorkflowState>
        {snapshot.claimPacket === null ? null : (
          <>
            <EvidenceBoard packet={snapshot.claimPacket} />
            <section className={styles.panel} aria-label="Stopped verification detail">
              <FieldComparison packet={snapshot.claimPacket} />
              <VerificationAttemptsPanel
                attempts={snapshot.verificationAttempts}
                packet={snapshot.claimPacket}
              />
            </section>
          </>
        )}
        <AgentEventStrip events={events} />
      </>
    );
  } else if (snapshot.case.state === "receipt") {
    content = (
      <WorkflowState title="Sandbox run complete" tone="success">
        <ReceiptPanel snapshot={snapshot} />
      </WorkflowState>
    );
  } else {
    content = (
      <SplitViewShell
        events={events}
        snapshot={snapshot}
        {...(onClarificationAnswer === undefined
          ? {}
          : { onClarificationAnswer })}
        {...(portalSurface === undefined ? {} : { portalSurface })}
      />
    );
  }

  return (
    <section className={styles.experience} aria-label="ClaimDone workflow">
      {showSandboxBanner ? <SandboxBanner /> : null}
      <div className={styles.experienceBody} aria-live="polite">
        {content}
      </div>
    </section>
  );
}

export function SandboxBanner() {
  return (
    <div className={styles.sandboxBanner} role="note">
      <span aria-hidden="true" className={styles.sandboxDot} />
      Sandbox only · Nothing is submitted to a real insurer
    </div>
  );
}

export interface SplitViewShellProps {
  readonly events: readonly WorkflowEventSummary[];
  readonly onClarificationAnswer?: (
    request: ClarificationAnswerRequest,
  ) => void;
  readonly portalSurface?: ReactNode;
  readonly snapshot: WorkflowSnapshot;
}

export function SplitViewShell({
  events,
  onClarificationAnswer,
  portalSurface,
  snapshot,
}: SplitViewShellProps) {
  const state = snapshot.case.state;
  const portalHeadingId = useId();
  return (
    <div className={styles.shell}>
      <header className={styles.shellHeader}>
        <div>
          <p className={styles.eyebrow}>Your insurance claim</p>
          <h1>{stateTitle(state)}</h1>
        </div>
        <span className={styles.stateBadge}>{stateLabel(state)}</span>
      </header>

      {snapshot.claimPacket === null ? null : (
        <CompletedClaimDocument packet={snapshot.claimPacket} state={state} />
      )}

      <HumanApprovalBoundary state={state} />

      {snapshot.clarification !== null ? (
        <ClarificationPanel
          clarification={snapshot.clarification}
          key={clarificationIdentityKey(snapshot.clarification)}
          {...(onClarificationAnswer === undefined
            ? {}
            : { onAnswer: onClarificationAnswer })}
        />
      ) : null}

      <details className={styles.auditDetails} open={state !== "review"}>
        <summary>
          <span>
            <strong>See how ClaimDone checked this claim</strong>
            <small>Evidence, deterministic checks, and agent activity</small>
          </span>
          <span aria-hidden="true">+</span>
        </summary>
        <div className={styles.auditBody}>
          <DeterministicGateTrail snapshot={snapshot} />
          <div className={styles.splitGrid}>
            <section className={styles.portalPane} aria-labelledby={portalHeadingId}>
              <div className={styles.sectionHeading}>
                <div>
                  <p className={styles.eyebrow}>Prepared claim</p>
                  <h2 id={portalHeadingId}>Claim fields</h2>
                </div>
                <span className={styles.readOnlyBadge}>Read only</span>
              </div>
              {portalSurface ?? <ReadOnlySandboxForm snapshot={snapshot} />}
              {snapshot.claimPacket !== null ? (
                <>
                  <FieldComparison packet={snapshot.claimPacket} />
                  <VerificationAttemptsPanel
                    attempts={snapshot.verificationAttempts}
                    packet={snapshot.claimPacket}
                  />
                </>
              ) : (
                <EmptyPanel>No verified claim fields are available yet.</EmptyPanel>
              )}
            </section>

            <aside className={styles.authorityPane} aria-label="Evidence and agent activity">
              {snapshot.claimPacket !== null ? (
                <>
                  <AgentPlan packet={snapshot.claimPacket} />
                  <EvidenceBoard packet={snapshot.claimPacket} />
                </>
              ) : (
                <EmptyPanel>Evidence analysis has not produced a claim packet yet.</EmptyPanel>
              )}
              <AgentEventStrip events={events} />
            </aside>
          </div>
        </div>
      </details>
    </div>
  );
}

function CompletedClaimDocument({
  packet,
  state,
}: {
  readonly packet: ClaimPacket;
  readonly state: string;
}) {
  const claim = packet.claim;
  const fieldStatuses = [
    ["Incident date", claim.incidentDate],
    ["Incident time", claim.incidentTime],
    ["Location", claim.location],
    ["Claimant", claim.claimantName],
    ["Policy reference", claim.policyReference],
    ["Vehicle registration", claim.vehicleRegistration],
    ["Other driver", claim.counterpartyKnown === "unknown" ? null : claim.counterpartyKnown],
    ["Incident statement", claim.narrative],
  ] as const;
  const completedCount = fieldStatuses.filter(([, value]) => value !== null).length;
  const completeness = Math.round((completedCount / fieldStatuses.length) * 100);
  const ready = state === "review" && claim.missingRequiredFields.length === 0;

  return (
    <article className={styles.claimDocument} aria-labelledby="prepared-claim-title">
      <div className={styles.claimDocumentHeader}>
        <div>
          <p className={styles.eyebrow}>Insurance claim</p>
          <h2 id="prepared-claim-title">
            {ready ? "Your complete claim" : "Your claim in progress"}
          </h2>
        </div>
        <span className={ready ? styles.completeBadge : styles.progressBadge}>
          {ready ? "Ready to review" : "Being prepared"}
        </span>
      </div>

      <div className={styles.completeness}>
        <div>
          <span>Completeness</span>
          <strong>{completeness}%</strong>
        </div>
        <div
          aria-label={`Claim completeness ${completeness} percent`}
          aria-valuemax={100}
          aria-valuemin={0}
          aria-valuenow={completeness}
          className={styles.completenessTrack}
          role="progressbar"
        >
          <span style={{ width: `${completeness}%` }} />
        </div>
      </div>

      <dl className={styles.claimFieldGrid}>
        {fieldStatuses.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value === null ? "Needs one detail" : "Complete · source linked"}</dd>
          </div>
        ))}
      </dl>

      <div className={styles.claimDocumentFooter}>
        <span>Created from {claim.attachments.length} photos + your statement</span>
        <strong>{packet.gateDecisions.filter(({ passed }) => passed).length} checks passed</strong>
      </div>
    </article>
  );
}

function DeterministicGateTrail({
  snapshot,
}: {
  readonly snapshot: WorkflowSnapshot;
}) {
  const headingId = useId();
  const gates = snapshot.claimPacket?.gateDecisions ?? [];
  if (gates.length === 0) return null;
  return (
    <section className={styles.panel} aria-labelledby={headingId}>
      <div className={styles.sectionHeading}>
        <div>
          <p className={styles.eyebrow}>Deterministic authority</p>
          <h2 id={headingId}>Gate trail</h2>
        </div>
        <span className={styles.countBadge}>{gates.length} gates</span>
      </div>
      <ol className={styles.eventList} aria-label="Deterministic gate trail">
        {gates.map((gate) => (
          <li
            className={gate.passed ? styles.event_success : styles.event_blocked}
            key={gate.gateId}
          >
            <span className={styles.eventCursor}>{gate.gateId}</span>
            <span>
              {gate.passed
                ? "Passed deterministically"
                : gate.reasonCodes.map((reason) => gateReasonLabel(reason)).join("; ")}
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}

export function EvidenceBoard({ packet }: { readonly packet: ClaimPacket }) {
  const sourceLabels = buildSourceLabels(packet);
  const headingId = useId();
  return (
    <section className={styles.panel} aria-labelledby={headingId}>
      <div className={styles.sectionHeading}>
        <div>
          <p className={styles.eyebrow}>Source-aware</p>
          <h2 id={headingId}>Evidence board</h2>
        </div>
        <span className={styles.countBadge}>{packet.evidence.length} sources</span>
      </div>

      <ul className={styles.evidenceList} aria-label="Approved evidence">
        {packet.evidence.map((evidence) => {
          const approved =
            evidence.kind === "transcript"
              ? evidence.transcriptConfirmed === true
              : evidence.modelCopyApproved;
          return (
            <li className={styles.evidenceCard} key={evidence.evidenceId}>
              <div>
                <strong>{evidenceKindLabel(evidence.kind)}</strong>
                <span className={styles.mutedText}>{evidence.mediaType}</span>
              </div>
              <span
                className={
                  approved ? styles.approvedBadge : styles.blockedBadge
                }
              >
                {evidence.kind === "transcript"
                  ? approved
                    ? "Confirmed"
                    : "Not confirmed"
                  : approved
                    ? "Model copy approved"
                    : "Blocked"}
              </span>
            </li>
          );
        })}
      </ul>

      <h3>Supported facts</h3>
      <ul className={styles.factList}>
        {packet.facts.map((fact) => (
          <li
            className={`${styles.factCard} ${fact.status === "observed" && fact.confidence !== null && fact.confidence < 0.8 ? styles.factWarning : ""}`}
            key={fact.factId}
          >
            <div className={styles.factHeader}>
              <strong>{fieldLabel(fact.field)}</strong>
              <span>{factStatusLabel(fact.status)}</span>
            </div>
            <p>{confidenceLabel(fact.confidence, fact.status)}</p>
            <p className={styles.sourceLine}>
              Sources: {fact.sourceRefs.map((sourceId) => sourceLabels.get(sourceId) ?? "Unavailable").join(", ")}
            </p>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function AgentPlan({ packet }: { readonly packet: ClaimPacket }) {
  const headingId = useId();
  return (
    <section className={styles.panel} aria-labelledby={headingId}>
      <div className={styles.sectionHeading}>
        <div>
          <p className={styles.eyebrow}>Bounded authority</p>
          <h2 id={headingId}>Visible agent plan</h2>
        </div>
        <span className={styles.countBadge}>{packet.plan.steps.length} steps</span>
      </div>
      <ol className={styles.planList}>
        {packet.plan.steps.map((step) => (
          <li key={step.sequence}>
            <span className={styles.planSequence}>{step.sequence}</span>
            <div>
              <strong>{planToolLabel(step.tool)}</strong>
              <p>{planToolDescription(step.tool)}</p>
            </div>
          </li>
        ))}
      </ol>
      <p className={styles.planBoundary}>
        The plan stops at review. Approval and submission are not agent tools.
      </p>
    </section>
  );
}

export interface ClarificationPanelProps {
  readonly clarification: NonNullable<WorkflowSnapshot["clarification"]>;
  readonly onAnswer?: (request: ClarificationAnswerRequest) => void;
}

export function clarificationIdentityKey(
  clarification: NonNullable<WorkflowSnapshot["clarification"]>,
): string {
  return JSON.stringify([
    clarification.caseId,
    clarification.clarificationId,
    clarification.field,
    clarification.round,
    clarification.expectedVersion,
  ]);
}

export function ClarificationPanel({
  clarification,
  onAnswer,
}: ClarificationPanelProps) {
  const [answer, setAnswer] = useState("");
  const [error, setError] = useState<string | null>(null);
  const headingId = useId();

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (onAnswer === undefined) return;
    try {
      onAnswer(buildClarificationAnswerRequest(clarification, answer));
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The answer is invalid.");
    }
  }

  const helpId = `${clarification.clarificationId}-help`;
  const errorId = `${clarification.clarificationId}-error`;
  return (
    <section className={styles.clarification} aria-labelledby={headingId}>
      <p className={styles.eyebrow}>One active clarification</p>
      <h2 id={headingId}>One detail is needed next</h2>
      <p className={styles.question}>{clarification.question}</p>
      <form onSubmit={submit}>
        <label htmlFor={clarification.clarificationId}>
          {fieldLabel(clarification.field)}
        </label>
        <textarea
          aria-describedby={error === null ? helpId : `${helpId} ${errorId}`}
          aria-invalid={error !== null}
          disabled={onAnswer === undefined}
          id={clarification.clarificationId}
          maxLength={4_000}
          onChange={(event) => setAnswer(event.target.value)}
          rows={3}
          value={answer}
        />
        <p className={styles.mutedText} id={helpId}>
          Round {clarification.round} · bound to case version {clarification.expectedVersion}.
          Whitespace is preserved exactly.
        </p>
        {error !== null ? (
          <p className={styles.inlineError} id={errorId} role="alert">
            {error}
          </p>
        ) : null}
        <button className={styles.primaryButton} disabled={onAnswer === undefined} type="submit">
          Send clarification
        </button>
        {onAnswer === undefined ? (
          <span className={styles.mutedText}>Command transport is not connected in this showcase.</span>
        ) : null}
      </form>
    </section>
  );
}

export function AgentEventStrip({
  events,
}: {
  readonly events: readonly WorkflowEventSummary[];
}) {
  const headingId = useId();
  return (
    <section className={styles.panel} aria-labelledby={headingId}>
      <div className={styles.sectionHeading}>
        <div>
          <p className={styles.eyebrow}>Redacted timeline</p>
          <h2 id={headingId}>Agent activity</h2>
        </div>
        <span className={styles.countBadge}>{events.length} events</span>
      </div>
      {events.length === 0 ? (
        <EmptyPanel>No redacted workflow events yet.</EmptyPanel>
      ) : (
        <ol className={styles.eventList}>
          {events.map((event) => (
            <li className={styles[`event_${event.severity}`]} key={event.cursor}>
              <span className={styles.eventCursor}>#{event.cursor}</span>
              <span>{event.label}</span>
              <time dateTime={event.occurredAt}>{formatTime(event.occurredAt)}</time>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

export function VerificationAttemptsPanel({
  attempts,
  packet,
}: {
  readonly attempts: WorkflowSnapshot["verificationAttempts"];
  readonly packet: ClaimPacket;
}) {
  const sourceLabels = buildSourceLabels(packet);
  const firstRepair = attempts?.attempts[0]?.repair ?? null;
  const headingId = useId();
  return (
    <section className={styles.attempts} aria-labelledby={headingId}>
      <h3 id={headingId}>Verification attempts</h3>
      {attempts === null ? (
        <EmptyPanel>No verification attempt has been recorded.</EmptyPanel>
      ) : (
        <ol>
          {attempts.attempts.map((attempt) => {
            const nonMatching = attempt.report.fieldResults.filter(
              (field) => field.status !== "match",
            );
            const repair =
              attempt.repair ??
              (attempt.attemptNumber === 2 ? firstRepair : null);
            return (
              <li key={attempt.attemptNumber}>
                <div className={styles.attemptHeader}>
                  <strong>Attempt {attempt.attemptNumber}</strong>
                  <StatusPill
                    status={
                      attempt.report.status === "verified" ? "match" : "mismatch"
                    }
                  />
                </div>
                <p>
                  {attempt.final ? "Final attempt" : "Repairable attempt"} ·{" "}
                  {attempt.report.status}
                </p>
                <p>
                  {nonMatching.length === 0
                    ? "No mismatching or missing scalar fields."
                    : `Affected fields: ${nonMatching
                        .map(
                          (field) =>
                            `${fieldLabel(field.field)} (${field.status})`,
                        )
                        .join(", ")}`}
                </p>
                <p className={styles.repairSummary}>
                  {repair === null
                    ? "No repair was authorized."
                    : `${
                        attempt.attemptNumber === 2
                          ? "Authorized repair used"
                          : "One narrow repair authorized"
                      }: ${fieldLabel(repair.field)} · Sources: ${repair.sourceRefs
                        .map(
                          (source) => sourceLabels.get(source) ?? "Unavailable",
                        )
                        .join(", ")}`}
                </p>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

export function HumanApprovalBoundary({ state }: { readonly state: string }) {
  const recorded = state === "human_approved" || state === "receipt";
  return (
    <aside className={styles.approvalBoundary} aria-label="Human approval boundary" role="note">
      <span aria-hidden="true" className={styles.lockIcon}>⌁</span>
      <div>
        <strong>
          {recorded ? "Human approval recorded" : "Not submitted / human approval required"}
        </strong>
        <p>
          The agent has no approval or submission authority. Approval is handled outside the
          agent browser through a separate one-time human capability.
        </p>
      </div>
    </aside>
  );
}

function ReadOnlySandboxForm({ snapshot }: { readonly snapshot: WorkflowSnapshot }) {
  if (snapshot.portalSession === null) {
    return <EmptyPanel>The sandbox form has not been opened by the workflow.</EmptyPanel>;
  }
  return (
    <div className={styles.formSurface}>
      <dl className={styles.portalMetadata}>
        <div><dt>Sandbox variant</dt><dd>{snapshot.portalSession.variant}</dd></div>
        <div><dt>Form version</dt><dd>{snapshot.portalSession.version}</dd></div>
        <div><dt>Visible fields</dt><dd>8 scalar slots · up to 3 staged attachments</dd></div>
        <div><dt>Submission</dt><dd>Unavailable to agent</dd></div>
      </dl>
      <div className={styles.formGrid} aria-label="Read-only sandbox form structure">
        {REQUIRED_SCALAR_FIELDS.map((field) => (
          <div className={field === "narrative" ? styles.formFieldWide : styles.formField} key={field}>
            <span>{fieldLabel(field)}</span>
            <output>Value withheld · verification status shown below</output>
          </div>
        ))}
        <div className={styles.formFieldWide}>
          <span>Evidence attachments</span>
          <output>Up to 3 staged attachments · identifiers withheld</output>
        </div>
      </div>
    </div>
  );
}

function FieldComparison({ packet }: { readonly packet: ClaimPacket }) {
  const headingId = useId();
  const results = new Map(
    packet.verification.fieldResults.map((result) => [result.field, result]),
  );
  const sources = new Map(
    packet.claim.fieldProvenance.map((entry) => [entry.field, entry.sourceRefs]),
  );
  const sourceLabels = buildSourceLabels(packet);
  return (
    <section className={styles.comparison} aria-labelledby={headingId}>
      <h3 id={headingId}>Field verification</h3>
      <div className={styles.tableScroller} tabIndex={0} aria-label="Field verification table">
        <table>
          <thead><tr><th scope="col">Field</th><th scope="col">Sources</th><th scope="col">Verifier</th></tr></thead>
          <tbody>
            {REQUIRED_SCALAR_FIELDS.map((field) => {
              const result = results.get(field);
              return (
                <tr key={field}>
                  <th scope="row">{fieldLabel(field)}</th>
                  <td>{sources.get(field)?.map((source) => sourceLabels.get(source) ?? "Unavailable").join(", ") ?? "Pending"}</td>
                  <td><StatusPill status={result?.status ?? "pending"} /></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function ReceiptPanel({ snapshot }: { readonly snapshot: WorkflowSnapshot }) {
  const headingId = useId();
  if (snapshot.receipt === null) return null;
  const receipt = snapshot.receipt;
  return (
    <section className={styles.receipt} aria-labelledby={headingId}>
      <h2 id={headingId}>Redacted sandbox receipt</h2>
      <p>No claimant, policy, registration, narrative, or attachment values are included.</p>
      <dl>
        <div><dt>Environment</dt><dd>Sandbox only</dd></div>
        <div><dt>Completed fields</dt><dd>{receipt.summary.completedFieldCount}</dd></div>
        <div><dt>Attachment count</dt><dd>{receipt.summary.attachmentCount}</dd></div>
        <div><dt>Verification</dt><dd>Passed</dd></div>
        <div><dt>Real insurer submission</dt><dd>No</dd></div>
      </dl>
      <HumanApprovalBoundary state="receipt" />
    </section>
  );
}

function WorkflowState({
  children,
  title,
  tone,
}: {
  readonly children: ReactNode;
  readonly title: string;
  readonly tone: "loading" | "empty" | "error" | "blocked" | "success";
}) {
  const isAlert = tone === "error" || tone === "blocked";
  return (
    <div className={`${styles.statePanel} ${styles[`state_${tone}`]}`} role={isAlert ? "alert" : "status"}>
      <h1>{title}</h1>
      <div>{children}</div>
    </div>
  );
}

function EmptyPanel({ children }: { readonly children: ReactNode }) {
  return <p className={styles.emptyPanel}>{children}</p>;
}

function StatusPill({ status }: { readonly status: "match" | "mismatch" | "missing" | "pending" }) {
  return <span className={styles[`status_${status}`]}>{statusLabel(status)}</span>;
}

const REQUIRED_SCALAR_FIELDS = [
  "incident_date",
  "incident_time",
  "location",
  "claimant_name",
  "policy_reference",
  "vehicle_registration",
  "counterparty_known",
  "narrative",
] as const satisfies readonly RequiredClaimField[];

function stateTitle(state: string): string {
  if (state === "verifying") return "Verification in progress";
  if (state === "review") return "Verified review ready";
  if (state === "awaiting_clarification") return "Clarification required";
  if (state === "human_approved") return "Human approval recorded";
  return "Claim workflow";
}

function stateLabel(state: string): string {
  return state.replaceAll("_", " ");
}

function isStopped(state: string): boolean {
  return ["blocked", "emergency_stopped", "abandoned", "failed"].includes(state);
}

function stoppedTitle(state: string): string {
  if (state === "emergency_stopped") return "Emergency stop activated";
  if (state === "abandoned") return "Workflow abandoned";
  if (state === "failed") return "Workflow failed";
  return "Workflow blocked";
}

function fieldLabel(field: string): string {
  const labels: Readonly<Record<string, string>> = {
    attachments: "Evidence attachments",
    claimant_name: "Claimant name",
    collision_type: "Collision type",
    counterparty_known: "Counterparty known",
    impact_area: "Impact area",
    immediate_danger: "Immediate danger",
    incident_date: "Incident date",
    incident_time: "Incident time",
    injury_status: "Injury status",
    location: "Incident location",
    narrative: "Neutral narrative",
    policy_reference: "Policy reference",
    vehicle_count: "Vehicle count",
    vehicle_registration: "Vehicle registration",
    visible_damage: "Visible damage",
  };
  return labels[field] ?? "Supported fact";
}

function evidenceKindLabel(kind: string): string {
  const labels: Readonly<Record<string, string>> = {
    clarification: "Confirmed clarification",
    image: "Staged image",
    transcript: "Transcript",
    user_statement: "User statement",
  };
  return labels[kind] ?? "Approved evidence";
}

function planToolLabel(tool: string): string {
  const labels: Readonly<Record<string, string>> = {
    ask_clarification: "Ask one clarification",
    check_required_fields: "Check required fields",
    fill_until_review: "Fill sandbox draft",
    inspect_evidence: "Inspect approved evidence",
    inspect_form: "Inspect sandbox form",
    read_receipt: "Read redacted receipt",
    verify_rendered_fields: "Verify rendered fields",
  };
  return labels[tool] ?? "Bounded workflow step";
}

function planToolDescription(tool: string): string {
  const descriptions: Readonly<Record<string, string>> = {
    ask_clarification: "Ask only the deterministic question accepted for this round.",
    check_required_fields: "Use the deterministic completeness result.",
    fill_until_review: "Write the sandbox draft only until its review boundary.",
    inspect_evidence: "Read only the staged and approved evidence inventory.",
    inspect_form: "Inspect only the local sandbox form structure.",
    read_receipt: "Read only the redacted sandbox receipt.",
    verify_rendered_fields: "Compare rendered fields before human review.",
  };
  return descriptions[tool] ?? "Perform one registered, bounded workflow action.";
}

function buildSourceLabels(packet: ClaimPacket): ReadonlyMap<string, string> {
  let imageNumber = 0;
  const evidenceLabels = new Map<string, string>();
  for (const evidence of packet.evidence) {
    if (evidence.kind === "image") imageNumber += 1;
    const label =
      evidence.kind === "image"
        ? `Staged image ${imageNumber}`
        : evidence.kind === "transcript"
          ? "Confirmed transcript"
          : evidence.kind === "clarification"
            ? "Confirmed clarification"
            : "User statement";
    evidenceLabels.set(evidence.evidenceId, label);
  }
  return new Map(
    packet.provenance.map((source) => [
      source.provenanceId,
      evidenceLabels.get(source.evidenceId) ?? "Unavailable",
    ]),
  );
}

function factStatusLabel(status: string): string {
  const labels: Readonly<Record<string, string>> = {
    not_supported: "Not supported",
    observed: "Observed",
    unknown: "Unknown",
    user_stated: "User stated",
  };
  return labels[status] ?? "Unknown";
}

export function confidenceLabel(confidence: number | null, status: string): string {
  if (confidence === null) return status === "user_stated" ? "User-supplied · not model-scored" : "Confidence unavailable";
  if (confidence < 0.8) return `Uncertain · ${Math.round(confidence * 100)}% · below deterministic 80% threshold`;
  if (confidence >= 0.9) return `High confidence · ${Math.round(confidence * 100)}%`;
  return `Meets deterministic threshold · ${Math.round(confidence * 100)}%`;
}

function statusLabel(status: string): string {
  const labels: Readonly<Record<string, string>> = {
    match: "Match",
    mismatch: "Mismatch",
    missing: "Missing",
    pending: "Pending",
  };
  return labels[status] ?? "Pending";
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("en", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    timeZone: "UTC",
  }).format(new Date(value));
}
