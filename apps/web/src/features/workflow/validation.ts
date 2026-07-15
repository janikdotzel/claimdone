import {
  CASE_TRANSITIONS,
  type AllowedTool,
  type AuditEventType,
  type CaseState,
  type EvidenceField,
  type GateReasonCode,
  type ProviderFailureCategory,
  type ProviderModelId,
  type RequiredClaimField,
  type VerificationState,
  type WorkflowEventEnvelope,
  type WorkflowOperation,
  type WorkflowSnapshot,
} from "../../../../../contracts/generated/claimdone";

/**
 * The generated TypeScript declarations are compile-time documentation only.
 * Every value crossing the HTTP/SSE boundary is parsed here before the UI sees it.
 * The backend remains authoritative for cross-record equality and deterministic gates;
 * this parser independently enforces closed shapes and the UI-critical state matrix.
 */
export class WorkflowPayloadError extends Error {
  constructor(readonly path: string, message: string) {
    super(`${path}: ${message}`);
    this.name = "WorkflowPayloadError";
  }
}

type JsonRecord = Record<string, unknown>;

const CONTRACT_VERSION = "4.0.0";
const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const WIRE_DATE = /^\d{4}-\d{2}-\d{2}$/;
const WIRE_TIME = /^\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?$/;
const WIRE_DATETIME =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/;
const UNSAFE_CONTROL = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/;

const CASE_STATES = [
  "created",
  "disclosed",
  "analyzing",
  "awaiting_transcript_confirmation",
  "awaiting_clarification",
  "ready_to_fill",
  "filling",
  "verifying",
  "review",
  "blocked",
  "human_approved",
  "receipt",
  "emergency_stopped",
  "abandoned",
  "failed",
] as const satisfies readonly CaseState[];

const REQUIRED_FIELDS = [
  "incident_date",
  "incident_time",
  "location",
  "claimant_name",
  "policy_reference",
  "vehicle_registration",
  "counterparty_known",
  "narrative",
  "attachments",
] as const satisfies readonly RequiredClaimField[];

const EVIDENCE_FIELDS = [
  "visible_damage",
  "collision_type",
  "vehicle_count",
  "impact_area",
  "incident_date",
  "incident_time",
  "location",
  "claimant_name",
  "policy_reference",
  "vehicle_registration",
  "counterparty_known",
  "narrative",
  "injury_status",
  "immediate_danger",
] as const satisfies readonly EvidenceField[];

const ALLOWED_TOOLS = [
  "inspect_evidence",
  "check_required_fields",
  "ask_clarification",
  "inspect_form",
  "fill_until_review",
  "verify_rendered_fields",
  "read_receipt",
] as const satisfies readonly AllowedTool[];

const GATE_REASON_CODES = [
  "G0_IMAGE_COUNT_INVALID",
  "G0_IMAGE_TYPE_INVALID",
  "G0_IMAGE_TOO_LARGE",
  "G0_INPUT_MODE_INVALID",
  "G0_AUDIO_TOO_LONG",
  "G0_CONSENT_MISSING",
  "G1_EXIF_UNREVIEWED",
  "G1_MODEL_COPY_NOT_APPROVED",
  "G1_SENSITIVE_LOG_DATA",
  "G2_SCHEMA_INVALID",
  "G2_REFUSAL",
  "G2_OUTPUT_TRUNCATED",
  "G2_REFERENCE_MISSING",
  "G2_RETRY_EXHAUSTED",
  "G3_INJURY_OR_EMERGENCY",
  "G3_REAL_PORTAL",
  "G3_LEGAL_OR_LIABILITY",
  "G3_PAYMENT_OR_COVERAGE",
  "G3_SUBMISSION_ACTION",
  "G3_MODEL_UNCERTAIN",
  "G4_PROVENANCE_MISSING",
  "G4_SENSITIVE_IMAGE_INFERENCE",
  "G4_FACT_NOT_WRITABLE",
  "G4_CONFIDENCE_BELOW_THRESHOLD",
  "G4_CONFLICTING_SOURCES",
  "G4_NARRATIVE_UNSUPPORTED",
  "G5_REQUIRED_FIELD_MISSING",
  "G5_QUESTION_INVALID",
  "G5_CLARIFICATION_LIMIT",
  "G6_TOOL_UNKNOWN",
  "G6_ARGUMENTS_INVALID",
  "G6_STATE_INVALID",
  "G6_URL_NOT_ALLOWED",
  "G6_LIMIT_EXCEEDED",
  "G6_FORBIDDEN_ACTION",
  "G7_FIELD_NOT_ALLOWED",
  "G7_VALUE_NOT_FROM_PACKET",
  "G7_PROVENANCE_MISSING",
  "G7_FIELD_NOT_EDITABLE",
  "G7_ATTACHMENT_MISMATCH",
  "G8_FIELD_MISMATCH",
  "G8_ATTACHMENT_MISMATCH",
  "G8_REQUIRED_FIELD_MISSING",
  "G8_MODEL_MISMATCH",
  "G9_AGENT_FORBIDDEN",
  "G9_ROLE_INVALID",
  "G9_TOKEN_INVALID",
  "G10_BEFORE_APPROVAL",
  "G10_REDACTION_FAILED",
  "G11_DETERMINISTIC_TESTS_FAILED",
  "G11_SAFETY_EVAL_FAILED",
  "G11_THRESHOLD_FAILED",
  "G11_PORTAL_SUCCESS_FAILED",
  "G11_APPROVAL_ATTACK_FAILED",
  "G11_CLEAN_CHECKOUT_FAILED",
  "G11_DOCUMENTATION_MISSING",
  "G11_LICENSE_MISSING",
  "G11_FIXTURES_MISSING",
  "G11_TEST_REPORT_MISSING",
  "G11_HUMAN_CHECKPOINT_MISSING",
] as const satisfies readonly GateReasonCode[];

const AUDIT_EVENT_TYPES = [
  "case_state_changed",
  "gate_decision",
  "plan_step",
  "tool_call",
  "clarification",
  "portal_fill",
  "verification",
  "retry",
  "operational_failure",
  "provider_call",
  "human_approval",
  "receipt",
  "reset",
] as const satisfies readonly AuditEventType[];

const PROVIDER_FAILURES = [
  "quota_exhausted",
  "billing_limit",
  "rate_limited",
  "timeout",
  "provider_unavailable",
  "model_not_found",
  "invalid_response",
  "content_filtered",
  "authentication_failed",
  "permission_denied",
  "invalid_request",
  "cancelled",
] as const satisfies readonly ProviderFailureCategory[];

const PROVIDER_MODELS = [
  "gpt-5.6-sol",
  "gpt-5.6-terra",
  "gpt-5.6-luna",
  "gpt-4o-transcribe",
  "claimdone-deterministic-mock",
] as const satisfies readonly ProviderModelId[];

const WORKFLOW_OPERATIONS = [
  "transcription",
  "extraction",
  "computer_use",
  "verification",
] as const satisfies readonly WorkflowOperation[];

const TERMINAL_STOP_STATES = new Set<CaseState>([
  "blocked",
  "emergency_stopped",
  "abandoned",
  "failed",
]);

const EXPECTED_AUDIT_TYPE = {
  clarification: "clarification",
  gate: "gate_decision",
  operational_failure: "operational_failure",
  plan_step: "plan_step",
  portal_fill: "portal_fill",
  provider_call: "provider_call",
  retry: "retry",
  state: "case_state_changed",
  tool_call: "tool_call",
  verification: "verification",
} as const satisfies Readonly<Record<string, AuditEventType>>;

const GATE_IDS = [
  "G0",
  "G1",
  "G2",
  "G3",
  "G4",
  "G5",
  "G6",
  "G7",
  "G8",
  "G9",
  "G10",
  "G11",
] as const;

const ANALYSIS_PLAN_TOOLS = [
  "inspect_evidence",
  "check_required_fields",
] as const satisfies readonly AllowedTool[];

const CLARIFICATION_PLAN_TOOLS = [
  ...ANALYSIS_PLAN_TOOLS,
  "ask_clarification",
] as const satisfies readonly AllowedTool[];

const SAFE_FILL_PLAN_TOOLS = [
  ...ANALYSIS_PLAN_TOOLS,
  "inspect_form",
  "fill_until_review",
  "verify_rendered_fields",
] as const satisfies readonly AllowedTool[];

const MODEL_BLOCK_REASON_BY_GATE: Readonly<Partial<Record<(typeof GATE_IDS)[number], GateReasonCode>>> = {
  G3: "G3_MODEL_UNCERTAIN",
  G8: "G8_MODEL_MISMATCH",
};

export function isKnownGateReasonCode(value: unknown): value is GateReasonCode {
  return GATE_REASON_CODES.some((reason) => value === reason);
}

export function validateGateDecisionBoundary(value: unknown, path = "$.gateDecision"): void {
  validateGateDecision(value, path);
}

