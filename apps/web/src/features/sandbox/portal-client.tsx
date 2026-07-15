"use client";

import Link from "next/link";
import type { ChangeEvent, FormEvent } from "react";
import { useCallback, useEffect, useMemo, useState } from "react";

import type {
  PortalFieldIssue,
  PortalFieldName,
  PortalDraftFields,
  PortalFixture,
  PortalState,
  PortalVariant,
  PortalView,
} from "./contracts";
import { EMPTY_PORTAL_FIELDS, clonePortalFields } from "./fixtures";
import styles from "./sandbox-portal.module.css";

interface SandboxPortalClientProps {
  readonly caseId: string;
  readonly variant: PortalVariant;
}

interface RequestErrorBody {
  readonly error?: Readonly<{
    code?: string;
    fieldErrors?: readonly PortalFieldIssue[];
    message?: string;
  }>;
}

class PortalRequestError extends Error {
  constructor(
    message: string,
    readonly fieldErrors: readonly PortalFieldIssue[] = [],
  ) {
    super(message);
    this.name = "PortalRequestError";
  }
}

const FIELD_ORDER: Readonly<Record<PortalVariant, readonly PortalFieldName[]>> = {
  A: [
    "incidentDate",
    "incidentTime",
    "location",
    "claimantName",
    "policyReference",
    "vehicleRegistration",
    "counterpartyKnown",
    "narrative",
    "attachments",
  ],
  B: [
    "claimantName",
    "vehicleRegistration",
    "policyReference",
    "counterpartyKnown",
    "location",
    "incidentDate",
    "incidentTime",
    "attachments",
    "narrative",
  ],
};

const FIELD_LABELS: Readonly<Record<PortalFieldName, string>> = {
  attachments: "Evidence images",
  claimantName: "Claimant name",
  counterpartyKnown: "Are the other driver's details known?",
  incidentDate: "Incident date",
  incidentTime: "Incident time",
  location: "Incident location",
  narrative: "What happened?",
  policyReference: "Policy reference",
  vehicleRegistration: "Vehicle registration",
};

