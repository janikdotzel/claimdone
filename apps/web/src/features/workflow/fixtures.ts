import type {
  CaseState,
  WorkflowEventEnvelope,
  WorkflowSnapshot,
} from "../../../../../contracts/generated/claimdone";
import canonicalHappyPath from "../../../../../contracts/examples/happy_path.json";

import {
  parseWorkflowEventEnvelope,
  parseWorkflowSnapshot,
} from "./validation";

const CASE_ID = "case-happy-001";
const CREATED_AT = "2026-07-14T12:00:00Z";
const UPDATED_AT = "2026-07-14T12:00:20Z";

const ANALYSIS_PLAN = {
  agentCanSubmit: false,
  steps: [
    { sequence: 1, tool: "inspect_evidence", reason: "Inspect approved staged evidence" },
    { sequence: 2, tool: "check_required_fields", reason: "Check deterministic required fields" },
  ],
};

const CLARIFICATION_PLAN = {
  agentCanSubmit: false,
  steps: [
    ...ANALYSIS_PLAN.steps,
    { sequence: 3, tool: "ask_clarification", reason: "Ask only for the missing required field" },
  ],
};

const SAFE_FILL_PLAN = {
  agentCanSubmit: false,
  steps: [
    ...ANALYSIS_PLAN.steps,
    { sequence: 3, tool: "inspect_form", reason: "Inspect the rendered sandbox form" },
    { sequence: 4, tool: "fill_until_review", reason: "Fill only the sandbox draft" },
    { sequence: 5, tool: "verify_rendered_fields", reason: "Verify every rendered field" },
  ],
};

const PENDING_VERIFICATION = {
  actualAttachmentCount: null,
  deterministicMatch: null,
  expectedAttachmentCount: 3,
  fieldResults: [],
  modelReportedMismatch: false,
  reviewAllowed: false,
  status: "pending",
  verifiedAt: null,
};

function packetForState(state: CaseState) {
  const portalState =
    state === "human_approved"
      ? "human_approved"
      : state === "verifying" || state === "review"
        ? "review"
        : "draft";
  const packet = structuredClone({
    ...canonicalHappyPath,
    plan:
      state === "awaiting_clarification"
        ? CLARIFICATION_PLAN
        : ["ready_to_fill", "filling", "verifying", "review", "human_approved"].includes(state)
          ? SAFE_FILL_PLAN
          : ANALYSIS_PLAN,
    portalState,
    state,
  });
  if (state === "awaiting_clarification") {
    return {
      ...packet,
      claim: {
        ...packet.claim,
        fieldProvenance: packet.claim.fieldProvenance.filter(
          (entry) => entry.field !== "incident_date",
        ),
        incidentDate: null,
        missingRequiredFields: ["incident_date"],
      },
      gateDecisions: [
        ...packet.gateDecisions.slice(0, 5),
        {
          contractVersion: "3.0.0",
          decidedAt: "2026-07-14T12:00:05Z",
          deterministicPassed: false,
          evidenceRefs: ["prov-statement"],
          gateId: "G5",
          modelBlocked: false,
          passed: false,
          reasonCodes: ["G5_REQUIRED_FIELD_MISSING"],
        },
      ],
      verification: PENDING_VERIFICATION,
    };
  }
  if (state === "ready_to_fill" || state === "filling" || state === "verifying") {
    return {
      ...packet,
      gateDecisions: packet.gateDecisions.slice(
        0,
        state === "verifying" ? 8 : 6,
      ),
      verification: PENDING_VERIFICATION,
    };
  }
  return packet;
}

function caseView(state: CaseState, version = 7) {
  return {
    caseId: CASE_ID,
    contractVersion: "3.0.0",
    createdAt: CREATED_AT,
    state,
    updatedAt: UPDATED_AT,
    version,
  };
}

function baseSnapshot(state: CaseState, version = 7) {
  return {
    case: caseView(state, version),
    claimPacket: null,
    clarification: null,
    contractVersion: "3.0.0",
    portalSession: null,
    receipt: null,
    requestId: `request-${state.replaceAll("_", "-")}`,
    transcriptConfirmation: null,
    verificationAttempts: null,
  };
}

function portalReview() {
  const claim = canonicalHappyPath.claim;
  return {
    auditCount: 4,
    caseId: CASE_ID,
    contractVersion: "3.0.0",
    fields: {
      attachments: [...claim.attachments],
      claimantName: claim.claimantName,
      counterpartyKnown: claim.counterpartyKnown,
      incidentDate: claim.incidentDate,
      incidentTime: claim.incidentTime,
      location: claim.location,
      narrative: claim.narrative,
      policyReference: claim.policyReference,
      vehicleRegistration: claim.vehicleRegistration,
    },
    state: "review",
    updatedAt: "2026-07-14T12:00:15Z",
    variant: "A",
    version: 3,
  };
}

