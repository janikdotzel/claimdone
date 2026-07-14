import type {
  CaseState,
  GateReasonCode,
  ProviderFailureCategory,
  WorkflowEventEnvelope,
  WorkflowSnapshot,
} from "../../../../../contracts/generated/claimdone";

export interface WorkflowEventSummary {
  readonly cursor: number;
  readonly kind: WorkflowEventEnvelope["event"]["kind"];
  readonly label: string;
  readonly occurredAt: string;
  readonly severity: "info" | "success" | "warning" | "blocked";
}

export interface WorkflowSnapshotRequestIdentity {
  readonly caseId: string;
  readonly refreshGeneration: number;
  readonly requestToken: number;
}

export interface WorkflowEventStore {
  readonly activeCaseId: string | null;
  readonly events: readonly WorkflowEventSummary[];
  readonly failedClosed: string | null;
  readonly lastCursor: number | null;
  readonly lastEnvelopeDigest: string | null;
  readonly latestSnapshotRequestToken: number;
  readonly needsSnapshotRefresh: boolean;
  readonly pendingSnapshotRequest: WorkflowSnapshotRequestIdentity | null;
  readonly refreshGeneration: number;
  readonly snapshot: WorkflowSnapshot | null;
}

export type WorkflowStoreAction =
  | {
      readonly caseId: string;
      readonly refreshGeneration: number;
      readonly requestToken: number;
      readonly type: "SNAPSHOT_REQUESTED";
    }
  | {
      readonly refreshGeneration: number;
      readonly requestToken: number;
      readonly snapshot: WorkflowSnapshot;
      readonly type: "SNAPSHOT_RECEIVED";
    }
  | { readonly envelope: WorkflowEventEnvelope; readonly type: "EVENT_RECEIVED" }
  | { readonly message: string; readonly type: "STREAM_FAILED" }
  | { readonly type: "STREAM_RESET" };

export const INITIAL_WORKFLOW_EVENT_STORE: WorkflowEventStore = {
  activeCaseId: null,
  events: [],
  failedClosed: null,
  lastCursor: null,
  lastEnvelopeDigest: null,
  latestSnapshotRequestToken: 0,
  needsSnapshotRefresh: false,
  pendingSnapshotRequest: null,
  refreshGeneration: 0,
  snapshot: null,
};

/**
 * Event projections are observational. Only SNAPSHOT_RECEIVED replaces product
 * authority; every accepted audit-backed event merely requests a fresh snapshot.
 */