export function parseWorkflowSnapshot(
  value: unknown,
  expectedCaseId?: string,
): WorkflowSnapshot {
  const root = object(value, "$", [
    "contractVersion",
    "requestId",
    "case",
    "claimPacket",
    "transcriptConfirmation",
    "clarification",
    "portalSession",
    "verificationAttempts",
    "receipt",
  ]);
  contract(root.contractVersion, "$.contractVersion");
  identifier(root.requestId, "$.requestId");
  validateCase(root.case, "$.case");
  const caseView = root.case as JsonRecord;
  const state = oneOf(caseView.state, "$.case.state", CASE_STATES);
  const caseId = identifier(caseView.caseId, "$.case.caseId");
  if (expectedCaseId !== undefined && caseId !== expectedCaseId) {
    fail("$.case.caseId", "does not match the requested case");
  }

  nullable(root.claimPacket, "$.claimPacket", validateClaimPacket);
  nullable(
    root.transcriptConfirmation,
    "$.transcriptConfirmation",
    validateTranscriptConfirmation,
  );
  nullable(root.clarification, "$.clarification", validateClarification);
  nullable(root.portalSession, "$.portalSession", validatePortalSession);
  nullable(
    root.verificationAttempts,
    "$.verificationAttempts",
    validateVerificationSeries,
  );
  nullable(root.receipt, "$.receipt", validateReceipt);
  validateSnapshotStateMatrix(root, state);

  // Identity, version, and cross-surface equality checks prevent stale or
  // contradictory route data from being rendered as authoritative UI state.
  const caseVersion = integer(caseView.version, "$.case.version", 1);
  for (const [key, versionKey] of [
    ["transcriptConfirmation", "version"],
    ["clarification", "expectedVersion"],
  ] as const) {
    const child = root[key];
    if (child !== null) {
      const childRecord = child as JsonRecord;
      if (childRecord.caseId !== caseId || childRecord[versionKey] !== caseVersion) {
        fail(`$.${key}`, "is not bound to the authoritative case identity/version");
      }
    }
  }
  for (const key of ["claimPacket", "portalSession", "verificationAttempts", "receipt"] as const) {
    const child = root[key];
    if (child !== null && (child as JsonRecord).caseId !== caseId) {
      fail(`$.${key}.caseId`, "does not match the authoritative case");
    }
  }
  if (
    root.claimPacket !== null &&
    (root.claimPacket as JsonRecord).state !== state
  ) {
    fail("$.claimPacket.state", "does not match the authoritative case state");
  }
  if (root.clarification !== null) {
    const clarification = root.clarification as JsonRecord;
    const requestedAt = Date.parse(clarification.requestedAt as string);
    const createdAt = Date.parse(caseView.createdAt as string);
    const updatedAt = Date.parse(caseView.updatedAt as string);
    if (requestedAt < createdAt || requestedAt > updatedAt) {
      fail("$.clarification.requestedAt", "must fall within the authoritative case lifetime");
    }
    const packet = root.claimPacket as JsonRecord;
    const blockingFields = deterministicClarificationFields(packet);
    if (blockingFields[0] !== clarification.field) {
      fail(
        "$.clarification.field",
        "must target the first deterministic missing or conflicting required field",
      );
    }
  }
  if (root.claimPacket !== null && root.verificationAttempts !== null) {
    validateAttemptPacketBindings(
      root.claimPacket as JsonRecord,
      root.verificationAttempts as JsonRecord,
      "$",
    );
  }

  return value as WorkflowSnapshot;
}

export function parseWorkflowEventEnvelope(
  value: unknown,
  expectedCaseId?: string,
): WorkflowEventEnvelope {
  const root = object(value, "$", [
    "contractVersion",
    "eventId",
    "caseId",
    "sourceAuditEventId",
    "sourceAuditEventType",
    "sourceAuditSequence",
    "cursor",
    "occurredAt",
    "event",
  ]);
  contract(root.contractVersion, "$.contractVersion");
  identifier(root.eventId, "$.eventId");
  const caseId = identifier(root.caseId, "$.caseId");
  if (expectedCaseId !== undefined && caseId !== expectedCaseId) {
    fail("$.caseId", "does not match the requested case");
  }
  identifier(root.sourceAuditEventId, "$.sourceAuditEventId");
  const auditType = oneOf(
    root.sourceAuditEventType,
    "$.sourceAuditEventType",
    AUDIT_EVENT_TYPES,
  );
  const sourceSequence = integer(
    root.sourceAuditSequence,
    "$.sourceAuditSequence",
    1,
  );
  const cursor = integer(root.cursor, "$.cursor", 1);
  if (cursor !== sourceSequence) {
    fail("$.cursor", "must equal sourceAuditSequence");
  }
  timestamp(root.occurredAt, "$.occurredAt");
  const kind = validateWorkflowEvent(root.event, "$.event");
  if (auditType !== EXPECTED_AUDIT_TYPE[kind]) {
    fail("$.sourceAuditEventType", "does not match the redacted workflow event kind");
  }
  return value as WorkflowEventEnvelope;
}

function validateSnapshotStateMatrix(root: JsonRecord, state: CaseState): void {
  const packet = root.claimPacket;
  const transcript = root.transcriptConfirmation;
  const clarification = root.clarification;
  const portal = root.portalSession;
  const attempts = root.verificationAttempts;
  const receipt = root.receipt;

  const packetRequired = new Set<CaseState>([
    "awaiting_clarification",
    "ready_to_fill",
    "filling",
    "verifying",
    "review",
    "human_approved",
  ]);
  if (packetRequired.has(state) && packet === null) {
    fail("$.claimPacket", `${state} requires a ClaimPacket`);
  }
  if (
    new Set<CaseState>([
      "created",
      "disclosed",
      "awaiting_transcript_confirmation",
      "receipt",
    ]).has(state) &&
    packet !== null
  ) {
    fail("$.claimPacket", `${state} cannot expose a ClaimPacket`);
  }
  if (
    (state === "awaiting_transcript_confirmation") !== (transcript !== null)
  ) {
    fail("$.transcriptConfirmation", "does not match the active case state");
  }
  if ((state === "awaiting_clarification") !== (clarification !== null)) {
    fail("$.clarification", "does not match the active case state");
  }
  if (TERMINAL_STOP_STATES.has(state) && (transcript !== null || clarification !== null)) {
    fail("$", "a terminal stop state cannot expose an active user action");
  }
  if (state === "receipt") {
    if (receipt === null) fail("$.receipt", "receipt state requires a redacted receipt");
    if ([packet, transcript, clarification, portal, attempts].some((item) => item !== null)) {
      fail("$", "receipt state may expose only the redacted receipt");
    }
    return;
  }
  if (receipt !== null) fail("$.receipt", "is allowed only in receipt state");

  const portalAllowed = new Set<CaseState>([
    "ready_to_fill",
    "filling",
    "verifying",
    "review",
    "blocked",
    "emergency_stopped",
    "abandoned",
    "failed",
  ]);
  if (portal !== null && !portalAllowed.has(state)) {
    fail("$.portalSession", `is not allowed in ${state}`);
  }
  if ((state === "verifying" || state === "review") && portal === null) {
    fail("$.portalSession", `${state} requires the rendered portal review`);
  }
  if (portal !== null) {
    const portalState = (portal as JsonRecord).state;
    if (
      ((state === "ready_to_fill" || state === "filling") && portalState !== "draft") ||
      ((state === "verifying" || state === "review") && portalState !== "review")
    ) {
      fail("$.portalSession.state", `is invalid for ${state}`);
    }
  }
  const attemptsAllowed =
    state === "verifying" || state === "review" || TERMINAL_STOP_STATES.has(state);
  if (attempts !== null && (!attemptsAllowed || packet === null)) {
    fail("$.verificationAttempts", `is not allowed in ${state}`);
  }
  if (state === "review" && attempts === null) {
    fail("$.verificationAttempts", "review requires completed verification");
  }
  if (state === "human_approved" && (portal !== null || attempts !== null)) {
    fail("$", "human_approved cannot expose an active portal or verification series");
  }
  if (packet !== null) {
    const portalState = (packet as JsonRecord).portalState;
    const expected =
      state === "human_approved"
        ? "human_approved"
        : state === "verifying" || state === "review"
          ? "review"
          : state === "analyzing" ||
              state === "awaiting_clarification" ||
              state === "ready_to_fill" ||
              state === "filling"
            ? "draft"
            : undefined;
    if (expected !== undefined && portalState !== expected) {
      fail("$.claimPacket.portalState", `must be ${expected} in ${state}`);
    }
    if (
      TERMINAL_STOP_STATES.has(state) &&
      portalState !== "draft" &&
      portalState !== "review"
    ) {
      fail("$.claimPacket.portalState", "terminal stop states allow only draft or review");
    }
  }
  if (
    portal !== null &&
    packet !== null &&
    (portal as JsonRecord).state !== (packet as JsonRecord).portalState
  ) {
    fail("$.portalSession.state", "must match ClaimPacket.portalState");
  }
  if (portal !== null && attempts !== null) {
    const finalAttempt = ((attempts as JsonRecord).attempts as readonly unknown[]).at(-1) as
      | JsonRecord
      | undefined;
    if (finalAttempt?.portalVersion !== (portal as JsonRecord).version) {
      fail("$.verificationAttempts", "final portalVersion must match the portal session");
    }
    const finalReport = finalAttempt?.report as JsonRecord | undefined;
    const portalFields = (portal as JsonRecord).fields as JsonRecord;
    if (!sameJson(finalReport?.actualAttachmentIds, portalFields.attachments)) {
      fail(
        "$.verificationAttempts",
        "final actualAttachmentIds must match the rendered portal attachments",
      );
    }
  }
  if (state === "review") {
    const packetRecord = packet as JsonRecord;
    const portalRecord = portal as JsonRecord;
    const attemptsRecord = attempts as JsonRecord;
    const claim = packetRecord.claim as JsonRecord;
    const portalFields = portalRecord.fields as JsonRecord;
    const expectedFields = {
      attachments: claim.attachments,
      claimantName: claim.claimantName,
      counterpartyKnown: claim.counterpartyKnown,
      incidentDate: claim.incidentDate,
      incidentTime: claim.incidentTime,
      location: claim.location,
      narrative: claim.narrative,
      policyReference: claim.policyReference,
      vehicleRegistration: claim.vehicleRegistration,
    };
    if (!sameJson(portalFields, expectedFields)) {
      fail("$.portalSession.fields", "must exactly equal the canonical claim values");
    }
    const series = attemptsRecord.attempts as readonly unknown[];
    const finalAttempt = series.at(-1) as JsonRecord | undefined;
    const packetG8 = (packetRecord.gateDecisions as readonly unknown[]).at(-1);
    if (
      !finalAttempt ||
      finalAttempt.final !== true ||
      finalAttempt.portalVersion !== portalRecord.version ||
      !sameJson(finalAttempt.report, packetRecord.verification) ||
      !sameJson(finalAttempt.gateDecision, packetG8)
    ) {
      fail("$.verificationAttempts", "must prove the current canonical portal and G8 result");
    }
  }
}

