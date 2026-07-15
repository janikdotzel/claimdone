import type {
  CaseState,
  ClarificationAnswerRequest,
  GateReasonCode,
  WorkflowSnapshot,
} from "../../../../../contracts/generated/claimdone";

import {
  isKnownGateReasonCode,
  parseWorkflowSnapshot,
  validateGateDecisionBoundary,
  WorkflowPayloadError,
} from "../workflow/validation";

export const DEFAULT_CLAIMDONE_API_ORIGIN = "http://127.0.0.1:8000";
export const DEFAULT_CLAIMDONE_PORTAL_ORIGIN = "http://127.0.0.1:3000";

const IDENTIFIER_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const INCIDENT_TIME_PATTERN = /^(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d$/;
const ERROR_CODE_PATTERN = /^[A-Z][A-Z0-9_]{0,127}$/;
const UNSAFE_CONTROL_PATTERN = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/;
const INT002_CREATED_VERSION = 1;
const INT002_CLARIFICATION_VERSION = 4;
const INT002_READY_VERSION = 5;
const INT002_REVIEW_VERSION = 9;

export type CreatedSnapshot = Extract<
  WorkflowSnapshot,
  { readonly case: { readonly state: "created" } }
>;
export type AwaitingClarificationResponse = Extract<
  WorkflowSnapshot,
  { readonly case: { readonly state: "awaiting_clarification" } }
> & {
  readonly clarification: Extract<
    NonNullable<WorkflowSnapshot["clarification"]>,
    object
  > & {
    readonly field: "incident_time";
    readonly round: 1;
  };
};
export type ReadyToFillResponse = Extract<
  WorkflowSnapshot,
  { readonly case: { readonly state: "ready_to_fill" } }
>;
export type ReviewResponse = Extract<
  WorkflowSnapshot,
  { readonly case: { readonly state: "review" } }
>;
export type IntakeFlowResponse =
  | AwaitingClarificationResponse
  | ReadyToFillResponse
  | ReviewResponse;

type SnapshotForState<State extends CaseState> = Extract<
  WorkflowSnapshot,
  { readonly case: { readonly state: State } }
>;

export function isWorkflowSnapshotState<State extends CaseState>(
  snapshot: WorkflowSnapshot | null,
  state: State,
): snapshot is SnapshotForState<State> {
  return snapshot?.case.state === state;
}

export function isInt002ClarificationSnapshot(
  snapshot: WorkflowSnapshot | null,
): snapshot is AwaitingClarificationResponse {
  return (
    isWorkflowSnapshotState(snapshot, "awaiting_clarification") &&
    snapshot.case.version === INT002_CLARIFICATION_VERSION &&
    snapshot.clarification.field === "incident_time" &&
    snapshot.clarification.round === 1 &&
    snapshot.clarification.expectedVersion === snapshot.case.version
  );
}

export interface IntakeSubmission {
  readonly audio: File | null;
  readonly dataProcessingApproved: boolean;
  readonly exifDecisions: readonly ("strip" | "retain")[];
  readonly expectedVersion: number;
  readonly imageRightsConfirmed: boolean;
  readonly images: readonly File[];
  readonly sandboxAcknowledged: boolean;
  readonly statementText: string | null;
}

export type NewIntakeSubmission = Omit<IntakeSubmission, "expectedVersion">;

export interface IntakeCaseLifecycle {
  readonly onCaseCreated?: (caseId: string) => void;
  readonly onCaseCleaned?: (caseId: string) => void;
}

export interface ApiFieldError {
  readonly field: string;
  readonly message: string;
  readonly reasonCode: GateReasonCode | null;
}

interface ErrorDetail {
  readonly code: string;
  readonly currentVersion: number | null;
  readonly fieldErrors: readonly ApiFieldError[];
  readonly message: string;
  readonly reasonCodes: readonly GateReasonCode[];
}

export class ClaimDoneApiError extends Error {
  constructor(
    readonly detail: ErrorDetail,
    readonly status: number,
  ) {
    super(detail.message);
    this.name = "ClaimDoneApiError";
  }
}

export class ClaimDonePendingCleanupError extends Error {
  constructor(
    readonly pendingCaseId: string,
    readonly primaryError: unknown,
    readonly cleanupError: unknown,
  ) {
    super("The intake failed and its server resources could not be fully deleted.");
    this.name = "ClaimDonePendingCleanupError";
  }
}

export type ClaimDoneFetch = (
  input: string | URL | Request,
  init?: RequestInit,
) => Promise<Response>;

export function claimDoneApiOrigin(): string {
  return normalizeConfiguredOrigin(
    process.env.NEXT_PUBLIC_CLAIMDONE_API_ORIGIN || DEFAULT_CLAIMDONE_API_ORIGIN,
    "API",
    DEFAULT_CLAIMDONE_API_ORIGIN,
  );
}

export function claimDonePortalOrigin(): string {
  return normalizeConfiguredOrigin(
    process.env.NEXT_PUBLIC_CLAIMDONE_PORTAL_ORIGIN ||
      DEFAULT_CLAIMDONE_PORTAL_ORIGIN,
    "portal",
    DEFAULT_CLAIMDONE_PORTAL_ORIGIN,
  );
}

export function portalAReviewUrl(caseId: string): string {
  assertIdentifier(caseId, "caseId");
  return `${claimDonePortalOrigin()}/sandbox/A/cases/${encodeURIComponent(caseId)}`;
}

export async function createCase(
  fetcher: ClaimDoneFetch = fetch,
): Promise<CreatedSnapshot> {
  const body = await requestJson(fetcher, `${claimDoneApiOrigin()}/api/cases`, {
    body: JSON.stringify({ metadata: {} }),
    headers: { "Content-Type": "application/json" },
    method: "POST",
  });
  const snapshot = parseMutationSnapshot(body);
  if (
    !isWorkflowSnapshotState(snapshot, "created") ||
    snapshot.case.version !== INT002_CREATED_VERSION
  ) {
    throw invalidResponse("Case creation did not return the canonical created snapshot.");
  }
  return snapshot;
}

export async function createAndSubmitIntake(
  submission: NewIntakeSubmission,
  fetcher: ClaimDoneFetch = fetch,
  lifecycle: IntakeCaseLifecycle = {},
): Promise<AwaitingClarificationResponse> {
  const created = await createCase(fetcher);
  lifecycle.onCaseCreated?.(created.case.caseId);
  try {
    return await submitIntake(
      created.case.caseId,
      { ...submission, expectedVersion: created.case.version },
      fetcher,
    );
  } catch (primaryError) {
    try {
      await deleteAuthoritativeCase(created.case.caseId, fetcher);
      lifecycle.onCaseCleaned?.(created.case.caseId);
    } catch (cleanupError) {
      throw new ClaimDonePendingCleanupError(
        created.case.caseId,
        primaryError,
        cleanupError,
      );
    }
    throw primaryError;
  }
}

export async function deleteCase(
  caseId: string,
  fetcher: ClaimDoneFetch = fetch,
): Promise<void> {
  assertIdentifier(caseId, "caseId");
  await requestDelete(
    fetcher,
    `${claimDoneApiOrigin()}/api/cases/${encodeURIComponent(caseId)}`,
    "The ClaimDone API could not be reached for cleanup.",
    "The ClaimDone API returned invalid cleanup JSON.",
  );
}

export async function deletePortalCase(
  caseId: string,
  fetcher: ClaimDoneFetch = fetch,
): Promise<void> {
  assertIdentifier(caseId, "caseId");
  await requestDelete(
    fetcher,
    `${claimDonePortalOrigin()}/api/sandbox/cases/${encodeURIComponent(caseId)}`,
    "The sandbox portal could not be reached for cleanup.",
    "The sandbox portal returned invalid cleanup JSON.",
  );
}

export async function deleteAuthoritativeCase(
  caseId: string,
  fetcher: ClaimDoneFetch = fetch,
): Promise<void> {
  // Portal cleanup is deliberately first and idempotent. If it fails, retain the
  // backend case so the caller still owns an authoritative retry target.
  await deletePortalCase(caseId, fetcher);
  await deleteCase(caseId, fetcher);
}

export async function submitIntake(
  caseId: string,
  submission: IntakeSubmission,
  fetcher: ClaimDoneFetch = fetch,
): Promise<AwaitingClarificationResponse> {
  assertIdentifier(caseId, "caseId");
  validateIntakeSubmission(submission);

  const form = new FormData();
  form.append("expectedVersion", String(submission.expectedVersion));
  for (const image of submission.images) form.append("images", image);
  if (submission.statementText !== null) {
    form.append("statementText", submission.statementText);
  } else if (submission.audio !== null) {
    form.append("audio", submission.audio);
  }
  form.append("sandboxAcknowledged", String(submission.sandboxAcknowledged));
  form.append("imageRightsConfirmed", String(submission.imageRightsConfirmed));
  form.append("dataProcessingApproved", String(submission.dataProcessingApproved));
  for (const decision of submission.exifDecisions) {
    form.append("exifDecisions", decision);
  }

  const body = await requestJson(
    fetcher,
    `${claimDoneApiOrigin()}/api/cases/${encodeURIComponent(caseId)}/intake`,
    { body: form, method: "POST" },
  );
  const snapshot = parseMutationSnapshot(body, caseId);
  if (
    !isInt002ClarificationSnapshot(snapshot)
  ) {
    throw invalidResponse(
      "Intake did not return the required version-bound incident_time clarification.",
    );
  }
  return snapshot;
}

export async function answerClarification(
  request: ClarificationAnswerRequest,
  fetcher: ClaimDoneFetch = fetch,
): Promise<ReadyToFillResponse> {
  validateClarificationAnswerRequest(request);
  const body = await requestJson(
    fetcher,
    `${claimDoneApiOrigin()}/api/cases/${encodeURIComponent(request.caseId)}/clarifications/${encodeURIComponent(request.clarificationId)}/answer`,
    {
      body: JSON.stringify(request),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
  );
  const snapshot = parseMutationSnapshot(body, request.caseId);
  if (
    !isWorkflowSnapshotState(snapshot, "ready_to_fill") ||
    snapshot.case.version !== INT002_READY_VERSION
  ) {
    throw invalidResponse(
      "Clarification did not return the canonical ready_to_fill snapshot.",
    );
  }
  return snapshot;
}

export async function runClaimToReview(
  caseId: string,
  expectedVersion: number,
  fetcher: ClaimDoneFetch = fetch,
): Promise<ReviewResponse> {
  assertIdentifier(caseId, "caseId");
  assertExactVersion(
    expectedVersion,
    INT002_READY_VERSION,
    "The INT-002 run requires the canonical ready version.",
  );
  const body = await requestJson(
    fetcher,
    `${claimDoneApiOrigin()}/api/cases/${encodeURIComponent(caseId)}/run`,
    {
      body: JSON.stringify({
        contractVersion: "4.0.0",
        expectedVersion,
      }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
  );
  const snapshot = parseMutationSnapshot(body, caseId);
  assertInt002ReviewSnapshot(snapshot);
  return snapshot;
}

export interface Int002ClarificationRunPorts {
  readonly answer?: (
    request: ClarificationAnswerRequest,
  ) => Promise<ReadyToFillResponse>;
  readonly onReady?: (snapshot: ReadyToFillResponse) => void;
  readonly run?: (
    caseId: string,
    expectedVersion: number,
  ) => Promise<ReviewResponse>;
}

/**
 * The ready callback is deliberately synchronous and runs before /run starts.
 * A caller can therefore commit READY authority and a distinct run request
 * identity before any run response (including an immediate failure) is handled.
 */
export async function answerThenRunToReview(
  request: ClarificationAnswerRequest,
  ports: Int002ClarificationRunPorts = {},
): Promise<ReviewResponse> {
  const ready = await (ports.answer ?? answerClarification)(request);
  ports.onReady?.(ready);
  return (ports.run ?? runClaimToReview)(ready.case.caseId, ready.case.version);
}

export function assertInt002ReviewSnapshot(
  snapshot: WorkflowSnapshot,
): asserts snapshot is ReviewResponse {
  if (
    !isWorkflowSnapshotState(snapshot, "review") ||
    snapshot.case.version !== INT002_REVIEW_VERSION ||
    snapshot.receipt !== null ||
    snapshot.portalSession.variant !== "A"
  ) {
    throw invalidResponse(
      "The run did not stop at the verified local Portal A review boundary.",
    );
  }

  const expectedGates = ["G0", "G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8"];
  const gates = snapshot.claimPacket.gateDecisions;
  if (
    gates.length !== expectedGates.length ||
    gates.some(
      (gate, index) =>
        gate.gateId !== expectedGates[index] ||
        !gate.passed ||
        !gate.deterministicPassed ||
        gate.modelBlocked ||
        gate.reasonCodes.length !== 0,
    )
  ) {
    throw invalidResponse("The final review snapshot does not prove green G0 through G8.");
  }

  const attempts = snapshot.verificationAttempts.attempts;
  const first = attempts[0];
  const second = attempts[1];
  const firstNonMatches = first?.report.fieldResults.filter(
    (field) => field.status !== "match",
  );
  if (
    attempts.length !== 2 ||
    first?.attemptNumber !== 1 ||
    first.final ||
    first.gateDecision !== null ||
    first.report.status !== "mismatch" ||
    first.report.deterministicMatch !== false ||
    first.report.reviewAllowed ||
    first.repair === null ||
    firstNonMatches?.length !== 1 ||
    firstNonMatches[0]?.field !== first.repair.field ||
    second?.attemptNumber !== 2 ||
    !second.final ||
    second.report.status !== "verified" ||
    second.report.deterministicMatch !== true ||
    !second.report.reviewAllowed ||
    second.gateDecision?.gateId !== "G8" ||
    !second.gateDecision.passed ||
    second.repairedFromAttemptId !== first.attemptId ||
    second.portalVersion !== first.repair.toPortalVersion ||
    snapshot.portalSession.version !== second.portalVersion
  ) {
    throw invalidResponse(
      "The final review snapshot does not contain the required fault, narrow repair, and verified second attempt.",
    );
  }
}

export function isWavFile(file: Pick<File, "name" | "type">): boolean {
  const mimeType = file.type.toLowerCase();
  return (
    ["audio/wav", "audio/wave", "audio/x-wav"].includes(mimeType) ||
    (mimeType === "" && file.name.toLowerCase().endsWith(".wav"))
  );
}

function validateIntakeSubmission(submission: IntakeSubmission): void {
  if (submission.images.length !== 3 || submission.exifDecisions.length !== 3) {
    throw clientInputError("Exactly three images and EXIF decisions are required.");
  }
  assertExactVersion(
    submission.expectedVersion,
    INT002_CREATED_VERSION,
    "The INT-002 intake requires the canonical created version.",
  );
  const hasText = submission.statementText !== null;
  const hasAudio = submission.audio !== null;
  if (hasText === hasAudio) {
    throw clientInputError("Provide exactly one written statement or WAV audio memo.");
  }
  if (submission.audio !== null && !isWavFile(submission.audio)) {
    throw clientInputError("Only WAV audio is supported in this build.");
  }
}

function validateClarificationAnswerRequest(
  request: ClarificationAnswerRequest,
): void {
  assertIdentifier(request.caseId, "caseId");
  assertIdentifier(request.clarificationId, "clarificationId");
  assertExactVersion(
    request.expectedVersion,
    INT002_CLARIFICATION_VERSION,
    "The INT-002 clarification requires the canonical awaiting-clarification version.",
  );
  if (
    request.contractVersion !== "4.0.0" ||
    request.field !== "incident_time" ||
    request.round !== 1 ||
    !INCIDENT_TIME_PATTERN.test(request.answer)
  ) {
    throw clientInputError(
      "The clarification must preserve incident time exactly as HH:MM:SS.",
    );
  }
}

async function requestJson(
  fetcher: ClaimDoneFetch,
  input: string,
  init: RequestInit,
): Promise<unknown> {
  let response: Response;
  try {
    response = await fetcher(input, { ...init, cache: "no-store" });
  } catch {
    throw new ClaimDoneApiError(
      {
        code: "CLIENT_NETWORK_ERROR",
        currentVersion: null,
        fieldErrors: [],
        message: "The ClaimDone API could not be reached.",
        reasonCodes: [],
      },
      0,
    );
  }

  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw invalidResponse("The ClaimDone API returned invalid JSON.", response.status);
  }
  if (!response.ok) throw parseErrorEnvelope(body, response.status);
  return body;
}

async function requestDelete(
  fetcher: ClaimDoneFetch,
  input: string,
  networkMessage: string,
  invalidJsonMessage: string,
): Promise<void> {
  let response: Response;
  try {
    response = await fetcher(input, { cache: "no-store", method: "DELETE" });
  } catch {
    throw new ClaimDoneApiError(
      {
        code: "CLIENT_NETWORK_ERROR",
        currentVersion: null,
        fieldErrors: [],
        message: networkMessage,
        reasonCodes: [],
      },
      0,
    );
  }
  if (response.ok) return;
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw invalidResponse(invalidJsonMessage, response.status);
  }
  throw parseErrorEnvelope(body, response.status);
}

function parseMutationSnapshot(
  value: unknown,
  expectedCaseId?: string,
): WorkflowSnapshot {
  try {
    return parseWorkflowSnapshot(value, expectedCaseId);
  } catch (error) {
    if (error instanceof WorkflowPayloadError) {
      throw invalidResponse(
        `The ClaimDone API returned an invalid canonical snapshot (${error.message}).`,
      );
    }
    throw error;
  }
}

function parseErrorEnvelope(value: unknown, status: number): ClaimDoneApiError {
  try {
    const envelope = exactRecord(value, ["error"]);
    const detail = exactRecord(envelope.error, [
      "code",
      "message",
      "reasonCodes",
      "fieldErrors",
      "gateDecision",
      "currentVersion",
    ]);
    if (
      typeof detail.code !== "string" ||
      !ERROR_CODE_PATTERN.test(detail.code) ||
      !isSafeText(detail.message, 512)
    ) {
      throw new Error("invalid error identity");
    }
    const reasonCodes = parseReasonCodes(detail.reasonCodes);
    if (!Array.isArray(detail.fieldErrors) || detail.fieldErrors.length > 64) {
      throw new Error("invalid field errors");
    }
    const fieldErrors = detail.fieldErrors.map(parseClosedFieldError);
    if (detail.gateDecision !== null) {
      validateGateDecisionBoundary(detail.gateDecision);
      const gateDecision = exactRecord(detail.gateDecision, [
        "contractVersion",
        "decidedAt",
        "deterministicPassed",
        "evidenceRefs",
        "gateId",
        "modelBlocked",
        "passed",
        "reasonCodes",
      ]);
      const gateReasonCodes = parseReasonCodes(gateDecision.reasonCodes);
      if (
        gateReasonCodes.length !== reasonCodes.length ||
        gateReasonCodes.some((reason, index) => reason !== reasonCodes[index])
      ) {
        throw new Error("gate and envelope reasons disagree");
      }
    }
    return new ClaimDoneApiError(
      {
        code: detail.code,
        currentVersion: parseCurrentVersion(detail.currentVersion),
        fieldErrors,
        message: detail.message,
        reasonCodes,
      },
      status,
    );
  } catch {
    return invalidResponse("The ClaimDone API returned an invalid error envelope.", status);
  }
}

function parseReasonCodes(value: unknown): GateReasonCode[] {
  if (
    !Array.isArray(value) ||
    value.length > 32 ||
    !value.every(isGateReasonCode) ||
    new Set(value).size !== value.length
  ) {
    throw new Error("invalid reason codes");
  }
  return value as GateReasonCode[];
}

function parseClosedFieldError(value: unknown): ApiFieldError {
  const item = exactRecord(value, ["field", "reasonCode", "message"]);
  if (
    !isSafeText(item.field, 256) ||
    !isSafeText(item.message, 512) ||
    (item.reasonCode !== null && !isGateReasonCode(item.reasonCode))
  ) {
    throw new Error("invalid field error");
  }
  return {
    field: item.field,
    message: item.message,
    reasonCode: item.reasonCode,
  };
}

function parseCurrentVersion(value: unknown): number | null {
  if (value === null) return null;
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 1) {
    throw new Error("invalid current version");
  }
  return value;
}

function exactRecord(value: unknown, keys: readonly string[]): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("expected object");
  }
  const item = value as Record<string, unknown>;
  const actual = Object.keys(item).sort();
  const expected = [...keys].sort();
  if (
    actual.length !== expected.length ||
    actual.some((key, index) => key !== expected[index])
  ) {
    throw new Error("unexpected object shape");
  }
  return item;
}