export function reduceWorkflowEventStore(
  state: WorkflowEventStore,
  action: WorkflowStoreAction,
): WorkflowEventStore {
  if (action.type === "STREAM_RESET") {
    return {
      ...INITIAL_WORKFLOW_EVENT_STORE,
      activeCaseId: state.activeCaseId,
      latestSnapshotRequestToken: state.latestSnapshotRequestToken,
      needsSnapshotRefresh: state.needsSnapshotRefresh,
      pendingSnapshotRequest: state.pendingSnapshotRequest,
      refreshGeneration: state.refreshGeneration,
      snapshot: state.snapshot,
    };
  }
  if (action.type === "SNAPSHOT_REQUESTED") {
    if (
      action.refreshGeneration !== state.refreshGeneration ||
      !Number.isSafeInteger(action.requestToken) ||
      action.requestToken <= state.latestSnapshotRequestToken
    ) {
      return state;
    }
    return {
      ...state,
      latestSnapshotRequestToken: action.requestToken,
      needsSnapshotRefresh: true,
      pendingSnapshotRequest: {
        caseId: action.caseId,
        refreshGeneration: action.refreshGeneration,
        requestToken: action.requestToken,
      },
    };
  }
  if (action.type === "SNAPSHOT_RECEIVED") {
    const pending = state.pendingSnapshotRequest;
    if (
      pending === null ||
      action.requestToken !== pending.requestToken ||
      action.refreshGeneration !== pending.refreshGeneration ||
      state.refreshGeneration !== pending.refreshGeneration
    ) {
      return state;
    }
    if (action.snapshot.case.caseId !== pending.caseId) {
      return failClosed(
        state,
        "A snapshot response did not match the requested case. Refresh is required.",
      );
    }

    const snapshotCaseId = action.snapshot.case.caseId;
    if (state.activeCaseId !== null && state.activeCaseId !== snapshotCaseId) {
      return {
        ...INITIAL_WORKFLOW_EVENT_STORE,
        activeCaseId: snapshotCaseId,
        latestSnapshotRequestToken: state.latestSnapshotRequestToken,
        refreshGeneration: state.refreshGeneration,
        snapshot: action.snapshot,
      };
    }
    if (
      state.snapshot !== null &&
      action.snapshot.case.version < state.snapshot.case.version
    ) {
      return {
        ...state,
        needsSnapshotRefresh: true,
        pendingSnapshotRequest: null,
      };
    }
    if (
      state.snapshot !== null &&
      action.snapshot.case.version === state.snapshot.case.version &&
      snapshotAuthorityDigest(action.snapshot) !==
        snapshotAuthorityDigest(state.snapshot)
    ) {
      return failClosed(
        state,
        "Two different snapshots used the same case version. Refresh is required.",
      );
    }
    return {
      ...state,
      activeCaseId: snapshotCaseId,
      // A same-case snapshot refresh proves product state, not stream cursor
      // integrity. Only a reset or an intentional case switch discards poison.
      failedClosed: state.failedClosed,
      needsSnapshotRefresh: state.failedClosed !== null,
      pendingSnapshotRequest: null,
      snapshot: action.snapshot,
    };
  }
  if (state.failedClosed !== null) return state;
  if (action.type === "STREAM_FAILED") {
    return failClosed(state, safeFailureMessage(action.message));
  }

  const { envelope } = action;
  if (state.activeCaseId !== null && envelope.caseId !== state.activeCaseId) {
    return failClosed(
      state,
      "An event for another case reached the active stream. Refresh is required.",
    );
  }
  const digest = canonicalJson(envelope);
  if (state.lastCursor !== null) {
    if (envelope.cursor < state.lastCursor) {
      return failClosed(state, "The event stream moved backwards. Refresh is required.");
    }
    if (envelope.cursor === state.lastCursor) {
      if (digest === state.lastEnvelopeDigest) return state;
      return failClosed(state, "Two different events used the same cursor. Refresh is required.");
    }
  }

  const summary = summarizeWorkflowEvent(envelope);
  return {
    ...state,
    activeCaseId: state.activeCaseId ?? envelope.caseId,
    events: [...state.events, summary],
    lastCursor: envelope.cursor,
    lastEnvelopeDigest: digest,
    needsSnapshotRefresh: true,
    pendingSnapshotRequest: null,
    refreshGeneration: state.refreshGeneration + 1,
  };
}

export function reconnectCursor(state: WorkflowEventStore): number | null {
  return state.lastCursor;
}

export function summarizeWorkflowEvent(
  envelope: WorkflowEventEnvelope,
): WorkflowEventSummary {
  const event = envelope.event;
  switch (event.kind) {
    case "state":
      return summary(envelope, stateLabel(event.toState), stateSeverity(event.toState));
    case "gate":
      return summary(
        envelope,
        event.decision.passed
          ? `${event.decision.gateId} deterministic check passed`
          : `${event.decision.gateId} blocked the workflow: ${event.decision.reasonCodes
              .map((reason) => gateReasonLabel(reason))
              .join("; ")}`,
        event.decision.passed ? "success" : "blocked",
      );
    case "clarification":
      return summary(
        envelope,
        event.status === "requested"
          ? `Clarification round ${event.round} requested`
          : event.status === "confirmed"
            ? `Clarification round ${event.round} confirmed`
            : `Clarification round ${event.round} exhausted`,
        event.status === "exhausted" ? "blocked" : "info",
      );
    case "plan_step":
      return summary(envelope, `Plan step ${event.sequence}: ${toolLabel(event.tool)}`, "info");
    case "tool_call":
      return summary(
        envelope,
        `${toolLabel(event.tool)} ${event.status === "started" ? "started" : event.status}`,
        event.status === "blocked" ? "blocked" : event.status === "succeeded" ? "success" : "info",
      );
    case "portal_fill":
      return summary(
        envelope,
        `Sandbox draft updated (${event.writtenFields.length} fields)`,
        "info",
      );
    case "verification":
      return summary(
        envelope,
        event.status === "verified"
          ? `Verification attempt ${event.attemptNumber} passed`
          : `Verification attempt ${event.attemptNumber} ${event.status}`,
        event.status === "verified" ? "success" : "blocked",
      );
    case "retry":
      return summary(envelope, "One controlled extraction retry started", "warning");
    case "operational_failure":
      return summary(envelope, operationalFailureLabel(event.failure.category), "blocked");
    case "provider_call":
      return summary(envelope, `${operationLabel(event.operation)} provider call completed`, "success");
  }
}