function validateAttemptPacketBindings(
  packet: JsonRecord,
  series: JsonRecord,
  path: string,
): void {
  const claim = packet.claim as JsonRecord;
  const expectedByField: Readonly<Record<string, unknown>> = {
    claimant_name: claim.claimantName,
    counterparty_known: claim.counterpartyKnown,
    incident_date: claim.incidentDate,
    incident_time: claim.incidentTime,
    location: claim.location,
    narrative: claim.narrative,
    policy_reference: claim.policyReference,
    vehicle_registration: claim.vehicleRegistration,
  };
  const sourcesByField = new Map(
    (claim.fieldProvenance as readonly unknown[]).map((entry) => {
      const source = entry as JsonRecord;
      return [source.field, source.sourceRefs] as const;
    }),
  );
  for (const [attemptIndex, attemptValue] of (
    series.attempts as readonly unknown[]
  ).entries()) {
    const report = (attemptValue as JsonRecord).report as JsonRecord;
    if (!sameJson(report.expectedAttachmentIds, claim.attachments)) {
      fail(
        `${path}.verificationAttempts.attempts[${attemptIndex}].report.expectedAttachmentIds`,
        "must exactly match the ordered canonical ClaimData attachments",
      );
    }
    for (const resultValue of report.fieldResults as readonly unknown[]) {
      const result = resultValue as JsonRecord;
      if (
        result.field === "attachments" ||
        !sameJson(result.expected, expectedByField[result.field as string]) ||
        !sameJson(result.sourceRefs, sourcesByField.get(result.field))
      ) {
        fail(
          `${path}.verificationAttempts.attempts[${attemptIndex}].report.fieldResults`,
          "must remain bound to canonical claim values and provenance",
        );
      }
    }
  }
}

function validateCase(value: unknown, path: string): void {
  const item = object(value, path, [
    "contractVersion",
    "caseId",
    "state",
    "version",
    "createdAt",
    "updatedAt",
  ]);
  contract(item.contractVersion, `${path}.contractVersion`);
  identifier(item.caseId, `${path}.caseId`);
  oneOf(item.state, `${path}.state`, CASE_STATES);
  integer(item.version, `${path}.version`, 1);
  const created = timestamp(item.createdAt, `${path}.createdAt`);
  const updated = timestamp(item.updatedAt, `${path}.updatedAt`);
  if (updated < created) fail(`${path}.updatedAt`, "cannot precede createdAt");
}

function validateClaimPacket(value: unknown, path: string): void {
  const item = object(value, path, [
    "contractVersion",
    "caseId",
    "state",
    "portalState",
    "scope",
    "evidence",
    "provenance",
    "facts",
    "claim",
    "plan",
    "gateDecisions",
    "verification",
  ]);
  contract(item.contractVersion, `${path}.contractVersion`);
  identifier(item.caseId, `${path}.caseId`);
  oneOf(item.state, `${path}.state`, CASE_STATES);
  oneOf(item.portalState, `${path}.portalState`, [
    "draft",
    "review",
    "human_approved",
    "receipt",
  ] as const);
  validateScope(item.scope, `${path}.scope`);
  const evidence = array(item.evidence, `${path}.evidence`, validateEvidenceItem, 4);
  const provenance = array(item.provenance, `${path}.provenance`, validateProvenance, 1);
  const facts = array(item.facts, `${path}.facts`, validateFact);
  validateClaimData(item.claim, `${path}.claim`);
  validatePlan(item.plan, `${path}.plan`);
  const gates = array(item.gateDecisions, `${path}.gateDecisions`, validateGateDecision);
  validateVerificationReport(item.verification, `${path}.verification`);
  validatePacketCrossReferences(item, evidence, provenance, facts, gates, path);
}

function validatePacketCrossReferences(
  packet: JsonRecord,
  evidence: readonly unknown[],
  provenance: readonly unknown[],
  facts: readonly unknown[],
  gates: readonly unknown[],
  path: string,
): void {
  const evidenceRecords = evidence.map((entry) => entry as JsonRecord);
  const evidenceIds = evidenceRecords.map((entry) => entry.evidenceId);
  if (new Set(evidenceIds).size !== evidenceIds.length) {
    fail(`${path}.evidence`, "evidence IDs must be unique");
  }
  const images = evidenceRecords.filter((entry) => entry.kind === "image");
  if (images.length !== 3) fail(`${path}.evidence`, "requires exactly three staged images");
  const claim = packet.claim as JsonRecord;
  const attachments = claim.attachments as readonly unknown[];
  if (!sameJson(images.map((entry) => entry.localRef), attachments)) {
    fail(`${path}.claim.attachments`, "must exactly match the ordered image localRefs");
  }

  const provenanceRecords = provenance.map((entry) => entry as JsonRecord);
  const provenanceIds = provenanceRecords.map((entry) => entry.provenanceId);
  if (new Set(provenanceIds).size !== provenanceIds.length) {
    fail(`${path}.provenance`, "provenance IDs must be unique");
  }
  const knownEvidence = new Set(evidenceIds);
  if (provenanceRecords.some((entry) => !knownEvidence.has(entry.evidenceId))) {
    fail(`${path}.provenance`, "must reference existing evidence");
  }
  const knownProvenance = new Set(provenanceIds);
  const requireKnownSources = (entries: readonly unknown[], entryPath: string): void => {
    for (const entry of entries) {
      const sources = (entry as JsonRecord).sourceRefs as readonly unknown[];
      if (sources.some((source) => !knownProvenance.has(source))) {
        fail(entryPath, "contains a source that is absent from provenance");
      }
    }
  };
  requireKnownSources(facts, `${path}.facts`);
  requireKnownSources(claim.fieldProvenance as readonly unknown[], `${path}.claim.fieldProvenance`);
  for (const gate of gates) {
    const refs = (gate as JsonRecord).evidenceRefs as readonly unknown[];
    if (refs.some((source) => !knownProvenance.has(source))) {
      fail(`${path}.gateDecisions`, "contains an unknown evidence source");
    }
  }
  const verification = packet.verification as JsonRecord;
  if (!sameJson(verification.expectedAttachmentIds, attachments)) {
    fail(
      `${path}.verification.expectedAttachmentIds`,
      "must exactly match the ordered canonical ClaimData attachments",
    );
  }
  requireKnownSources(verification.fieldResults as readonly unknown[], `${path}.verification.fieldResults`);

  const factIds = facts.map((entry) => (entry as JsonRecord).factId);
  if (new Set(factIds).size !== factIds.length) fail(`${path}.facts`, "fact IDs must be unique");

  const gateRecords = gates.map((entry) => entry as JsonRecord);
  const gateIds = gateRecords.map((entry) => entry.gateId);
  if (new Set(gateIds).size !== gateIds.length) {
    fail(`${path}.gateDecisions`, "may contain at most one decision per gate");
  }
  gateRecords.forEach((gate, index) => {
    if (gate.gateId !== GATE_IDS[index]) {
      fail(`${path}.gateDecisions[${index}].gateId`, "must form a contiguous G0-based prefix");
    }
    if (
      index > 0 &&
      Date.parse(gate.decidedAt as string) < Date.parse(gateRecords[index - 1]?.decidedAt as string)
    ) {
      fail(`${path}.gateDecisions[${index}].decidedAt`, "cannot precede the prior gate");
    }
  });

  const state = packet.state as CaseState;
  const planTools = ((packet.plan as JsonRecord).steps as readonly unknown[]).map(
    (step) => (step as JsonRecord).tool,
  );
  if (state === "awaiting_clarification") {
    assertPlanTools(planTools, CLARIFICATION_PLAN_TOOLS, path);
  } else if (
    new Set<CaseState>([
      "ready_to_fill",
      "filling",
      "verifying",
      "review",
      "human_approved",
    ]).has(state)
  ) {
    assertPlanTools(planTools, SAFE_FILL_PLAN_TOOLS, path);
  } else if (state === "analyzing") {
    assertPlanTools(planTools, ANALYSIS_PLAN_TOOLS, path);
  } else if (TERMINAL_STOP_STATES.has(state)) {
    const analysisPhasePassed =
      gateRecords.length >= 6 &&
      gateRecords.slice(0, 6).every((gate) => gate.passed === true);
    assertPlanTools(
      planTools,
      analysisPhasePassed ? SAFE_FILL_PLAN_TOOLS : ANALYSIS_PLAN_TOOLS,
      path,
    );
  }

  if (state === "awaiting_clarification") {
    if (
      gateRecords.length !== 6 ||
      gateRecords.slice(0, 4).some((gate) => gate.passed !== true)
    ) {
      fail(
        `${path}.gateDecisions`,
        "clarification requires passed G0..G3 followed by the bounded G4/G5 diagnostic path",
      );
    }
    const g4 = gateRecords[4];
    const g5 = gateRecords[5];
    const g4Passed = g4?.passed === true;
    const conflictDiagnostic =
      g4?.passed === false &&
      sameJson(g4.reasonCodes, ["G4_CONFLICTING_SOURCES"]);
    if (!g4Passed && !conflictDiagnostic) {
      fail(
        `${path}.gateDecisions[4]`,
        "only G4_CONFLICTING_SOURCES may continue to the clarification diagnostic",
      );
    }
    if (
      conflictDiagnostic &&
      requiredSupportedFactConflictFields(facts).length === 0
    ) {
      fail(
        `${path}.gateDecisions[4]`,
        "G4 conflict clarification requires a conflicting required claim field",
      );
    }
    if (!g5 || !sameJson(g5.reasonCodes, ["G5_REQUIRED_FIELD_MISSING"])) {
      fail(`${path}.gateDecisions`, "clarification requires only G5_REQUIRED_FIELD_MISSING");
    }
  } else {
    assertFailedGateTerminates(gateRecords, path);
    if (state === "ready_to_fill") {
      assertGatePrefix(gateRecords, path, 6, true);
    } else if (state === "review") {
      assertGatePrefix(gateRecords, path, 9, true);
    } else if (state === "human_approved") {
      assertGatePrefix(gateRecords, path, 10, true);
    }
  }

  const g4 = gateRecords[4];
  if (g4 !== undefined) {
    const reasons = g4.reasonCodes as readonly unknown[];
    const hasLowConfidence = facts.some((fact) => {
      const record = fact as JsonRecord;
      return (
        record.status === "observed" &&
        typeof record.confidence === "number" &&
        record.confidence < 0.8
      );
    });
    if (
      hasLowConfidence !== reasons.includes("G4_CONFIDENCE_BELOW_THRESHOLD")
    ) {
      fail(
        `${path}.gateDecisions[4]`,
        "G4 confidence reason must match the deterministic 0.8 fact threshold",
      );
    }
    const hasConflict = supportedFactConflictFields(facts).size > 0;
    if (hasConflict !== reasons.includes("G4_CONFLICTING_SOURCES")) {
      fail(
        `${path}.gateDecisions[4]`,
        "G4 conflict reason must match the complete supported fact inventory",
      );
    }
  }

  const fieldSources = new Map(
    (claim.fieldProvenance as readonly unknown[]).map((entry) => {
      const source = entry as JsonRecord;
      return [source.field, source.sourceRefs] as const;
    }),
  );
  const claimValueByField: Readonly<Record<string, unknown>> = {
    claimant_name: claim.claimantName,
    counterparty_known: claim.counterpartyKnown,
    incident_date: claim.incidentDate,
    incident_time: claim.incidentTime,
    location: claim.location,
    narrative: claim.narrative,
    policy_reference: claim.policyReference,
    vehicle_registration: claim.vehicleRegistration,
  };
  for (const resultValue of verification.fieldResults as readonly unknown[]) {
    const result = resultValue as JsonRecord;
    if (result.field === "attachments") fail(`${path}.verification.fieldResults`, "attachments use ID verification");
    if (!sameJson(result.expected, claimValueByField[result.field as string])) {
      fail(`${path}.verification.fieldResults`, "expected values must match canonical ClaimData");
    }
    if (!sameJson(result.sourceRefs, fieldSources.get(result.field))) {
      fail(`${path}.verification.fieldResults`, "sourceRefs must match claim field provenance");
    }
  }
  if (
    ["review", "human_approved"].includes(state) &&
    (claim.missingRequiredFields as readonly unknown[]).length > 0
  ) {
    fail(`${path}.claim.missingRequiredFields`, "review and later states require complete claim data");
  }
  if (["review", "human_approved"].includes(state) && verification.reviewAllowed !== true) {
    fail(`${path}.verification`, "review and later states require successful verification");
  }
}