export function SandboxPortalClient({ caseId, variant }: SandboxPortalClientProps) {
  const [view, setView] = useState<PortalView | null>(null);
  const [fields, setFields] = useState<PortalDraftFields>(() =>
    clonePortalFields(EMPTY_PORTAL_FIELDS),
  );
  const [fixture, setFixture] = useState<PortalFixture>("complete");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [globalError, setGlobalError] = useState<string | null>(null);
  const [issues, setIssues] = useState<readonly PortalFieldIssue[]>([]);

  const load = useCallback(async () => {
    setGlobalError(null);
    try {
      const loaded = await requestPortal<PortalView>(portalUrl(caseId, variant));
      setView(loaded);
      setFields(clonePortalFields(loaded.fields));
    } catch (error) {
      setGlobalError(errorMessage(error));
    }
  }, [caseId, variant]);

  useEffect(() => {
    let cancelled = false;
    requestPortal<PortalView>(portalUrl(caseId, variant))
      .then((loaded) => {
        if (cancelled) return;
        setGlobalError(null);
        setView(loaded);
        setFields(clonePortalFields(loaded.fields));
      })
      .catch((error: unknown) => {
        if (!cancelled) setGlobalError(errorMessage(error));
      });
    return () => {
      cancelled = true;
    };
  }, [caseId, variant]);

  const issuesByField = useMemo(() => {
    const map = new Map<PortalFieldName, string>();
    for (const issue of issues) map.set(issue.field, issue.message);
    return map;
  }, [issues]);

  async function saveDraft(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (!view) return;
    setBusy(true);
    clearFeedback();
    try {
      if (portalDraftFieldsEqual(view.fields, fields)) {
        setMessage("Draft is already up to date on the sandbox server.");
        return;
      }
      const saved = await putDraft(caseId, variant, view.version, fields);
      setView(saved);
      setFields(clonePortalFields(saved.fields));
      setMessage("Draft saved on the sandbox server.");
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
    }
  }

  async function continueToReview(): Promise<void> {
    if (!view) return;
    setBusy(true);
    clearFeedback();
    try {
      const reviewed = await advancePortalDraftToReview(
        caseId,
        variant,
        view,
        fields,
      );
      setView(reviewed);
      setFields(clonePortalFields(reviewed.fields));
      setMessage("Review reached. No claim has been submitted.");
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
    }
  }

  async function resetFixture(): Promise<void> {
    setBusy(true);
    clearFeedback();
    try {
      const reset = await requestPortal<PortalView>("/api/dev/reset", {
        body: JSON.stringify({ caseId, fixture, variant }),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      });
      setView(reset);
      setFields(clonePortalFields(reset.fields));
      setMessage(`Developer fixture “${fixture}” loaded.`);
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
    }
  }

  function clearFeedback(): void {
    setGlobalError(null);
    setIssues([]);
    setMessage(null);
  }

  function showError(error: unknown): void {
    if (error instanceof PortalRequestError) setIssues(error.fieldErrors);
    setGlobalError(errorMessage(error));
  }

  function setTextField(
    field: Exclude<PortalFieldName, "attachments" | "counterpartyKnown">,
    value: string,
  ): void {
    setFields((current) => ({ ...current, [field]: value }));
  }

  function setCounterparty(value: PortalDraftFields["counterpartyKnown"]): void {
    setFields((current) => ({ ...current, counterpartyKnown: value }));
  }

  function chooseAttachments(event: ChangeEvent<HTMLInputElement>): void {
    const assetIds = demoAssetIdsForFiles(Array.from(event.target.files ?? []));
    setFields((current) => ({ ...current, attachments: assetIds }));
    if (assetIds.length !== 3) {
      setIssues([
        {
          code: "PORTAL_ATTACHMENT_COUNT",
          field: "attachments",
          message: "Choose exactly three staged images to generate demo asset IDs.",
        },
      ]);
    } else {
      setIssues((current) => current.filter((issue) => issue.field !== "attachments"));
    }
  }

  function removeAttachment(index: number): void {
    setFields((current) => ({
      ...current,
      attachments: current.attachments.filter((_, itemIndex) => itemIndex !== index),
    }));
  }

  const otherVariant: PortalVariant = variant === "A" ? "B" : "A";

  if (!view && !globalError) {
    return (
      <PortalFrame caseId={caseId} state="draft" variant={variant}>
        <div aria-live="polite" className={styles.loading} role="status">
          <span className={styles.spinner} aria-hidden="true" />
          Loading the sandbox portal…
        </div>
      </PortalFrame>
    );
  }

  if (!view) {
    return (
      <PortalFrame caseId={caseId} state="draft" variant={variant}>
        <div className={styles.errorPanel} role="alert">
          <h2>Portal unavailable</h2>
          <p>{globalError}</p>
          <div className={styles.formActions}>
            <button
              className={styles.primaryButton}
              disabled={busy}
              onClick={() => void resetFixture()}
              type="button"
            >
              {busy ? "Creating…" : "Create deterministic fixture"}
            </button>
            <button
              className={styles.secondaryButton}
              disabled={busy}
              onClick={() => void load()}
              type="button"
            >
              Try read again
            </button>
          </div>
        </div>
      </PortalFrame>
    );
  }

  return (
    <PortalFrame caseId={caseId} state={view.state} variant={variant}>
      <div className={styles.toolbar}>
        <div>
          <span className={styles.eyebrow}>Developer tools</span>
          <p>Reset to deterministic synthetic data. This never approves or submits a claim.</p>
        </div>
        <div className={styles.fixtureControls}>
          <label htmlFor="fixture-select">Fixture</label>
          <select
            id="fixture-select"
            onChange={(event) => setFixture(event.target.value as PortalFixture)}
            value={fixture}
          >
            <option value="empty">Empty draft</option>
            <option value="complete">Complete demo</option>
          </select>
          <button
            className={styles.secondaryButton}
            disabled={busy}
            onClick={() => void resetFixture()}
            type="button"
          >
            Reset fixture
          </button>
          <Link
            className={styles.variantLink}
            href={`/sandbox/${otherVariant}/cases/${caseId}-${otherVariant.toLowerCase()}`}
          >
            Open layout {otherVariant}
          </Link>
        </div>
      </div>

      {globalError ? (
        <div className={styles.inlineError} role="alert">
          <strong>Action blocked.</strong> {globalError}
        </div>
      ) : null}
      {message ? (
        <div className={styles.successMessage} role="status">
          {message}
        </div>
      ) : null}

      {view.state === "draft" ? (
        <form className={styles.form} onSubmit={(event) => void saveDraft(event)}>
          <div className={styles.sectionHeading}>
            <div>
              <span className={styles.eyebrow}>Claim details</span>
              <h2>Review the prepared draft</h2>
            </div>
            <span className={styles.version}>Server version {view.version}</span>
          </div>
          <div className={variant === "B" ? styles.fieldsVariantB : styles.fieldsVariantA}>
            {FIELD_ORDER[variant].map((field) => (
              <PortalField
                error={issuesByField.get(field)}
                field={field}
                fields={fields}
                key={field}
                onAttachments={chooseAttachments}
                onCounterparty={setCounterparty}
                onRemoveAttachment={removeAttachment}
                onText={setTextField}
                variant={variant}
              />
            ))}
          </div>
          <div className={styles.formActions}>
            <button className={styles.secondaryButton} disabled={busy} type="submit">
              {busy ? "Saving…" : "Save draft"}
            </button>
            <button
              className={styles.primaryButton}
              disabled={busy}
              onClick={() => void continueToReview()}
              type="button"
            >
              Continue to review
            </button>
          </div>
        </form>
      ) : (
        <PortalStateView fields={view.fields} state={view.state} />
      )}

      <footer className={styles.auditFooter}>
        <span>{view.auditCount} redacted server audit events</span>
        <span>Last updated {formatTimestamp(view.updatedAt)}</span>
      </footer>
    </PortalFrame>
  );
}