export function operationalFailureLabel(category: ProviderFailureCategory): string {
  if (category === "quota_exhausted" || category === "billing_limit") {
    return "Provider quota, billing limit, or insufficient_quota reached. The externally configured €10 OpenAI project limit may be exhausted. No automatic retry will run.";
  }
  if (category === "rate_limited") {
    return "Provider rate limit reached. This failure is terminal for the current run; no automatic retry will run.";
  }
  if (category === "content_filtered") {
    return "The provider filtered the response. The run stopped without an automatic retry.";
  }
  if (category === "authentication_failed" || category === "permission_denied") {
    return "Provider access failed. The run stopped without exposing credentials or retrying.";
  }
  return "A redacted provider failure stopped the current run.";
}

export function gateReasonLabel(reason: GateReasonCode | undefined): string {
  if (reason === undefined) return "A deterministic check failed";
  const labels: Readonly<Record<GateReasonCode, string>> = {
    G0_AUDIO_TOO_LONG: "Audio exceeds the bounded duration",
    G0_CONSENT_MISSING: "Required sandbox consent is missing",
    G0_IMAGE_COUNT_INVALID: "Exactly three staged images are required",
    G0_IMAGE_TOO_LARGE: "A staged image exceeds the size limit",
    G0_IMAGE_TYPE_INVALID: "A staged image type is not allowed",
    G0_INPUT_MODE_INVALID: "The selected input mode is invalid",
    G1_EXIF_UNREVIEWED: "Image metadata was not safely reviewed",
    G1_MODEL_COPY_NOT_APPROVED: "Evidence was not approved for model use",
    G1_SENSITIVE_LOG_DATA: "Sensitive data reached a logging boundary",
    G2_OUTPUT_TRUNCATED: "The model response was incomplete",
    G2_REFERENCE_MISSING: "The model response referenced an unknown source",
    G2_REFUSAL: "The provider refused the extraction",
    G2_RETRY_EXHAUSTED: "The single controlled extraction retry was exhausted",
    G2_SCHEMA_INVALID: "The model response did not match the closed schema",
    G3_INJURY_OR_EMERGENCY: "An injury or emergency is outside this demo scope",
    G3_LEGAL_OR_LIABILITY: "Legal or liability content is outside scope",
    G3_MODEL_UNCERTAIN: "The model added a safety uncertainty block",
    G3_PAYMENT_OR_COVERAGE: "Payment or coverage advice is outside scope",
    G3_REAL_PORTAL: "Only the local sandbox portal is allowed",
    G3_SUBMISSION_ACTION: "Submission actions are forbidden to the agent",
    G4_CONFIDENCE_BELOW_THRESHOLD: "Observed evidence confidence is too low",
    G4_CONFLICTING_SOURCES: "Approved sources conflict",
    G4_FACT_NOT_WRITABLE: "A fact is not allowed in the claim draft",
    G4_NARRATIVE_UNSUPPORTED: "The narrative is not fully supported",
    G4_PROVENANCE_MISSING: "A claim fact is missing provenance",
    G4_SENSITIVE_IMAGE_INFERENCE: "A sensitive image inference was blocked",
    G5_CLARIFICATION_LIMIT: "The clarification limit was reached",
    G5_QUESTION_INVALID: "The clarification question was invalid",
    G5_REQUIRED_FIELD_MISSING: "A required claim field is missing",
    G6_ARGUMENTS_INVALID: "Tool arguments failed validation",
    G6_FORBIDDEN_ACTION: "The requested tool action is forbidden",
    G6_LIMIT_EXCEEDED: "The bounded tool limit was exceeded",
    G6_STATE_INVALID: "The tool is not allowed in the current state",
    G6_TOOL_UNKNOWN: "The requested tool is not registered",
    G6_URL_NOT_ALLOWED: "The requested URL is outside the sandbox allowlist",
    G7_ATTACHMENT_MISMATCH: "Staged attachments do not match the claim packet",
    G7_FIELD_NOT_ALLOWED: "The portal field is not writable",
    G7_FIELD_NOT_EDITABLE: "The portal field is currently locked",
    G7_PROVENANCE_MISSING: "A portal write lacks provenance",
    G7_VALUE_NOT_FROM_PACKET: "A portal value differs from the claim packet",
    G8_ATTACHMENT_MISMATCH: "Rendered attachments differ from the claim packet",
    G8_FIELD_MISMATCH: "A rendered field differs from the claim packet",
    G8_MODEL_MISMATCH: "The model verifier added a mismatch block",
    G8_REQUIRED_FIELD_MISSING: "A rendered required field is missing",
    G9_AGENT_FORBIDDEN: "The agent is forbidden from approving",
    G9_ROLE_INVALID: "The approval role is invalid",
    G9_TOKEN_INVALID: "The one-time human capability is invalid",
    G10_BEFORE_APPROVAL: "A receipt was requested before human approval",
    G10_REDACTION_FAILED: "Receipt redaction failed",
    G11_APPROVAL_ATTACK_FAILED: "Approval boundary attack tests failed",
    G11_CLEAN_CHECKOUT_FAILED: "Clean-checkout verification failed",
    G11_DETERMINISTIC_TESTS_FAILED: "Deterministic tests failed",
    G11_DOCUMENTATION_MISSING: "Required documentation is missing",
    G11_FIXTURES_MISSING: "Required staged fixtures are missing",
    G11_HUMAN_CHECKPOINT_MISSING: "A human release checkpoint is missing",
    G11_LICENSE_MISSING: "Required license information is missing",
    G11_PORTAL_SUCCESS_FAILED: "Sandbox portal success checks failed",
    G11_SAFETY_EVAL_FAILED: "Safety evaluations failed",
    G11_TEST_REPORT_MISSING: "The verification report is missing",
    G11_THRESHOLD_FAILED: "An evaluation threshold failed",
  };
  return labels[reason];
}