function supportedFactConflictFields(
  facts: readonly unknown[],
): ReadonlySet<EvidenceField> {
  const valuesByField = new Map<EvidenceField, unknown[]>();
  const conflicts = new Set<EvidenceField>();
  for (const factValue of facts) {
    const fact = factValue as JsonRecord;
    if (fact.status !== "observed" && fact.status !== "user_stated") continue;
    const field = fact.field as EvidenceField;
    const values = valuesByField.get(field) ?? [];
    if (!values.some((value) => sameJson(value, fact.value))) {
      values.push(fact.value);
      valuesByField.set(field, values);
      if (values.length > 1) conflicts.add(field);
    }
  }
  return conflicts;
}

function requiredSupportedFactConflictFields(
  facts: readonly unknown[],
): readonly RequiredClaimField[] {
  const conflicts = supportedFactConflictFields(facts);
  return REQUIRED_FIELDS.filter(
    (field) => field !== "attachments" && conflicts.has(field),
  );
}

function deterministicClarificationFields(
  packet: JsonRecord,
): readonly RequiredClaimField[] {
  const claim = packet.claim as JsonRecord;
  const missing = new Set(
    claim.missingRequiredFields as readonly RequiredClaimField[],
  );
  const conflicts = new Set(
    requiredSupportedFactConflictFields(packet.facts as readonly unknown[]),
  );
  return REQUIRED_FIELDS.filter(
    (field) => missing.has(field) || conflicts.has(field),
  );
}

function assertFailedGateTerminates(
  gates: readonly JsonRecord[],
  path: string,
): void {
  const intermediateFailure = gates
    .slice(0, -1)
    .findIndex((gate) => gate.passed !== true);
  if (intermediateFailure !== -1) {
    fail(
      `${path}.gateDecisions[${intermediateFailure}]`,
      "a failed gate must terminate this state's history",
    );
  }
}

function assertPlanTools(
  actual: readonly unknown[],
  expected: readonly AllowedTool[],
  path: string,
): void {
  if (!sameJson(actual, expected)) {
    fail(
      `${path}.plan.steps`,
      `requires the exact tool sequence: ${expected.join(", ")}`,
    );
  }
}

function assertGatePrefix(
  gates: readonly JsonRecord[],
  path: string,
  length: number,
  finalPassed: boolean,
): void {
  if (gates.length !== length || gates.slice(0, -1).some((gate) => gate.passed !== true)) {
    fail(`${path}.gateDecisions`, `requires the exact G0..G${length - 1} gate sequence`);
  }
  if (gates.at(-1)?.passed !== finalPassed) {
    fail(`${path}.gateDecisions`, `requires G${length - 1} passed=${String(finalPassed)}`);
  }
}

function validateScope(value: unknown, path: string): void {
  const item = object(value, path, [
    "environment",
    "scenario",
    "agentCanSubmit",
    "finalActionOwner",
  ]);
  literal(item.environment, `${path}.environment`, "sandbox");
  literal(item.scenario, `${path}.scenario`, "two_vehicle_rear_end_no_injury");
  literal(item.agentCanSubmit, `${path}.agentCanSubmit`, false);
  literal(item.finalActionOwner, `${path}.finalActionOwner`, "human");
}

function validateEvidenceItem(value: unknown, path: string): void {
  const item = object(
    value,
    path,
    ["evidenceId", "kind", "localRef", "mediaType", "modelCopyApproved", "sha256", "text"],
    ["transcriptConfirmed"],
  );
  identifier(item.evidenceId, `${path}.evidenceId`);
  oneOf(item.kind, `${path}.kind`, [
    "image",
    "user_statement",
    "transcript",
    "clarification",
  ] as const);
  attachmentIdentifier(item.localRef, `${path}.localRef`);
  oneOf(item.mediaType, `${path}.mediaType`, [
    "image/jpeg",
    "image/png",
    "text/plain",
  ] as const);
  boolean(item.modelCopyApproved, `${path}.modelCopyApproved`);
  digest(item.sha256, `${path}.sha256`);
  nullable(item.text, `${path}.text`, (entry, entryPath) => {
    safeText(entry, entryPath, 1, 4_000);
  });
  if (Object.hasOwn(item, "transcriptConfirmed")) {
    nullable(item.transcriptConfirmed, `${path}.transcriptConfirmed`, boolean);
  }
  if (item.kind === "image") {
    if (!(["image/jpeg", "image/png"] as const).includes(item.mediaType as "image/jpeg" | "image/png") || item.text !== null) {
      fail(path, "image evidence must use an image media type and contain no text");
    }
  } else if (item.mediaType !== "text/plain" || item.text === null) {
    fail(path, "text evidence must use text/plain and contain text");
  }
  if (item.kind !== "transcript" && Object.hasOwn(item, "transcriptConfirmed") && item.transcriptConfirmed !== null) {
    fail(`${path}.transcriptConfirmed`, "is allowed only for transcript evidence");
  }
  if (item.kind === "transcript" && item.transcriptConfirmed !== true) {
    fail(`${path}.transcriptConfirmed`, "transcript evidence requires human confirmation");
  }
}

function validateProvenance(value: unknown, path: string): void {
  const item = object(value, path, [
    "provenanceId",
    "evidenceId",
    "locator",
    "userConfirmed",
  ]);
  identifier(item.provenanceId, `${path}.provenanceId`);
  identifier(item.evidenceId, `${path}.evidenceId`);
  nullable(item.locator, `${path}.locator`, (entry, entryPath) => {
    safeText(entry, entryPath, 1, 4_000);
  });
  boolean(item.userConfirmed, `${path}.userConfirmed`);
}

function validateFact(value: unknown, path: string): void {
  const item = object(value, path, [
    "factId",
    "field",
    "value",
    "status",
    "sourceRefs",
    "confidence",
  ]);
  identifier(item.factId, `${path}.factId`);
  oneOf(item.field, `${path}.field`, EVIDENCE_FIELDS);
  scalar(item.value, `${path}.value`);
  oneOf(item.status, `${path}.status`, [
    "observed",
    "user_stated",
    "unknown",
    "not_supported",
  ] as const);
  array(item.sourceRefs, `${path}.sourceRefs`, identifier);
  nullable(item.confidence, `${path}.confidence`, (entry, entryPath) => {
    finiteNumber(entry, entryPath, 0, 1);
  });
  const supported = item.status === "observed" || item.status === "user_stated";
  const sourceRefs = item.sourceRefs as readonly unknown[];
  if (supported && (item.value === null || sourceRefs.length === 0)) {
    fail(path, "supported facts require a value and provenance");
  }
  if ((item.status === "unknown" || item.status === "not_supported") && (item.value !== null || item.confidence !== null)) {
    fail(path, "unsupported facts cannot carry a value or confidence");
  }
  if ((item.status === "observed") !== (item.confidence !== null)) {
    fail(`${path}.confidence`, "is required only for observed facts");
  }
}

function validateClaimData(value: unknown, path: string): void {
  const item = object(value, path, [
    "attachments",
    "claimantName",
    "counterpartyKnown",
    "fieldProvenance",
    "incidentDate",
    "incidentTime",
    "location",
    "missingRequiredFields",
    "narrative",
    "policyReference",
    "vehicleRegistration",
  ]);
  attachmentIdentifiers(item.attachments, `${path}.attachments`, 3, 3);
  for (const key of [
    "claimantName",
    "location",
    "narrative",
    "policyReference",
    "vehicleRegistration",
  ] as const) {
    nullable(item[key], `${path}.${key}`, (entry, entryPath) => {
      safeText(entry, entryPath, 1, 4_000);
    });
  }
  nullable(item.incidentDate, `${path}.incidentDate`, wireDate);
  nullable(item.incidentTime, `${path}.incidentTime`, wireTime);
  oneOf(item.counterpartyKnown, `${path}.counterpartyKnown`, [
    "yes",
    "no",
    "unknown",
  ] as const);
  const fieldProvenance = array(item.fieldProvenance, `${path}.fieldProvenance`, (entry, entryPath) => {
    const source = object(entry, entryPath, ["field", "sourceRefs"]);
    oneOf(source.field, `${entryPath}.field`, REQUIRED_FIELDS);
    array(source.sourceRefs, `${entryPath}.sourceRefs`, identifier, 1);
  });
  const missing = array(item.missingRequiredFields, `${path}.missingRequiredFields`, (entry, entryPath) => {
    oneOf(entry, entryPath, REQUIRED_FIELDS);
  });
  if (new Set(missing).size !== missing.length) fail(`${path}.missingRequiredFields`, "must be unique");
  const expectedMissing = new Set<RequiredClaimField>();
  const nullableFields = {
    claimant_name: item.claimantName,
    incident_date: item.incidentDate,
    incident_time: item.incidentTime,
    location: item.location,
    narrative: item.narrative,
    policy_reference: item.policyReference,
    vehicle_registration: item.vehicleRegistration,
  } as const;
  for (const [field, fieldValue] of Object.entries(nullableFields)) {
    if (fieldValue === null) expectedMissing.add(field as RequiredClaimField);
  }
  if (!sameStringSet(missing, expectedMissing)) {
    fail(`${path}.missingRequiredFields`, "must exactly match null required fields");
  }
  const provenanceFields = fieldProvenance.map((entry) => (entry as JsonRecord).field);
  if (new Set(provenanceFields).size !== provenanceFields.length) {
    fail(`${path}.fieldProvenance`, "may contain each claim field only once");
  }
  const expectedProvenance = REQUIRED_FIELDS.filter((field) => !expectedMissing.has(field));
  if (!sameStringSet(provenanceFields, new Set(expectedProvenance))) {
    fail(`${path}.fieldProvenance`, "must cover every populated required field exactly once");
  }
}