export type SavePortalDraft = (
  caseId: string,
  variant: PortalVariant,
  expectedVersion: number,
  fields: PortalDraftFields,
) => Promise<PortalView>;

export type StartPortalReview = (
  caseId: string,
  variant: PortalVariant,
  expectedVersion: number,
) => Promise<PortalView>;

/**
 * Preserve exact-once active-run writes while retaining normal dirty-draft behavior.
 * A byte-identical loaded view is already authoritative and can advance directly.
 */
export async function advancePortalDraftToReview(
  caseId: string,
  variant: PortalVariant,
  view: PortalView,
  fields: PortalDraftFields,
  save: SavePortalDraft = putDraft,
  review: StartPortalReview = postReview,
): Promise<PortalView> {
  const saved = portalDraftFieldsEqual(view.fields, fields)
    ? view
    : await save(caseId, variant, view.version, fields);
  return review(caseId, variant, saved.version);
}

export function portalDraftFieldsEqual(
  left: PortalDraftFields,
  right: PortalDraftFields,
): boolean {
  return (
    left.incidentDate === right.incidentDate &&
    left.incidentTime === right.incidentTime &&
    left.location === right.location &&
    left.claimantName === right.claimantName &&
    left.policyReference === right.policyReference &&
    left.vehicleRegistration === right.vehicleRegistration &&
    left.counterpartyKnown === right.counterpartyKnown &&
    left.narrative === right.narrative &&
    left.attachments.length === right.attachments.length &&
    left.attachments.every(
      (attachmentId, index) => attachmentId === right.attachments[index],
    )
  );
}

interface PortalFieldProps {
  readonly error: string | undefined;
  readonly field: PortalFieldName;
  readonly fields: PortalDraftFields;
  readonly onAttachments: (event: ChangeEvent<HTMLInputElement>) => void;
  readonly onCounterparty: (value: PortalDraftFields["counterpartyKnown"]) => void;
  readonly onRemoveAttachment: (index: number) => void;
  readonly onText: (
    field: Exclude<PortalFieldName, "attachments" | "counterpartyKnown">,
    value: string,
  ) => void;
  readonly variant: PortalVariant;
}

