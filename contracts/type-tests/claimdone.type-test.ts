import type {
  ClaimData,
  ClaimScope,
  GateDecision,
  ReleaseDecision,
} from "../generated/claimdone";

const threeAttachments: ClaimData["attachments"] = ["one", "two", "three"];
const noSubmissionAuthority: ClaimScope["agentCanSubmit"] = false;
const gateResult: GateDecision["passed"] = false;
const releaseResult: ReleaseDecision["passed"] = false;

// @ts-expect-error ClaimData requires exactly three attachment references.
const twoAttachments: ClaimData["attachments"] = ["one", "two"];

// @ts-expect-error The agent submission boundary is the literal false.
const forbiddenSubmissionAuthority: ClaimScope["agentCanSubmit"] = true;

void threeAttachments;
void noSubmissionAuthority;
void gateResult;
void releaseResult;
void twoAttachments;
void forbiddenSubmissionAuthority;