function validatePlan(value: unknown, path: string): void {
  const item = object(value, path, ["agentCanSubmit", "steps"]);
  literal(item.agentCanSubmit, `${path}.agentCanSubmit`, false);
  const steps = array(item.steps, `${path}.steps`, (entry, entryPath) => {
    const step = object(entry, entryPath, ["reason", "sequence", "tool"]);
    safeText(step.reason, `${entryPath}.reason`, 1, 512);
    integer(step.sequence, `${entryPath}.sequence`, 1, 40);
    oneOf(step.tool, `${entryPath}.tool`, ALLOWED_TOOLS);
  }, 1, 40);
  steps.forEach((entry, index) => {
    if ((entry as JsonRecord).sequence !== index + 1) {
      fail(`${path}.steps[${index}].sequence`, "must be contiguous and one-based");
    }
  });
}

function validateGateDecision(value: unknown, path: string): void {
  const item = object(value, path, [
    "contractVersion",
    "decidedAt",
    "deterministicPassed",
    "evidenceRefs",
    "gateId",
    "modelBlocked",
    "passed",
    "reasonCodes",
  ]);
  contract(item.contractVersion, `${path}.contractVersion`);
  timestamp(item.decidedAt, `${path}.decidedAt`);
  boolean(item.deterministicPassed, `${path}.deterministicPassed`);
  array(item.evidenceRefs, `${path}.evidenceRefs`, identifier);
  const gateId = oneOf(item.gateId, `${path}.gateId`, GATE_IDS);
  boolean(item.modelBlocked, `${path}.modelBlocked`);
  boolean(item.passed, `${path}.passed`);
  const reasons = array(item.reasonCodes, `${path}.reasonCodes`, (entry, entryPath) => {
    oneOf(entry, entryPath, GATE_REASON_CODES);
  });
  if (new Set(reasons).size !== reasons.length) fail(`${path}.reasonCodes`, "must be unique");
  if (reasons.some((reason) => typeof reason !== "string" || !reason.startsWith(`${gateId}_`))) {
    fail(`${path}.reasonCodes`, "must belong to the selected gate");
  }
  const modelReason = MODEL_BLOCK_REASON_BY_GATE[gateId];
  const modelReasonPresent = modelReason !== undefined && reasons.includes(modelReason);
  const deterministicReasons = reasons.filter((reason) => reason !== modelReason);
  const deterministicPassed = deterministicReasons.length === 0;
  if (item.deterministicPassed !== deterministicPassed) {
    fail(`${path}.deterministicPassed`, "must be derived from non-model reason codes");
  }
  if (item.modelBlocked !== modelReasonPresent) {
    fail(`${path}.modelBlocked`, "must be derived from the gate's model-only reason");
  }
  if (item.passed !== (deterministicPassed && !modelReasonPresent)) {
    fail(`${path}.passed`, "must equal deterministicPassed AND NOT modelBlocked");
  }
  if ((item.passed && reasons.length !== 0) || (!item.passed && reasons.length === 0)) {
    fail(`${path}.reasonCodes`, "must be empty exactly when the gate passed");
  }
}

function validateVerificationReport(value: unknown, path: string): void {
  const item = object(value, path, [
    "actualAttachmentCount",
    "actualAttachmentIds",
    "deterministicMatch",
    "expectedAttachmentCount",
    "expectedAttachmentIds",
    "fieldResults",
    "modelReportedMismatch",
    "reviewAllowed",
    "status",
    "verifiedAt",
  ]);
  nullable(item.actualAttachmentCount, `${path}.actualAttachmentCount`, (entry, entryPath) => {
    integer(entry, entryPath, 0, 3);
  });
  nullable(item.actualAttachmentIds, `${path}.actualAttachmentIds`, (entry, entryPath) => {
    attachmentIdentifiers(entry, entryPath, 0, 3);
  });
  nullable(item.deterministicMatch, `${path}.deterministicMatch`, boolean);
  literal(item.expectedAttachmentCount, `${path}.expectedAttachmentCount`, 3);
  const expectedAttachmentIds = attachmentIdentifiers(
    item.expectedAttachmentIds,
    `${path}.expectedAttachmentIds`,
    3,
    3,
  );
  const fields = array(item.fieldResults, `${path}.fieldResults`, validateVerificationField);
  boolean(item.modelReportedMismatch, `${path}.modelReportedMismatch`);
  boolean(item.reviewAllowed, `${path}.reviewAllowed`);
  oneOf(item.status, `${path}.status`, [
    "pending",
    "verified",
    "mismatch",
    "blocked",
  ] as const satisfies readonly VerificationState[]);
  nullable(item.verifiedAt, `${path}.verifiedAt`, timestamp);
  const fieldNames = fields.map((field) => (field as JsonRecord).field);
  if (new Set(fieldNames).size !== fieldNames.length) fail(`${path}.fieldResults`, "must be unique by field");
  const requiredScalarFields = REQUIRED_FIELDS.filter((field) => field !== "attachments");
  const fieldsComplete = sameStringSet(fieldNames, new Set(requiredScalarFields));
  const fieldsMatch = fieldsComplete && fields.every((field) => (field as JsonRecord).status === "match");
  if (expectedAttachmentIds.length !== item.expectedAttachmentCount) {
    fail(`${path}.expectedAttachmentCount`, "must equal expectedAttachmentIds length");
  }
  const actualCountPresent = item.actualAttachmentCount !== null;
  const actualIdsPresent = item.actualAttachmentIds !== null;
  if (actualCountPresent !== actualIdsPresent) {
    fail(path, "actualAttachmentCount and actualAttachmentIds must be jointly set");
  }
  const actualAttachmentIds = item.actualAttachmentIds as readonly unknown[] | null;
  if (
    actualAttachmentIds !== null &&
    item.actualAttachmentCount !== actualAttachmentIds.length
  ) {
    fail(`${path}.actualAttachmentCount`, "must equal actualAttachmentIds length");
  }
  const attachmentsEvaluated = actualIdsPresent;
  const attachmentsMatch = sameJson(actualAttachmentIds, expectedAttachmentIds);
  const fieldMismatch = fields.some((field) => (field as JsonRecord).status !== "match");
  const attachmentMismatch = attachmentsEvaluated && !attachmentsMatch;
  if (fieldsComplete && attachmentsEvaluated) {
    if (item.deterministicMatch !== (fieldsMatch && attachmentsMatch)) {
      fail(`${path}.deterministicMatch`, "must be derived from all fields and attachments");
    }
  } else if (item.deterministicMatch === true) {
    fail(`${path}.deterministicMatch`, "cannot pass partial verification");
  } else if (item.deterministicMatch === false && !fieldMismatch && !attachmentMismatch) {
    fail(`${path}.deterministicMatch`, "partial verification can fail only after a mismatch");
  } else if (item.deterministicMatch === null && (fieldMismatch || attachmentMismatch)) {
    fail(`${path}.deterministicMatch`, "must record an observed mismatch as false");
  }
  const reviewAllowed = item.status === "verified" && item.deterministicMatch === true && item.modelReportedMismatch === false && fieldsMatch && attachmentsMatch;
  if (item.reviewAllowed !== reviewAllowed) fail(`${path}.reviewAllowed`, "must be derived from deterministic verification");
  if (item.status === "pending") {
    if (item.deterministicMatch !== null || item.verifiedAt !== null || fields.length !== 0 || item.actualAttachmentCount !== null || item.actualAttachmentIds !== null || item.modelReportedMismatch !== false) {
      fail(path, "pending verification cannot contain results, signals, or a timestamp");
    }
  } else if (item.verifiedAt === null) {
    fail(`${path}.verifiedAt`, "is required for completed verification");
  }
  if (item.status === "verified" && !reviewAllowed) fail(path, "verified status requires every check to pass");
  if (item.status === "mismatch" && !(item.deterministicMatch === false || item.modelReportedMismatch || fieldMismatch || attachmentMismatch)) {
    fail(path, "mismatch status requires a mismatch signal");
  }
}

function validateVerificationField(value: unknown, path: string): void {
  const item = object(value, path, ["actual", "expected", "field", "sourceRefs", "status"]);
  scalar(item.actual, `${path}.actual`);
  scalar(item.expected, `${path}.expected`);
  oneOf(item.field, `${path}.field`, REQUIRED_FIELDS);
  array(item.sourceRefs, `${path}.sourceRefs`, identifier, 1);
  const status = oneOf(item.status, `${path}.status`, ["match", "mismatch", "missing"] as const);
  const valuesMatch = typeof item.actual === typeof item.expected && item.actual === item.expected;
  if (status === "match" && !valuesMatch) fail(path, "match requires equal values of the same type");
  if (status === "mismatch" && valuesMatch) fail(path, "mismatch requires different values");
  if (status === "missing" && (item.actual !== null || item.expected === null)) {
    fail(path, "missing requires a non-null expected value and null actual");
  }
}

function validateTranscriptConfirmation(value: unknown, path: string): void {
  const item = object(value, path, [
    "contractVersion",
    "caseId",
    "confirmed",
    "text",
    "transcriptId",
    "transcriptSha256",
    "version",
  ]);
  contract(item.contractVersion, `${path}.contractVersion`);
  identifier(item.caseId, `${path}.caseId`);
  literal(item.confirmed, `${path}.confirmed`, false);
  safeText(item.text, `${path}.text`, 1, 4_000);
  identifier(item.transcriptId, `${path}.transcriptId`);
  digest(item.transcriptSha256, `${path}.transcriptSha256`);
  integer(item.version, `${path}.version`, 1);
}