function verificationAttempts() {
  return {
    attempts: [
      {
        attemptId: "verification-001",
        attemptNumber: 1,
        caseId: CASE_ID,
        caseState: "verifying",
        contractVersion: "3.0.0",
        final: true,
        gateDecision:
          canonicalHappyPath.gateDecisions.find((decision) => decision.gateId === "G8") ??
          null,
        portalVersion: 3,
        repair: null,
        repairedFromAttemptId: null,
        report: structuredClone(canonicalHappyPath.verification),
      },
    ],
    caseId: CASE_ID,
    contractVersion: "3.0.0",
  };
}

export const CREATED_SNAPSHOT: WorkflowSnapshot = parseWorkflowSnapshot(
  baseSnapshot("created"),
  CASE_ID,
);

export const CLARIFICATION_SNAPSHOT: WorkflowSnapshot = parseWorkflowSnapshot(
  {
    ...baseSnapshot("awaiting_clarification"),
    claimPacket: packetForState("awaiting_clarification"),
    clarification: {
      caseId: CASE_ID,
      clarificationId: "clarification-001",
      contractVersion: "3.0.0",
      expectedVersion: 7,
      field: "incident_date",
      question: "What was the date of the staged demo incident?",
      requestedAt: "2026-07-14T12:00:10Z",
      round: 1,
      status: "requested",
    },
  },
  CASE_ID,
);

export const VERIFYING_SNAPSHOT: WorkflowSnapshot = parseWorkflowSnapshot(
  {
    ...baseSnapshot("verifying"),
    claimPacket: packetForState("verifying"),
    portalSession: portalReview(),
  },
  CASE_ID,
);

export const READY_SNAPSHOT: WorkflowSnapshot = parseWorkflowSnapshot(
  {
    ...baseSnapshot("ready_to_fill"),
    claimPacket: packetForState("ready_to_fill"),
  },
  CASE_ID,
);

export const REVIEW_SNAPSHOT: WorkflowSnapshot = parseWorkflowSnapshot(
  {
    ...baseSnapshot("review"),
    claimPacket: packetForState("review"),
    portalSession: portalReview(),
    verificationAttempts: verificationAttempts(),
  },
  CASE_ID,
);

export const BLOCKED_SNAPSHOT: WorkflowSnapshot = parseWorkflowSnapshot(
  {
    ...baseSnapshot("blocked"),
    claimPacket: {
      ...packetForState("awaiting_clarification"),
      plan: ANALYSIS_PLAN,
      state: "blocked",
    },
  },
  CASE_ID,
);

export const EMERGENCY_SNAPSHOT: WorkflowSnapshot = parseWorkflowSnapshot(
  {
    ...baseSnapshot("emergency_stopped"),
    claimPacket: {
      ...packetForState("review"),
      gateDecisions: [
        ...canonicalHappyPath.gateDecisions.slice(0, 3),
        {
          contractVersion: "3.0.0",
          decidedAt: "2026-07-14T12:00:03Z",
          deterministicPassed: false,
          evidenceRefs: ["prov-statement"],
          gateId: "G3",
          modelBlocked: false,
          passed: false,
          reasonCodes: ["G3_INJURY_OR_EMERGENCY"],
        },
      ],
      plan: ANALYSIS_PLAN,
      portalState: "draft",
      state: "emergency_stopped",
      verification: PENDING_VERIFICATION,
    },
  },
  CASE_ID,
);

function mismatchReport() {
  return {
    ...structuredClone(canonicalHappyPath.verification),
    deterministicMatch: false,
    fieldResults: canonicalHappyPath.verification.fieldResults.map((field) =>
      field.field === "location"
        ? { ...field, actual: "Different staged location", status: "mismatch" }
        : structuredClone(field),
    ),
    reviewAllowed: false,
    status: "mismatch",
    verifiedAt: "2026-07-14T12:00:01Z",
  };
}

function failedG8() {
  return {
    contractVersion: "3.0.0",
    decidedAt: "2026-07-14T12:00:08Z",
    deterministicPassed: false,
    evidenceRefs: ["prov-statement"],
    gateId: "G8",
    modelBlocked: false,
    passed: false,
    reasonCodes: ["G8_FIELD_MISMATCH"],
  };
}

export const REPAIR_SNAPSHOT: WorkflowSnapshot = parseWorkflowSnapshot(
  {
    ...baseSnapshot("verifying"),
    claimPacket: {
      ...packetForState("verifying"),
      gateDecisions: structuredClone(canonicalHappyPath.gateDecisions),
      verification: structuredClone(canonicalHappyPath.verification),
    },
    portalSession: { ...portalReview(), version: 4 },
    verificationAttempts: {
      attempts: [
        {
          attemptId: "verification-repairable",
          attemptNumber: 1,
          caseId: CASE_ID,
          caseState: "verifying",
          contractVersion: "3.0.0",
          final: false,
          gateDecision: null,
          portalVersion: 3,
          repair: {
            field: "location",
            fromPortalVersion: 3,
            repairNumber: 1,
            sourceRefs: ["prov-statement"],
            toPortalVersion: 4,
          },
          repairedFromAttemptId: null,
          report: mismatchReport(),
        },
        {
          attemptId: "verification-repaired",
          attemptNumber: 2,
          caseId: CASE_ID,
          caseState: "verifying",
          contractVersion: "3.0.0",
          final: true,
          gateDecision:
            canonicalHappyPath.gateDecisions.find((decision) => decision.gateId === "G8") ??
            null,
          portalVersion: 4,
          repair: null,
          repairedFromAttemptId: "verification-repairable",
          report: structuredClone(canonicalHappyPath.verification),
        },
      ],
      caseId: CASE_ID,
      contractVersion: "3.0.0",
    },
  },
  CASE_ID,
);