function PortalField(props: PortalFieldProps) {
  const { error, field, fields, variant } = props;
  const inputId = `portal-${field}`;
  const errorId = `${inputId}-error`;
  const describedBy = error ? errorId : undefined;

  if (field === "attachments") {
    return (
      <div className={`${styles.field} ${styles.wideField}`}>
        <label htmlFor={inputId}>{FIELD_LABELS[field]}</label>
        <span className={styles.hint}>
          Server fills use approved asset IDs. This developer control creates synthetic
          demo IDs only; file bytes are not uploaded.
        </span>
        <input
          accept="image/jpeg,image/png"
          aria-describedby={describedBy}
          aria-invalid={Boolean(error)}
          id={inputId}
          multiple
          onChange={props.onAttachments}
          type="file"
        />
        {fields.attachments.length ? (
          <ul className={styles.attachmentList}>
            {fields.attachments.map((name, index) => (
              <li key={`${name}-${index}`}>
                <span>{name}</span>
                <button onClick={() => props.onRemoveAttachment(index)} type="button">
                  Remove<span className={styles.srOnly}> {name}</span>
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <span className={styles.emptyValue}>No images attached.</span>
        )}
        <FieldError error={error} id={errorId} />
      </div>
    );
  }

  if (field === "counterpartyKnown") {
    const labelId = `${inputId}-label`;
    return (
      <div className={styles.field}>
        {variant === "A" ? (
          <label htmlFor={inputId}>{FIELD_LABELS[field]}</label>
        ) : (
          <span className={styles.controlLabel} id={labelId}>
            {FIELD_LABELS[field]}
          </span>
        )}
        <select
          aria-describedby={describedBy}
          aria-invalid={Boolean(error)}
          aria-labelledby={variant === "B" ? labelId : undefined}
          id={inputId}
          onChange={(event) =>
            props.onCounterparty(event.target.value as PortalDraftFields["counterpartyKnown"])
          }
          value={fields.counterpartyKnown}
        >
          <option value="">Select…</option>
          <option value="yes">Yes</option>
          <option value="no">No</option>
          <option value="unknown">Unknown</option>
        </select>
        <FieldError error={error} id={errorId} />
      </div>
    );
  }

  const inputType = field === "incidentDate" ? "date" : field === "incidentTime" ? "time" : "text";
  if (field === "narrative") {
    return (
      <div className={`${styles.field} ${styles.wideField}`}>
        <label htmlFor={inputId}>{FIELD_LABELS[field]}</label>
        <textarea
          aria-describedby={describedBy}
          aria-invalid={Boolean(error)}
          id={inputId}
          onChange={(event) => props.onText(field, event.target.value)}
          rows={5}
          value={fields[field]}
        />
        <FieldError error={error} id={errorId} />
      </div>
    );
  }
  return (
    <div className={styles.field}>
      <label htmlFor={inputId}>{FIELD_LABELS[field]}</label>
      <input
        aria-describedby={describedBy}
        aria-invalid={Boolean(error)}
        id={inputId}
        onChange={(event) => props.onText(field, event.target.value)}
        step={field === "incidentTime" ? 1 : undefined}
        type={inputType}
        value={fields[field]}
      />
      <FieldError error={error} id={errorId} />
    </div>
  );
}

function FieldError({ error, id }: Readonly<{ error: string | undefined; id: string }>) {
  return error ? (
    <span className={styles.fieldError} id={id}>
      {error}
    </span>
  ) : null;
}

export function PortalStateView({
  fields,
  state,
}: Readonly<{ fields: PortalDraftFields; state: Exclude<PortalState, "draft"> }>) {
  const isReceipt = state === "receipt";
  return (
    <section className={styles.reviewPanel} aria-labelledby="portal-review-heading">
      <div className={styles.sectionHeading}>
        <div>
          <span className={styles.eyebrow}>{isReceipt ? "Sandbox receipt" : "Read-only"}</span>
          <h2 id="portal-review-heading">
            {state === "review"
              ? "Ready for human review"
              : state === "human_approved"
                ? "Approved by a separate human context"
                : "Sandbox receipt created"}
          </h2>
        </div>
        <span className={styles.stateBadge}>{state.replaceAll("_", " ")}</span>
      </div>
      <div className={styles.boundaryNotice} role="note">
        <strong>Agent boundary:</strong> this portal view cannot approve, submit, or create a
        receipt. Human approval will use a separate one-time token in the secured workflow.
      </div>
      <dl className={styles.reviewGrid}>
        <ReviewValue label="Incident" value={`${fields.incidentDate} at ${fields.incidentTime}`} />
        <ReviewValue label="Location" value={fields.location} />
        <ReviewValue label="Claimant" value={isReceipt ? "Redacted" : fields.claimantName} />
        <ReviewValue
          label="Policy reference"
          value={isReceipt ? mask(fields.policyReference) : fields.policyReference}
        />
        <ReviewValue
          label="Vehicle"
          value={isReceipt ? mask(fields.vehicleRegistration) : fields.vehicleRegistration}
        />
        <ReviewValue label="Counterparty known" value={fields.counterpartyKnown || "Not set"} />
        <ReviewValue label="Narrative" value={isReceipt ? "Redacted after approval" : fields.narrative} wide />
        <ReviewValue label="Attachments" value={`${fields.attachments.length} approved images`} />
      </dl>
      {state === "review" ? (
        <button className={styles.disabledApproval} disabled type="button">
          Human approval required in a separate context
        </button>
      ) : null}
    </section>
  );
}

function ReviewValue({
  label,
  value,
  wide = false,
}: Readonly<{ label: string; value: string; wide?: boolean }>) {
  return (
    <div className={wide ? styles.reviewValueWide : styles.reviewValue}>
      <dt>{label}</dt>
      <dd>{value || "Not provided"}</dd>
    </div>
  );
}

function PortalFrame({
  caseId,
  children,
  state,
  variant,
}: Readonly<{
  caseId: string;
  children: React.ReactNode;
  state: PortalState;
  variant: PortalVariant;
}>) {
  return (
    <main className={styles.portalPage} id="main-content">
      <div className={styles.sandboxBanner} role="note">
        <span className={styles.sandboxDot} aria-hidden="true" />
        Sandbox simulation · no real insurer · nothing is submitted
      </div>
      <header className={styles.portalHeader}>
        <div className={styles.brandMark} aria-hidden="true">
          CD
        </div>
        <div>
          <span className={styles.eyebrow}>ClaimDone demo portal</span>
          <h1>Vehicle incident form</h1>
        </div>
        <div className={styles.headerMeta}>
          <span>Case {caseId}</span>
          <span>Layout {variant}</span>
          <span className={styles.stateBadge}>{state.replaceAll("_", " ")}</span>
        </div>
      </header>
      <div className={styles.portalContent}>{children}</div>
    </main>
  );
}

async function putDraft(
  caseId: string,
  variant: PortalVariant,
  expectedVersion: number,
  fields: PortalDraftFields,
): Promise<PortalView> {
  return requestPortal<PortalView>(portalUrl(caseId, variant, "/draft"), {
    body: JSON.stringify({ expectedVersion, fields }),
    headers: { "Content-Type": "application/json" },
    method: "PUT",
  });
}

function portalUrl(caseId: string, variant: PortalVariant, suffix = ""): string {
  return `/api/sandbox/cases/${encodeURIComponent(caseId)}${suffix}?variant=${variant}`;
}

async function requestPortal<T>(input: string, init?: RequestInit): Promise<T> {
  const response = await fetch(input, { ...init, cache: "no-store" });
  const body = (await response.json()) as T | RequestErrorBody;
  if (!response.ok) {
    const error = (body as RequestErrorBody).error;
    throw new PortalRequestError(
      error?.message ?? "The sandbox portal rejected the request.",
      error?.fieldErrors ?? [],
    );
  }

  return body as T;
}

async function postReview(
  caseId: string,
  variant: PortalVariant,
  expectedVersion: number,
): Promise<PortalView> {
  return requestPortal<PortalView>(portalUrl(caseId, variant, "/review"), {
    body: JSON.stringify({ expectedVersion }),
    headers: { "Content-Type": "application/json" },
    method: "POST",
  });
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Unexpected sandbox portal error.";
}

function mask(value: string): string {
  if (!value) return "Redacted";
  return `${value.slice(0, 2)}••••${value.slice(-1)}`;
}

function formatTimestamp(value: string): string {
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.valueOf()) ? "just now" : timestamp.toLocaleTimeString();
}

export function demoAssetIdsForFiles(
  files: readonly Pick<File, "name">[],
): readonly string[] {
  return files.map((file, index) => {
    const stem = file.name.replace(/\.[^.]+$/, "").toLowerCase();
    const slug = stem.replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 72);
    return `asset-demo-local-${index + 1}-${slug || "image"}`;
  });
}