function isSafeText(value: unknown, maximumLength: number): value is string {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= maximumLength &&
    !UNSAFE_CONTROL_PATTERN.test(value)
  );
}

function invalidResponse(message: string, status = 502): ClaimDoneApiError {
  return new ClaimDoneApiError(
    {
      code: "CLIENT_INVALID_RESPONSE",
      currentVersion: null,
      fieldErrors: [],
      message,
      reasonCodes: [],
    },
    status,
  );
}

function clientInputError(message: string): ClaimDoneApiError {
  return new ClaimDoneApiError(
    {
      code: "CLIENT_INPUT_INVALID",
      currentVersion: null,
      fieldErrors: [],
      message,
      reasonCodes: [],
    },
    0,
  );
}

function assertIdentifier(value: string, label: string): void {
  if (!IDENTIFIER_PATTERN.test(value)) {
    throw clientInputError(`${label} is invalid.`);
  }
}

function assertVersion(value: number, message: string): void {
  if (!Number.isSafeInteger(value) || value < 1) throw clientInputError(message);
}

function assertExactVersion(value: number, expected: number, message: string): void {
  assertVersion(value, message);
  if (value !== expected) throw clientInputError(message);
}

function isGateReasonCode(value: unknown): value is GateReasonCode {
  return isKnownGateReasonCode(value);
}

function normalizeConfiguredOrigin(
  value: string,
  label: string,
  allowedOrigin: string,
): string {
  if (value !== allowedOrigin) {
    throw clientInputError(
      `The configured ${label} value must be the approved loopback origin.`,
    );
  }
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw clientInputError(`The configured ${label} origin is invalid.`);
  }
  if (
    url.protocol !== "http:" ||
    url.username !== "" ||
    url.password !== "" ||
    (url.pathname !== "" && url.pathname !== "/") ||
    url.search !== "" ||
    url.hash !== "" ||
    url.origin !== allowedOrigin
  ) {
    throw clientInputError(
      `The configured ${label} value must be the approved loopback origin.`,
    );
  }
  return url.origin;
}