export const G8_BLOCKED_SNAPSHOT: WorkflowSnapshot = parseWorkflowSnapshot(
  {
    ...baseSnapshot("blocked"),
    claimPacket: {
      ...packetForState("review"),
      gateDecisions: [
        ...canonicalHappyPath.gateDecisions.slice(0, 8),
        failedG8(),
      ],
      state: "blocked",
      verification: mismatchReport(),
    },
    portalSession: {
      ...portalReview(),
      fields: { ...portalReview().fields, location: "Different staged location" },
    },
    verificationAttempts: {
      attempts: [
        {
          attemptId: "verification-blocked",
          attemptNumber: 1,
          caseId: CASE_ID,
          caseState: "verifying",
          contractVersion: "3.0.0",
          final: true,
          gateDecision: failedG8(),
          portalVersion: 3,
          repair: null,
          repairedFromAttemptId: null,
          report: mismatchReport(),
        },
      ],
      caseId: CASE_ID,
      contractVersion: "3.0.0",
    },
  },
  CASE_ID,
);

export const RECEIPT_SNAPSHOT: WorkflowSnapshot = parseWorkflowSnapshot(
  {
    ...baseSnapshot("receipt", 9),
    receipt: {
      approvalId: "approval-001",
      approvedAt: "2026-07-14T12:00:25Z",
      caseId: CASE_ID,
      contractVersion: "3.0.0",
      environment: "sandbox",
      humanApproved: true,
      receiptId: "receipt-001",
      redacted: true,
      renderedAt: "2026-07-14T12:00:26Z",
      sandboxOnly: true,
      state: "receipt",
      submittedToRealInsurer: false,
      summary: {
        attachmentCount: 3,
        completedFieldCount: 8,
        finalActionOwner: "human",
        verificationPassed: true,
      },
      variant: "A",
      version: 2,
    },
  },
  CASE_ID,
);

function eventEnvelope(
  cursor: number,
  sourceAuditEventType: WorkflowEventEnvelope["sourceAuditEventType"],
  event: object,
): WorkflowEventEnvelope {
  return parseWorkflowEventEnvelope(
    {
      caseId: CASE_ID,
      contractVersion: "3.0.0",
      cursor,
      event,
      eventId: `workflow-event-${cursor}`,
      occurredAt: `2026-07-14T12:00:${String(cursor).padStart(2, "0")}Z`,
      sourceAuditEventId: `audit-event-${cursor}`,
      sourceAuditEventType,
      sourceAuditSequence: cursor,
    },
    CASE_ID,
  );
}

export const SHOWCASE_EVENTS = [
  eventEnvelope(1, "plan_step", {
    kind: "plan_step",
    sequence: 1,
    tool: "inspect_evidence",
  }),
  eventEnvelope(2, "tool_call", {
    durationMs: 240,
    invocationId: "tool-001",
    kind: "tool_call",
    sequence: 1,
    status: "succeeded",
    tool: "inspect_evidence",
  }),
  eventEnvelope(3, "retry", {
    callSequence: 2,
    durationMs: 800,
    failure: { category: "invalid_response", retryable: true, terminal: false },
    kind: "retry",
    modelId: "gpt-5.6-sol",
    operation: "extraction",
    providerMode: "live",
    retryAttempt: 1,
  }),
  eventEnvelope(4, "gate_decision", {
    decision: canonicalHappyPath.gateDecisions[0],
    kind: "gate",
  }),
  eventEnvelope(5, "portal_fill", {
    kind: "portal_fill",
    portalVersion: 3,
    variant: "A",
    writtenFields: ["location"],
  }),
  eventEnvelope(6, "verification", {
    attemptNumber: 1,
    deterministicMatch: true,
    final: true,
    kind: "verification",
    modelReportedMismatch: false,
    repairUsed: false,
    status: "verified",
  }),
] as const satisfies readonly WorkflowEventEnvelope[];

export const QUOTA_EVENT: WorkflowEventEnvelope = eventEnvelope(
  7,
  "operational_failure",
  {
    callSequence: 3,
    durationMs: 50,
    failure: { category: "quota_exhausted", retryable: false, terminal: true },
    kind: "operational_failure",
    modelId: "gpt-5.6-sol",
    operation: "extraction",
    providerMode: "live",
    retryAttempt: 0,
  },
);

export const WORKFLOW_SHOWCASE_SNAPSHOTS = [
  CREATED_SNAPSHOT,
  CLARIFICATION_SNAPSHOT,
  READY_SNAPSHOT,
  VERIFYING_SNAPSHOT,
  REVIEW_SNAPSHOT,
  BLOCKED_SNAPSHOT,
  EMERGENCY_SNAPSHOT,
  RECEIPT_SNAPSHOT,
] as const;