function validateClarification(value: unknown, path: string): void {
  const item = object(value, path, [
    "contractVersion",
    "caseId",
    "clarificationId",
    "expectedVersion",
    "field",
    "question",
    "requestedAt",
    "round",
    "status",
  ]);
  contract(item.contractVersion, `${path}.contractVersion`);
  identifier(item.caseId, `${path}.caseId`);
  identifier(item.clarificationId, `${path}.clarificationId`);
  integer(item.expectedVersion, `${path}.expectedVersion`, 1);
  oneOf(item.field, `${path}.field`, REQUIRED_FIELDS);
  safeText(item.question, `${path}.question`, 1, 512);
  timestamp(item.requestedAt, `${path}.requestedAt`);
  oneOf(item.round, `${path}.round`, [1, 2, 3] as const);
  literal(item.status, `${path}.status`, "requested");
}

function validatePortalSession(value: unknown, path: string): void {
  const item = object(
    value,
    path,
    ["contractVersion", "caseId", "fields", "state", "updatedAt", "variant", "version"],
    ["auditCount"],
  );
  contract(item.contractVersion, `${path}.contractVersion`);
  identifier(item.caseId, `${path}.caseId`);
  validatePortalFields(item.fields, `${path}.fields`);
  oneOf(item.state, `${path}.state`, ["draft", "review"] as const);
  timestamp(item.updatedAt, `${path}.updatedAt`);
  oneOf(item.variant, `${path}.variant`, ["A", "B"] as const);
  integer(item.version, `${path}.version`, 1);
  if (Object.hasOwn(item, "auditCount")) {
    nullable(item.auditCount, `${path}.auditCount`, (entry, entryPath) => {
      integer(entry, entryPath, 0);
    });
  }
}

function validatePortalFields(value: unknown, path: string): void {
  const item = object(value, path, [
    "attachments",
    "claimantName",
    "counterpartyKnown",
    "incidentDate",
    "incidentTime",
    "location",
    "narrative",
    "policyReference",
    "vehicleRegistration",
  ]);
  attachmentIdentifiers(item.attachments, `${path}.attachments`, 0, 3);
  safeText(item.claimantName, `${path}.claimantName`, 0, 512);
  safeText(item.incidentDate, `${path}.incidentDate`, 0, 10);
  safeText(item.incidentTime, `${path}.incidentTime`, 0, 21);
  safeText(item.location, `${path}.location`, 0, 512);
  safeText(item.narrative, `${path}.narrative`, 0, 4_000);
  safeText(item.policyReference, `${path}.policyReference`, 0, 512);
  safeText(item.vehicleRegistration, `${path}.vehicleRegistration`, 0, 512);
  oneOf(item.counterpartyKnown, `${path}.counterpartyKnown`, [
    "",
    "yes",
    "no",
    "unknown",
  ] as const);
}

function validateVerificationSeries(value: unknown, path: string): void {
  const item = object(value, path, ["attempts", "caseId", "contractVersion"]);
  contract(item.contractVersion, `${path}.contractVersion`);
  identifier(item.caseId, `${path}.caseId`);
  const attempts = array(item.attempts, `${path}.attempts`, validateVerificationAttempt, 1, 2);
  const attemptIds = attempts.map((attempt) => (attempt as JsonRecord).attemptId);
  if (new Set(attemptIds).size !== attemptIds.length) {
    fail(`${path}.attempts`, "attempt IDs must be unique");
  }
  attempts.forEach((attempt, index) => {
    const attemptRecord = attempt as JsonRecord;
    if (attemptRecord.caseId !== item.caseId || attemptRecord.attemptNumber !== index + 1) {
      fail(`${path}.attempts[${index}]`, "is not a contiguous case-bound attempt");
    }
  });

  const first = attempts[0] as JsonRecord;
  if (attempts.length === 1) {
    if (first.final !== true) {
      fail(`${path}.attempts[0]`, "a non-final first attempt requires its repaired second attempt");
    }
    return;
  }

  const second = attempts[1] as JsonRecord;
  const repair = first.repair as JsonRecord | null;
  if (first.final === true || repair === null) {
    fail(`${path}.attempts`, "a second attempt requires a non-final repair authorization");
  }
  if (second.repairedFromAttemptId !== first.attemptId) {
    fail(`${path}.attempts[1].repairedFromAttemptId`, "must reference attempt one");
  }
  if (second.portalVersion !== repair.toPortalVersion) {
    fail(`${path}.attempts[1].portalVersion`, "must verify the authorized repaired portal version");
  }
  const firstReport = first.report as JsonRecord;
  const secondReport = second.report as JsonRecord;
  const firstVerifiedAt = Date.parse(firstReport.verifiedAt as string);
  const secondVerifiedAt = Date.parse(secondReport.verifiedAt as string);
  if (secondVerifiedAt <= firstVerifiedAt) {
    fail(`${path}.attempts[1].report.verifiedAt`, "must follow the first verification attempt");
  }

  const firstResults = new Map(
    (firstReport.fieldResults as readonly unknown[]).map((result) => {
      const record = result as JsonRecord;
      return [record.field, record] as const;
    }),
  );
  const secondResults = new Map(
    (secondReport.fieldResults as readonly unknown[]).map((result) => {
      const record = result as JsonRecord;
      return [record.field, record] as const;
    }),
  );
  if (!sameStringSet([...firstResults.keys()], new Set(secondResults.keys()))) {
    fail(`${path}.attempts[1].report.fieldResults`, "must compare the same field set");
  }
  for (const [field, firstResult] of firstResults) {
    const secondResult = secondResults.get(field);
    if (
      secondResult === undefined ||
      typeof firstResult.expected !== typeof secondResult.expected ||
      !sameJson(firstResult.expected, secondResult.expected) ||
      !sameJson(firstResult.sourceRefs, secondResult.sourceRefs)
    ) {
      fail(`${path}.attempts[1].report.fieldResults`, "cannot change expected values or provenance");
    }
    if (field !== repair.field && !sameJson(firstResult, secondResult)) {
      fail(`${path}.attempts[1].report.fieldResults`, "cannot change a non-target rendered field");
    }
  }
  if (
    firstReport.expectedAttachmentCount !== secondReport.expectedAttachmentCount ||
    firstReport.actualAttachmentCount !== secondReport.actualAttachmentCount ||
    !sameJson(firstReport.expectedAttachmentIds, secondReport.expectedAttachmentIds) ||
    !sameJson(firstReport.actualAttachmentIds, secondReport.actualAttachmentIds)
  ) {
    fail(`${path}.attempts[1].report`, "a scalar repair cannot change attachment verification");
  }
}

function validateVerificationAttempt(value: unknown, path: string): void {
  const item = object(value, path, [
    "attemptId",
    "attemptNumber",
    "caseId",
    "caseState",
    "contractVersion",
    "final",
    "gateDecision",
    "portalVersion",
    "repair",
    "repairedFromAttemptId",
    "report",
  ]);
  identifier(item.attemptId, `${path}.attemptId`);
  oneOf(item.attemptNumber, `${path}.attemptNumber`, [1, 2] as const);
  identifier(item.caseId, `${path}.caseId`);
  literal(item.caseState, `${path}.caseState`, "verifying");
  contract(item.contractVersion, `${path}.contractVersion`);
  boolean(item.final, `${path}.final`);
  nullable(item.gateDecision, `${path}.gateDecision`, validateGateDecision);
  integer(item.portalVersion, `${path}.portalVersion`, 1);
  nullable(item.repair, `${path}.repair`, validateRepair);
  nullable(item.repairedFromAttemptId, `${path}.repairedFromAttemptId`, identifier);
  validateVerificationReport(item.report, `${path}.report`);
  const report = item.report as JsonRecord;
  if (report.status === "pending") {
    fail(`${path}.report`, "a verification attempt requires a completed report");
  }
  if (item.final !== (item.gateDecision !== null)) {
    fail(path, "only a final verification attempt may carry G8");
  }
  if (item.final === true) {
    const gate = item.gateDecision as JsonRecord;
    if (gate.gateId !== "G8") {
      fail(`${path}.gateDecision.gateId`, "only G8 may finalize verification");
    }
    if (gate.passed !== report.reviewAllowed) {
      fail(`${path}.gateDecision.passed`, "must equal report.reviewAllowed");
    }
    if (
      report.verifiedAt !== null &&
      Date.parse(gate.decidedAt as string) < Date.parse(report.verifiedAt as string)
    ) {
      fail(`${path}.gateDecision.decidedAt`, "cannot precede the verification report");
    }
    const expectedReasons = deriveG8ReasonCodes(report);
    if (!sameJson(gate.reasonCodes, expectedReasons)) {
      fail(`${path}.gateDecision.reasonCodes`, "must be derived exactly from the report");
    }
    if (report.reviewAllowed !== true && expectedReasons.length === 0) {
      fail(`${path}.gateDecision`, "a failed final report requires a derived G8 reason");
    }
  }

  if (item.attemptNumber === 1) {
    if (item.repairedFromAttemptId !== null) {
      fail(`${path}.repairedFromAttemptId`, "attempt one cannot reference an earlier attempt");
    }
  } else if (
    item.repairedFromAttemptId === null ||
    item.repair !== null ||
    item.final !== true
  ) {
    fail(path, "attempt two must be final, reference attempt one, and authorize no further repair");
  }

  if (item.repair === null) {
    if (item.final !== true) {
      fail(path, "a non-final attempt requires one narrow repair authorization");
    }
    return;
  }

  const repair = item.repair as JsonRecord;
  const results = report.fieldResults as readonly unknown[];
  const nonMatching = results.filter(
    (result) => (result as JsonRecord).status !== "match",
  );
  const expectedFields = REQUIRED_FIELDS.filter((field) => field !== "attachments");
  if (
    item.attemptNumber !== 1 ||
    item.final === true ||
    report.status !== "mismatch" ||
    report.deterministicMatch !== false ||
    report.modelReportedMismatch !== false ||
    !sameJson(report.actualAttachmentIds, report.expectedAttachmentIds) ||
    !sameStringSet(
      results.map((result) => (result as JsonRecord).field),
      new Set(expectedFields),
    ) ||
    nonMatching.length !== 1
  ) {
    fail(path, "repair requires exactly one complete deterministic scalar mismatch on attempt one");
  }
  const mismatching = nonMatching[0] as JsonRecord;
  if (
    mismatching.field !== repair.field ||
    !sameJson(mismatching.sourceRefs, repair.sourceRefs) ||
    repair.fromPortalVersion !== item.portalVersion
  ) {
    fail(`${path}.repair`, "must target the sole mismatch, its provenance, and current portal version");
  }
}