function summary(
  envelope: WorkflowEventEnvelope,
  label: string,
  severity: WorkflowEventSummary["severity"],
): WorkflowEventSummary {
  return {
    cursor: envelope.cursor,
    kind: envelope.event.kind,
    label,
    occurredAt: envelope.occurredAt,
    severity,
  };
}

function stateLabel(state: CaseState): string {
  const labels: Readonly<Record<CaseState, string>> = {
    abandoned: "Workflow abandoned",
    analyzing: "Evidence analysis started",
    awaiting_clarification: "One clarification is required",
    awaiting_transcript_confirmation: "Transcript confirmation is required",
    blocked: "Workflow blocked",
    created: "Case created",
    disclosed: "Sandbox disclosure accepted",
    emergency_stopped: "Emergency stop activated",
    failed: "Workflow failed",
    filling: "Sandbox draft is being filled",
    human_approved: "Human approval recorded",
    ready_to_fill: "Claim packet ready for sandbox fill",
    receipt: "Redacted sandbox receipt available",
    review: "Verified review state reached",
    verifying: "Rendered sandbox values are being verified",
  };
  return labels[state];
}

function stateSeverity(state: CaseState): WorkflowEventSummary["severity"] {
  if (["blocked", "emergency_stopped", "abandoned", "failed"].includes(state)) {
    return "blocked";
  }
  if (["review", "human_approved", "receipt"].includes(state)) return "success";
  return "info";
}

function toolLabel(tool: string): string {
  const labels: Readonly<Record<string, string>> = {
    ask_clarification: "Ask clarification",
    check_required_fields: "Check required fields",
    fill_until_review: "Fill sandbox draft",
    inspect_evidence: "Inspect approved evidence",
    inspect_form: "Inspect sandbox form",
    read_receipt: "Read redacted receipt",
    verify_rendered_fields: "Verify rendered fields",
  };
  return labels[tool] ?? "Allowed workflow tool";
}

function operationLabel(operation: string): string {
  const labels: Readonly<Record<string, string>> = {
    computer_use: "Sandbox interaction",
    extraction: "Evidence extraction",
    transcription: "Audio transcription",
    verification: "Verification",
  };
  return labels[operation] ?? "Workflow";
}

function failClosed(state: WorkflowEventStore, message: string): WorkflowEventStore {
  return {
    ...state,
    failedClosed: message,
    needsSnapshotRefresh: true,
    pendingSnapshotRequest: null,
    refreshGeneration: state.refreshGeneration + 1,
  };
}

function safeFailureMessage(message: string): string {
  return message.trim().length === 0
    ? "The redacted event stream failed. Refresh is required."
    : "The redacted event stream failed. Refresh is required.";
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    const item = value as Readonly<Record<string, unknown>>;
    return `{${Object.keys(item)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(item[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

function snapshotAuthorityDigest(snapshot: WorkflowSnapshot): string {
  // requestId identifies the transport request, not the versioned authority.
  return canonicalJson({ ...snapshot, requestId: "" });
}
