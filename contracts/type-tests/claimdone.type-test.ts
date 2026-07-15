import type {
  ClaimData,
  ClaimScope,
  ClarificationAnswerRequest,
  GateDecision,
  PortalSessionView,
  PortalRunRenderFaultInjection,
  PortalRunRenderFaultRepair,
  PortalRunSetup,
  ReleaseDecision,
  ToolCallWorkflowEvent,
  ToolInvocation,
  VerificationReport,
  WorkflowCaseView,
  WorkflowSnapshot,
} from "../generated/claimdone";

const threeAttachments: ClaimData["attachments"] = ["one", "two", "three"];
const expectedAttachmentIds: VerificationReport["expectedAttachmentIds"] = [
  "one",
  "two",
  "three",
];
const actualAttachmentIds: VerificationReport["actualAttachmentIds"] = ["one"];
const noSubmissionAuthority: ClaimScope["agentCanSubmit"] = false;
const gateResult: GateDecision["passed"] = false;
const releaseResult: ReleaseDecision["passed"] = false;
const invocation: ToolInvocation = {
  contractVersion: "4.0.0",
  invocationId: "trusted-invocation-1",
  sequence: 1,
  tool: "inspect_evidence",
  arguments: {},
};
const caseView = {
  contractVersion: "4.0.0",
  caseId: "case-1",
  state: "created",
  version: 1,
  createdAt: "2026-07-14T12:00:00Z",
  updatedAt: "2026-07-14T12:00:00Z",
} as const satisfies WorkflowCaseView;
const clarificationAnswer: ClarificationAnswerRequest = {
  contractVersion: "4.0.0",
  caseId: "case-1",
  clarificationId: "clarification-1",
  field: "incident_date",
  round: 1,
  expectedVersion: 2,
  answer: "  exact answer  ",
};
const workflowSnapshot: WorkflowSnapshot = {
  contractVersion: "4.0.0",
  requestId: "request-1",
  case: caseView,
  claimPacket: null,
  transcriptConfirmation: null,
  clarification: null,
  portalSession: null,
  verificationAttempts: null,
  receipt: null,
};
const portalSession = {} as PortalSessionView;
const portalRunSetup: PortalRunSetup = {
  contractVersion: "4.0.0",
  runId: "run-portal-1",
  caseId: "case-1",
  variant: "A",
  expectedFields: {
    attachments: ["one", "two", "three"],
    claimantName: "Demo Claimant",
    counterpartyKnown: "yes",
    incidentDate: "2026-07-14",
    incidentTime: "14:30:00",
    location: "Berlin",
    narrative: "Synthetic staged incident.",
    policyReference: "DEMO-42",
    vehicleRegistration: "DEMO-CD-1",
  },
};
const renderFaultInjection: PortalRunRenderFaultInjection = {
  caseId: "case-1",
  contractVersion: "4.0.0",
  expectedVersion: 3,
  field: "claimant_name",
  runId: "run-portal-1",
  variant: "A",
};
const renderFaultRepair: PortalRunRenderFaultRepair = renderFaultInjection;
const startedTool: ToolCallWorkflowEvent = {
  invocationId: "invocation-started",
  kind: "tool_call",
  sequence: 1,
  status: "started",
  tool: "inspect_form",
};

const shortPortalRunSetup: PortalRunSetup = {
  ...portalRunSetup,
  expectedFields: {
    ...portalRunSetup.expectedFields,
    // @ts-expect-error Packet-bound portal setup requires exactly three IDs.
    attachments: ["one", "two"],
  },
};
const terminalTool: ToolCallWorkflowEvent = {
  durationMs: 10,
  invocationId: "invocation-terminal",
  kind: "tool_call",
  sequence: 2,
  status: "succeeded",
  tool: "inspect_form",
};

// @ts-expect-error ClaimData requires exactly three attachment references.
const twoAttachments: ClaimData["attachments"] = ["one", "two"];

// @ts-expect-error Verification expectedAttachmentIds requires exactly three references.
const shortExpectedAttachmentIds: VerificationReport["expectedAttachmentIds"] = [
  "one",
  "two",
];

// @ts-expect-error The agent submission boundary is the literal false.
const forbiddenSubmissionAuthority: ClaimScope["agentCanSubmit"] = true;

const forbiddenInvocationArguments: ToolInvocation = {
  contractVersion: "4.0.0",
  invocationId: "trusted-invocation-2",
  sequence: 2,
  tool: "inspect_form",
  // @ts-expect-error Tool arguments cannot carry model-controlled IDs, URLs, or values.
  arguments: { caseId: "case-1", url: "https://example.test", value: "secret" },
};

const snapshotWithRawIntake: WorkflowSnapshot = {
  ...workflowSnapshot,
  // @ts-expect-error WorkflowSnapshot cannot expose a free-form raw intake summary.
  rawIntakeSummary: {},
};

const reviewCase = {
  ...caseView,
  state: "review",
} as const satisfies WorkflowCaseView;

// @ts-expect-error Review cannot expose an all-null payload set.
const reviewWithNullPayloads: WorkflowSnapshot = {
  ...workflowSnapshot,
  case: reviewCase,
};

// @ts-expect-error Created state cannot expose a portal session.
const createdWithPortal: WorkflowSnapshot = {
  ...workflowSnapshot,
  portalSession,
};

// @ts-expect-error Terminal tool events require durationMs.
const terminalToolWithoutDuration: ToolCallWorkflowEvent = {
  invocationId: "invocation-terminal-missing-duration",
  kind: "tool_call",
  sequence: 3,
  status: "blocked",
  tool: "inspect_form",
};

const startedToolWithDuration: ToolCallWorkflowEvent = {
  // @ts-expect-error Started tool events must not expose a duration.
  durationMs: 1,
  invocationId: "invocation-started-with-duration",
  kind: "tool_call",
  sequence: 4,
  status: "started",
  tool: "inspect_form",
};

const startedToolWithNullDuration: ToolCallWorkflowEvent = {
  // @ts-expect-error Started tool events must also reject explicit null duration.
  durationMs: null,
  invocationId: "invocation-started-with-null-duration",
  kind: "tool_call",
  sequence: 5,
  status: "started",
  tool: "inspect_form",
};

void threeAttachments;
void noSubmissionAuthority;
void gateResult;
void releaseResult;
void invocation;
void caseView;
void clarificationAnswer;
void workflowSnapshot;
void portalSession;
void portalRunSetup;
void renderFaultInjection;
void renderFaultRepair;
void shortPortalRunSetup;
void startedTool;
void terminalTool;
void twoAttachments;
void forbiddenSubmissionAuthority;
void forbiddenInvocationArguments;
void snapshotWithRawIntake;
void reviewCase;
void reviewWithNullPayloads;
void createdWithPortal;
void terminalToolWithoutDuration;
void startedToolWithDuration;
void startedToolWithNullDuration;