function deriveG8ReasonCodes(report: JsonRecord): GateReasonCode[] {
  const results = report.fieldResults as readonly unknown[];
  const presentFields = results.map((result) => (result as JsonRecord).field);
  const expectedFields = REQUIRED_FIELDS.filter((field) => field !== "attachments");
  const reasons: GateReasonCode[] = [];
  if (results.some((result) => (result as JsonRecord).status === "mismatch")) {
    reasons.push("G8_FIELD_MISMATCH");
  }
  if (
    report.actualAttachmentIds !== null &&
    !sameJson(report.actualAttachmentIds, report.expectedAttachmentIds)
  ) {
    reasons.push("G8_ATTACHMENT_MISMATCH");
  }
  if (
    !sameStringSet(presentFields, new Set(expectedFields)) ||
    results.some((result) => (result as JsonRecord).status === "missing") ||
    report.actualAttachmentCount === null ||
    report.actualAttachmentIds === null
  ) {
    reasons.push("G8_REQUIRED_FIELD_MISSING");
  }
  if (report.modelReportedMismatch === true) reasons.push("G8_MODEL_MISMATCH");
  return reasons;
}

function validateRepair(value: unknown, path: string): void {
  const item = object(value, path, [
    "field",
    "fromPortalVersion",
    "repairNumber",
    "sourceRefs",
    "toPortalVersion",
  ]);
  oneOf(item.field, `${path}.field`, REQUIRED_FIELDS.filter((field) => field !== "attachments"));
  const from = integer(item.fromPortalVersion, `${path}.fromPortalVersion`, 1);
  literal(item.repairNumber, `${path}.repairNumber`, 1);
  const sources = array(item.sourceRefs, `${path}.sourceRefs`, identifier, 1);
  if (new Set(sources).size !== sources.length) {
    fail(`${path}.sourceRefs`, "cannot contain duplicates");
  }
  const to = integer(item.toPortalVersion, `${path}.toPortalVersion`, 2);
  if (to !== from + 1) fail(`${path}.toPortalVersion`, "must increment exactly once");
}

function validateReceipt(value: unknown, path: string): void {
  const item = object(value, path, [
    "approvalId",
    "approvedAt",
    "caseId",
    "contractVersion",
    "environment",
    "humanApproved",
    "receiptId",
    "redacted",
    "renderedAt",
    "sandboxOnly",
    "state",
    "submittedToRealInsurer",
    "summary",
    "variant",
    "version",
  ]);
  identifier(item.approvalId, `${path}.approvalId`);
  const approvedAt = timestamp(item.approvedAt, `${path}.approvedAt`);
  identifier(item.caseId, `${path}.caseId`);
  contract(item.contractVersion, `${path}.contractVersion`);
  literal(item.environment, `${path}.environment`, "sandbox");
  literal(item.humanApproved, `${path}.humanApproved`, true);
  identifier(item.receiptId, `${path}.receiptId`);
  literal(item.redacted, `${path}.redacted`, true);
  const renderedAt = timestamp(item.renderedAt, `${path}.renderedAt`);
  if (renderedAt < approvedAt) {
    fail(`${path}.renderedAt`, "cannot precede human approvedAt");
  }
  literal(item.sandboxOnly, `${path}.sandboxOnly`, true);
  literal(item.state, `${path}.state`, "receipt");
  literal(item.submittedToRealInsurer, `${path}.submittedToRealInsurer`, false);
  const summary = object(item.summary, `${path}.summary`, [
    "attachmentCount",
    "completedFieldCount",
    "finalActionOwner",
    "verificationPassed",
  ]);
  literal(summary.attachmentCount, `${path}.summary.attachmentCount`, 3);
  literal(summary.completedFieldCount, `${path}.summary.completedFieldCount`, 8);
  literal(summary.finalActionOwner, `${path}.summary.finalActionOwner`, "human");
  literal(summary.verificationPassed, `${path}.summary.verificationPassed`, true);
  oneOf(item.variant, `${path}.variant`, ["A", "B"] as const);
  integer(item.version, `${path}.version`, 1);
}

type WorkflowEventKind = keyof typeof EXPECTED_AUDIT_TYPE;

function validateWorkflowEvent(value: unknown, path: string): WorkflowEventKind {
  const base = record(value, path);
  const kind = oneOf(base.kind, `${path}.kind`, [
    "state",
    "gate",
    "clarification",
    "plan_step",
    "tool_call",
    "portal_fill",
    "verification",
    "retry",
    "operational_failure",
    "provider_call",
  ] as const);
  switch (kind) {
    case "state": {
      const item = object(value, path, ["actor", "fromState", "kind", "toState"]);
      oneOf(item.actor, `${path}.actor`, ["system", "agent", "human"] as const);
      const from = oneOf(item.fromState, `${path}.fromState`, CASE_STATES);
      const to = oneOf(item.toState, `${path}.toState`, CASE_STATES);
      if (!(CASE_TRANSITIONS[from] as readonly CaseState[]).includes(to)) {
        fail(path, "contains an invalid case transition");
      }
      if (to === "human_approved" && item.actor !== "human") {
        fail(`${path}.actor`, "only a human may approve");
      }
      break;
    }
    case "gate": {
      const item = object(value, path, ["decision", "kind"]);
      validateGateDecision(item.decision, `${path}.decision`);
      break;
    }
    case "clarification": {
      const item = object(value, path, ["field", "kind", "round", "status"]);
      oneOf(item.field, `${path}.field`, REQUIRED_FIELDS);
      oneOf(item.round, `${path}.round`, [1, 2, 3] as const);
      oneOf(item.status, `${path}.status`, ["requested", "confirmed", "exhausted"] as const);
      break;
    }
    case "plan_step": {
      const item = object(value, path, ["kind", "sequence", "tool"]);
      integer(item.sequence, `${path}.sequence`, 1, 40);
      oneOf(item.tool, `${path}.tool`, ALLOWED_TOOLS);
      break;
    }
    case "tool_call": {
      const item = record(value, path);
      const status = oneOf(item.status, `${path}.status`, ["started", "succeeded", "blocked"] as const);
      object(
        value,
        path,
        status === "started"
          ? ["invocationId", "kind", "sequence", "status", "tool"]
          : ["durationMs", "invocationId", "kind", "sequence", "status", "tool"],
      );
      identifier(item.invocationId, `${path}.invocationId`);
      integer(item.sequence, `${path}.sequence`, 1, 40);
      oneOf(item.tool, `${path}.tool`, ALLOWED_TOOLS);
      if (status !== "started") integer(item.durationMs, `${path}.durationMs`, 0);
      break;
    }
    case "portal_fill": {
      const item = object(value, path, ["kind", "portalVersion", "variant", "writtenFields"]);
      integer(item.portalVersion, `${path}.portalVersion`, 1);
      oneOf(item.variant, `${path}.variant`, ["A", "B"] as const);
      const fields = array(item.writtenFields, `${path}.writtenFields`, (entry, entryPath) => {
        oneOf(entry, entryPath, REQUIRED_FIELDS);
      }, 1);
      if (new Set(fields).size !== fields.length) fail(`${path}.writtenFields`, "must be unique");
      break;
    }
    case "verification": {
      const item = object(value, path, [
        "attemptNumber",
        "deterministicMatch",
        "final",
        "kind",
        "modelReportedMismatch",
        "repairUsed",
        "status",
      ]);
      const attempt = oneOf(item.attemptNumber, `${path}.attemptNumber`, [1, 2] as const);
      const status = oneOf(item.status, `${path}.status`, ["verified", "mismatch", "blocked"] as const);
      const deterministic = boolean(item.deterministicMatch, `${path}.deterministicMatch`);
      const modelMismatch = boolean(item.modelReportedMismatch, `${path}.modelReportedMismatch`);
      const repair = boolean(item.repairUsed, `${path}.repairUsed`);
      const final = boolean(item.final, `${path}.final`);
      if ((attempt === 1 && repair) || (attempt === 2 && (!repair || !final))) {
        fail(path, "contains invalid bounded repair metadata");
      }
      if (status === "verified" && (!deterministic || modelMismatch)) {
        fail(path, "a verified event requires every check to pass");
      }
      if (status === "mismatch" && deterministic && !modelMismatch) {
        fail(path, "a mismatch event requires a mismatch signal");
      }
      break;
    }
    case "retry": {
      const item = object(value, path, [
        "callSequence",
        "durationMs",
        "failure",
        "kind",
        "modelId",
        "operation",
        "providerMode",
        "retryAttempt",
      ]);
      literal(item.operation, `${path}.operation`, "extraction");
      literal(item.retryAttempt, `${path}.retryAttempt`, 1);
      validateProviderMetadata(item, path);
      const failure = validateProviderFailure(item.failure, `${path}.failure`);
      if (!failure.retryable || failure.terminal) {
        fail(`${path}.failure`, "a retry requires a retryable, non-terminal failure");
      }
      break;
    }
    case "operational_failure": {
      const item = object(value, path, [
        "callSequence",
        "durationMs",
        "failure",
        "kind",
        "modelId",
        "operation",
        "providerMode",
        "retryAttempt",
      ]);
      validateProviderMetadata(item, path);
      const failure = validateProviderFailure(item.failure, `${path}.failure`);
      if (!failure.terminal) fail(`${path}.failure`, "must be terminal");
      break;
    }
    case "provider_call": {
      const item = object(
        value,
        path,
        [
          "callSequence",
          "durationMs",
          "kind",
          "modelId",
          "operation",
          "providerMode",
          "retryAttempt",
          "status",
        ],
        ["cost", "usage"],
      );
      validateProviderMetadata(item, path);
      literal(item.status, `${path}.status`, "succeeded");
      if (Object.hasOwn(item, "usage")) nullable(item.usage, `${path}.usage`, validateUsage);
      if (Object.hasOwn(item, "cost")) nullable(item.cost, `${path}.cost`, validateCost);
      break;
    }
  }
  return kind;
}

