import type {
  ClaimData,
  ClaimScope,
  GateDecision,
  ReleaseDecision,
  ToolInvocation,
} from "../generated/claimdone";

const threeAttachments: ClaimData["attachments"] = ["one", "two", "three"];
const noSubmissionAuthority: ClaimScope["agentCanSubmit"] = false;
const gateResult: GateDecision["passed"] = false;
const releaseResult: ReleaseDecision["passed"] = false;
const invocation: ToolInvocation = {
  contractVersion: "2.0.0",
  invocationId: "trusted-invocation-1",
  sequence: 1,
  tool: "inspect_evidence",
  arguments: {},
};

// @ts-expect-error ClaimData requires exactly three attachment references.
const twoAttachments: ClaimData["attachments"] = ["one", "two"];

// @ts-expect-error The agent submission boundary is the literal false.
const forbiddenSubmissionAuthority: ClaimScope["agentCanSubmit"] = true;

const forbiddenInvocationArguments: ToolInvocation = {
  contractVersion: "2.0.0",
  invocationId: "trusted-invocation-2",
  sequence: 2,
  tool: "inspect_form",
  // @ts-expect-error Tool arguments cannot carry model-controlled IDs, URLs, or values.
  arguments: { caseId: "case-1", url: "https://example.test", value: "secret" },
};

void threeAttachments;
void noSubmissionAuthority;
void gateResult;
void releaseResult;
void invocation;
void twoAttachments;
void forbiddenSubmissionAuthority;
void forbiddenInvocationArguments;
