import type {
  ClaimData,
  ClaimScope,
  ClarificationAnswerRequest,
  GateDecision,
  PortalSessionView,
  ReleaseDecision,
  ToolCallWorkflowEvent,
  ToolInvocation,
  WorkflowCaseView,
  WorkflowSnapshot,
} from "../generated/claimdone";

const threeAttachments: ClaimData["attachments"] = ["one", "two", "three"];
const noSubmissionAuthority: ClaimScope["agentCanSubmit"] = false;
const gateResult: GateDecision["passed"] = false;
const releaseResult: ReleaseDecision["passed"] = false;
const invocation: ToolInvocation = {
  contractVersion: "3.0.0",
  invocationId: "trusted-invocation-1",
  sequence: 1,
  tool: "inspect_evidence",
  arguments: {},
};
const caseView = {
  contractVersion: "3.0.0",
  caseId: "case-1",
  state: "created",
  version: 1,
  createdAt: "2026-07-14T12:00:00Z",
  updatedAt: "2026-07-14T12:00:00Z",
} as const satisfies WorkflowCaseView;
const clarificationAnswer: ClarificationAnswerRequest = {
  contractVersion: "3.0.0",
  caseId: "case-1",
  clarificationId: "clarification-1",
  field: "incident_date",
  round: 1,
  expectedVersion: 2,
  answer: "  exact answer  ",
};
const workflowSnapshot: WorkflowSnapshot = {
  contractVersion: "3.0.0",
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
const startedTool: ToolCallWorkflowEvent = {
  invocationId: "invocation-started",
  kind: "tool_call",
  sequence: 1,
  status: "started",
  tool: "inspect_form",
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

// @ts-expect-error The agent submission boundary is the literal false.
const forbiddenSubmissionAuthority: ClaimScope["agentCanSubmit"] = true;

const forbiddenInvocationArguments: ToolInvocation = {
  contractVersion: "3.0.0",
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