function validateProviderMetadata(item: JsonRecord, path: string): void {
  const operation = oneOf(item.operation, `${path}.operation`, WORKFLOW_OPERATIONS);
  const model = oneOf(item.modelId, `${path}.modelId`, PROVIDER_MODELS);
  const mode = oneOf(item.providerMode, `${path}.providerMode`, ["mock", "live"] as const);
  integer(item.callSequence, `${path}.callSequence`, 1, 40);
  const retry = oneOf(item.retryAttempt, `${path}.retryAttempt`, [0, 1] as const);
  integer(item.durationMs, `${path}.durationMs`, 0);
  if (operation !== "extraction" && retry !== 0) fail(`${path}.retryAttempt`, "is allowed only for extraction");
  if (mode === "mock" && model !== "claimdone-deterministic-mock") {
    fail(`${path}.modelId`, "mock mode requires the deterministic mock");
  }
  if (mode === "live") {
    const expected = operation === "transcription" ? "gpt-4o-transcribe" : "gpt-5.6-sol";
    if (model !== expected) fail(`${path}.modelId`, `live ${operation} requires ${expected}`);
  }
}

function validateProviderFailure(
  value: unknown,
  path: string,
): { readonly category: ProviderFailureCategory; readonly retryable: boolean; readonly terminal: boolean } {
  const item = object(value, path, ["category", "retryable", "terminal"]);
  const category = oneOf(item.category, `${path}.category`, PROVIDER_FAILURES);
  const retryable = boolean(item.retryable, `${path}.retryable`);
  const terminal = boolean(item.terminal, `${path}.terminal`);
  if (retryable && terminal) fail(path, "a terminal failure cannot be retryable");
  const alwaysTerminal = new Set<ProviderFailureCategory>([
    "quota_exhausted",
    "billing_limit",
    "rate_limited",
    "authentication_failed",
    "permission_denied",
    "model_not_found",
    "invalid_request",
    "cancelled",
    "content_filtered",
  ]);
  if (alwaysTerminal.has(category) && (retryable || !terminal)) {
    fail(path, `${category} must be terminal and non-retryable`);
  }
  return { category, retryable, terminal };
}

function validateUsage(value: unknown, path: string): void {
  const item = object(value, path, ["inputTokens", "outputTokens", "totalTokens"]);
  const input = integer(item.inputTokens, `${path}.inputTokens`, 0);
  const output = integer(item.outputTokens, `${path}.outputTokens`, 0);
  const total = integer(item.totalTokens, `${path}.totalTokens`, 0);
  if (input + output !== total) fail(`${path}.totalTokens`, "must equal inputTokens + outputTokens");
}

function validateCost(value: unknown, path: string): void {
  const item = object(value, path, ["currency", "estimatedCostMicros", "pricingSnapshotId"]);
  literal(item.currency, `${path}.currency`, "USD");
  integer(item.estimatedCostMicros, `${path}.estimatedCostMicros`, 0);
  identifier(item.pricingSnapshotId, `${path}.pricingSnapshotId`);
}

function record(value: unknown, path: string): JsonRecord {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    fail(path, "must be an object");
  }
  const prototype = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null) fail(path, "must be a plain object");
  return value as JsonRecord;
}

function object(
  value: unknown,
  path: string,
  required: readonly string[],
  optional: readonly string[] = [],
): JsonRecord {
  const item = record(value, path);
  const allowed = new Set([...required, ...optional]);
  for (const key of Object.keys(item)) {
    if (!allowed.has(key)) fail(`${path}.${key}`, "is not an allowed field");
  }
  for (const key of required) {
    if (!Object.hasOwn(item, key)) fail(`${path}.${key}`, "is required");
  }
  return item;
}

function nullable(
  value: unknown,
  path: string,
  validator: (entry: unknown, entryPath: string) => unknown,
): void {
  if (value !== null) validator(value, path);
}

function array(
  value: unknown,
  path: string,
  validator: (entry: unknown, entryPath: string) => unknown,
  minimum = 0,
  maximum = Number.MAX_SAFE_INTEGER,
): readonly unknown[] {
  if (!Array.isArray(value) || value.length < minimum || value.length > maximum) {
    fail(path, `must be an array with ${minimum}..${maximum} items`);
  }
  value.forEach((entry, index) => validator(entry, `${path}[${index}]`));
  return value;
}

function oneOf<const T extends readonly (string | number)[]>(
  value: unknown,
  path: string,
  choices: T,
): T[number] {
  if (!choices.some((choice) => value === choice)) {
    fail(path, `must be one of ${choices.join(", ")}`);
  }
  return value as T[number];
}

function literal<const T extends string | number | boolean>(
  value: unknown,
  path: string,
  expected: T,
): T {
  if (value !== expected) fail(path, `must be ${String(expected)}`);
  return expected;
}

function boolean(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") fail(path, "must be a boolean");
  return value;
}

function integer(value: unknown, path: string, minimum: number, maximum = Number.MAX_SAFE_INTEGER): number {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < minimum ||
    value > maximum
  ) {
    fail(path, `must be an integer in ${minimum}..${maximum}`);
  }
  return value;
}

function finiteNumber(value: unknown, path: string, minimum: number, maximum: number): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < minimum || value > maximum) {
    fail(path, `must be a finite number in ${minimum}..${maximum}`);
  }
  return value;
}

function scalar(value: unknown, path: string): void {
  if (value === null || typeof value === "boolean") return;
  if (typeof value === "string") {
    safeText(value, path, 0, 4_000);
    return;
  }
  if (typeof value === "number" && Number.isFinite(value)) return;
  fail(path, "must be a finite JSON scalar");
}

function safeText(value: unknown, path: string, minimum: number, maximum: number): string {
  if (
    typeof value !== "string" ||
    value.length < minimum ||
    value.length > maximum ||
    UNSAFE_CONTROL.test(value)
  ) {
    fail(path, `must be safe text with ${minimum}..${maximum} characters`);
  }
  return value;
}

function identifier(value: unknown, path: string): string {
  if (typeof value !== "string" || !IDENTIFIER.test(value)) {
    fail(path, "must be a valid identifier");
  }
  return value;
}

function attachmentIdentifier(value: unknown, path: string): string {
  return identifier(value, path);
}

function attachmentIdentifiers(
  value: unknown,
  path: string,
  minimum: number,
  maximum: number,
): readonly unknown[] {
  const identifiers = array(value, path, attachmentIdentifier, minimum, maximum);
  if (new Set(identifiers).size !== identifiers.length) {
    fail(path, "attachment identifiers must be unique");
  }
  return identifiers;
}

function digest(value: unknown, path: string): string {
  if (typeof value !== "string" || !SHA256.test(value)) fail(path, "must be a SHA-256 digest");
  return value;
}

function wireDate(value: unknown, path: string): void {
  if (typeof value !== "string" || !WIRE_DATE.test(value)) {
    fail(path, "must be a valid ISO date");
  }
  const [year, month, day] = value.split("-").map(Number);
  if (year === undefined || month === undefined || day === undefined || !validCalendarDate(year, month, day)) {
    fail(path, "must be a real ISO calendar date");
  }
}

function wireTime(value: unknown, path: string): void {
  if (typeof value !== "string" || !WIRE_TIME.test(value)) fail(path, "must be a valid wire time");
  const match = /^(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(?:Z|[+-](\d{2}):(\d{2}))?$/.exec(value);
  if (!match) fail(path, "must be a valid wire time");
  const [, hourText, minuteText, secondText, offsetHourText, offsetMinuteText] = match;
  const hour = Number(hourText);
  const minute = Number(minuteText);
  const second = Number(secondText);
  const offsetHour = offsetHourText === undefined ? 0 : Number(offsetHourText);
  const offsetMinute = offsetMinuteText === undefined ? 0 : Number(offsetMinuteText);
  if (hour > 23 || minute > 59 || second > 59 || offsetHour > 23 || offsetMinute > 59) {
    fail(path, "must be a valid wire time");
  }
}

function timestamp(value: unknown, path: string): number {
  if (typeof value !== "string" || !WIRE_DATETIME.test(value)) {
    fail(path, "must be an ISO timestamp with timezone");
  }
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(Z|([+-])(\d{2}):(\d{2}))$/.exec(value);
  if (!match) fail(path, "must be an ISO timestamp with timezone");
  const [, yearText, monthText, dayText, hourText, minuteText, secondText, , , offsetHourText, offsetMinuteText] = match;
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  const hour = Number(hourText);
  const minute = Number(minuteText);
  const second = Number(secondText);
  const offsetHour = offsetHourText === undefined ? 0 : Number(offsetHourText);
  const offsetMinute = offsetMinuteText === undefined ? 0 : Number(offsetMinuteText);
  if (
    !validCalendarDate(year, month, day) ||
    hour > 23 ||
    minute > 59 ||
    second > 59 ||
    offsetHour > 23 ||
    offsetMinute > 59
  ) {
    fail(path, "must contain real calendar, clock, and timezone components");
  }
  const result = Date.parse(value);
  if (Number.isNaN(result)) fail(path, "must be a real timestamp");
  return result;
}

function validCalendarDate(year: number, month: number, day: number): boolean {
  if (year < 1 || month < 1 || month > 12 || day < 1) return false;
  const days = [31, isLeapYear(year) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return day <= (days[month - 1] ?? 0);
}

function isLeapYear(year: number): boolean {
  return year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
}

function sameStringSet(values: readonly unknown[], expected: ReadonlySet<unknown>): boolean {
  return values.length === expected.size && values.every((value) => expected.has(value));
}

function sameJson(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if (Array.isArray(left) && Array.isArray(right)) {
    return left.length === right.length && left.every((entry, index) => sameJson(entry, right[index]));
  }
  if (
    typeof left === "object" && left !== null && !Array.isArray(left) &&
    typeof right === "object" && right !== null && !Array.isArray(right)
  ) {
    const leftRecord = left as Readonly<Record<string, unknown>>;
    const rightRecord = right as Readonly<Record<string, unknown>>;
    const leftKeys = Object.keys(leftRecord).sort();
    const rightKeys = Object.keys(rightRecord).sort();
    return sameJson(leftKeys, rightKeys) && leftKeys.every((key) => sameJson(leftRecord[key], rightRecord[key]));
  }
  return false;
}

function contract(value: unknown, path: string): void {
  literal(value, path, CONTRACT_VERSION);
}

function fail(path: string, message: string): never {
  throw new WorkflowPayloadError(path, message);
}
