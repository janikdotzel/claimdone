import type {
  CaseState,
  GateDecision,
  GateId,
  GateReasonCode,
  PortalState,
  RequiredClaimField,
} from "../../../../../contracts/generated/claimdone";

export const DEFAULT_CLAIMDONE_API_ORIGIN = "http://127.0.0.1:8000";
export const DEFAULT_CLAIMDONE_PORTAL_ORIGIN = "http://127.0.0.1:3000";

const FLOW_GATE_IDS = ["G0", "G1", "G2", "G3", "G4", "G5"] as const;
const IDENTIFIER_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

export interface CaseView {
  readonly activeClarification: Readonly<Record<string, unknown>> | null;
  readonly caseId: string;
  readonly claimPacket: Readonly<Record<string, unknown>> | null;
  readonly createdAt: string;
  readonly intakeSummary: Readonly<Record<string, unknown>> | null;
  readonly portalState: PortalState;
  readonly redactedMetadata: Readonly<Record<string, string>>;
  readonly state: CaseState;
  readonly updatedAt: string;
  readonly version: number;
}

export interface ClarificationView {
  readonly clarificationId: string;
  readonly expectedVersion: number;
  readonly field: "incident_time";
  readonly question: string;
  readonly round?: number;
}

export interface PortalReviewView {
  readonly renderedValues: Readonly<Record<string, unknown>>;
  readonly reviewUrl: string;
  readonly verificationState: "pending";
}

interface FlowResponseBase {
  readonly case: CaseView;
  readonly draftRevision: number;
  readonly gateHistory: readonly GateDecision[];
  readonly requestId: string;
}

export interface AwaitingClarificationResponse extends FlowResponseBase {
  readonly clarification: ClarificationView;
  readonly phase: "awaiting_clarification";
  readonly portal: null;
}

export interface ReviewResponse extends FlowResponseBase {
  readonly clarification: null;
  readonly phase: "review";
  readonly portal: PortalReviewView;
}

export type IntakeFlowResponse = AwaitingClarificationResponse | ReviewResponse;

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
  readonly onCaseReleased?: (caseId: string) => void;
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
  const configured = process.env.NEXT_PUBLIC_CLAIMDONE_API_ORIGIN?.trim();
  return normalizeConfiguredOrigin(configured || DEFAULT_CLAIMDONE_API_ORIGIN, "API");
}

export function claimDonePortalOrigin(): string {
  const configured = process.env.NEXT_PUBLIC_CLAIMDONE_PORTAL_ORIGIN?.trim();
  return normalizeConfiguredOrigin(
    configured || DEFAULT_CLAIMDONE_PORTAL_ORIGIN,
    "portal",
  );
}

export async function createCase(
  fetcher: ClaimDoneFetch = fetch,
): Promise<CaseView> {
  const body = await requestJson(
    fetcher,
    `${claimDoneApiOrigin()}/api/cases`,
    {
      body: JSON.stringify({ metadata: {} }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
  );
  return parseCaseView(body);
}

export async function createAndSubmitIntake(
  submission: NewIntakeSubmission,
  fetcher: ClaimDoneFetch = fetch,
  lifecycle: IntakeCaseLifecycle = {},
): Promise<AwaitingClarificationResponse> {
  const created = await createCase(fetcher);
  lifecycle.onCaseCreated?.(created.caseId);
  try {
    const response = await submitIntake(
      created.caseId,
      { ...submission, expectedVersion: created.version },
      fetcher,
    );
    lifecycle.onCaseReleased?.(created.caseId);
    return response;
  } catch (primaryError) {
    try {
      await deleteAuthoritativeCase(created.caseId, fetcher);
      lifecycle.onCaseReleased?.(created.caseId);
    } catch (cleanupError) {
      throw new ClaimDonePendingCleanupError(
        created.caseId,
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
  let response: Response;
  try {
    response = await fetcher(
      `${claimDoneApiOrigin()}/api/cases/${encodeURIComponent(caseId)}`,
      { cache: "no-store", method: "DELETE" },
    );
  } catch {
    throw new ClaimDoneApiError(
      {
        code: "CLIENT_NETWORK_ERROR",
        currentVersion: null,
        fieldErrors: [],
        message: "The ClaimDone API could not be reached for cleanup.",
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
    throw invalidResponse("The ClaimDone API returned invalid cleanup JSON.", response.status);
  }
  throw parseErrorEnvelope(body, response.status);
}

export async function deletePortalCase(
  caseId: string,
  fetcher: ClaimDoneFetch = fetch,
): Promise<void> {
  assertIdentifier(caseId, "caseId");
  let response: Response;
  try {
    response = await fetcher(
      `${claimDonePortalOrigin()}/api/sandbox/cases/${encodeURIComponent(caseId)}`,
      { cache: "no-store", method: "DELETE" },
    );
  } catch {
    throw new ClaimDoneApiError(
      {
        code: "CLIENT_NETWORK_ERROR",
        currentVersion: null,
        fieldErrors: [],
        message: "The sandbox portal could not be reached for cleanup.",
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
    throw invalidResponse("The sandbox portal returned invalid cleanup JSON.", response.status);
  }
  throw parseErrorEnvelope(body, response.status);
}

export async function deleteAuthoritativeCase(
  caseId: string,
  fetcher: ClaimDoneFetch = fetch,
): Promise<void> {
  await Promise.all([deleteCase(caseId, fetcher), deletePortalCase(caseId, fetcher)]);
}

export async function submitIntake(
  caseId: string,
  submission: IntakeSubmission,
  fetcher: ClaimDoneFetch = fetch,
): Promise<AwaitingClarificationResponse> {
  assertIdentifier(caseId, "caseId");
  if (submission.images.length !== 3 || submission.exifDecisions.length !== 3) {
    throw clientInputError("Exactly three images and EXIF decisions are required.");
  }
  if (!Number.isSafeInteger(submission.expectedVersion) || submission.expectedVersion < 1) {
    throw clientInputError("The intake requires a valid case version.");
  }
  const hasText = submission.statementText !== null;
  const hasAudio = submission.audio !== null;
  if (hasText === hasAudio) {
    throw clientInputError("Provide exactly one written statement or WAV audio memo.");
  }
  if (submission.audio !== null && !isWavFile(submission.audio)) {
    throw clientInputError("Only WAV audio is supported in this build.");
  }

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
  const parsed = parseFlowResponse(body, caseId);
  if (parsed.phase !== "awaiting_clarification") {
    throw invalidResponse("Intake did not return the required clarification phase.");
  }
  return parsed;
}

export async function answerClarification(
  caseId: string,
  clarificationId: string,
  expectedVersion: number,
  answer: string,
  fetcher: ClaimDoneFetch = fetch,
): Promise<ReviewResponse> {
  assertIdentifier(caseId, "caseId");
  assertIdentifier(clarificationId, "clarificationId");
  if (!Number.isSafeInteger(expectedVersion) || expectedVersion < 1) {
    throw clientInputError("The clarification requires a valid case version.");
  }
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(answer)) {
    throw clientInputError("Enter the incident time as HH:MM.");
  }
  const body = await requestJson(
    fetcher,
    `${claimDoneApiOrigin()}/api/cases/${encodeURIComponent(caseId)}/clarifications/${encodeURIComponent(clarificationId)}/answer`,
    {
      body: JSON.stringify({ answer, expectedVersion }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
  );
  const parsed = parseFlowResponse(body, caseId);
  if (parsed.phase !== "review") {
    throw invalidResponse("Clarification did not return the required review phase.");
  }
  return parsed;
}

export function isWavFile(file: Pick<File, "name" | "type">): boolean {
  const mimeType = file.type.toLowerCase();
  return (
    ["audio/wav", "audio/wave", "audio/x-wav"].includes(mimeType) ||
    (mimeType === "" && file.name.toLowerCase().endsWith(".wav"))
  );
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

function parseErrorEnvelope(value: unknown, status: number): ClaimDoneApiError {
  const envelope = asRecord(value);
  const detail = asRecord(envelope?.error);
  if (!detail || typeof detail.code !== "string" || typeof detail.message !== "string") {
    return invalidResponse("The ClaimDone API returned an invalid error envelope.", status);
  }
  const reasonCodes = Array.isArray(detail.reasonCodes)
    ? detail.reasonCodes.filter(isGateReasonCode)
    : [];
  const fieldErrors = Array.isArray(detail.fieldErrors)
    ? detail.fieldErrors.flatMap((item): ApiFieldError[] => {
        const fieldError = asRecord(item);
        if (
          !fieldError ||
          typeof fieldError.field !== "string" ||
          typeof fieldError.message !== "string"
        ) {
          return [];
        }
        return [
          {
            field: fieldError.field,
            message: fieldError.message,
            reasonCode: isGateReasonCode(fieldError.reasonCode)
              ? fieldError.reasonCode
              : null,
          },
        ];
      })
    : [];
  return new ClaimDoneApiError(
    {
      code: detail.code,
      currentVersion:
        typeof detail.currentVersion === "number" &&
        Number.isSafeInteger(detail.currentVersion)
          ? detail.currentVersion
          : null,
      fieldErrors,
      message: detail.message,
      reasonCodes,
    },
    status,
  );
}

function parseFlowResponse(value: unknown, expectedCaseId: string): IntakeFlowResponse {
  const body = asRecord(value);
  if (!body) throw invalidResponse("The workflow response must be an object.");
  const requestId = requireIdentifier(body.requestId, "requestId");
  const caseView = parseCaseView(body.case);
  if (caseView.caseId !== expectedCaseId) {
    throw invalidResponse("Workflow response caseId does not match the requested case.");
  }
  if (
    typeof body.draftRevision !== "number" ||
    !Number.isSafeInteger(body.draftRevision) ||
    body.draftRevision !== caseView.version
  ) {
    throw invalidResponse("Draft revision does not match the authoritative case version.");
  }
  const gateHistory = parseGateHistory(body.gateHistory);

  if (body.phase === "awaiting_clarification") {
    if (caseView.state !== "awaiting_clarification" || caseView.portalState !== "draft") {
      throw invalidResponse("Clarification phase does not match the case state.");
    }
    const clarification = parseClarification(body.clarification);
    if (clarification.expectedVersion !== caseView.version || body.portal !== null) {
      throw invalidResponse("Clarification is not bound to the current case version.");
    }
    assertGateOutcome(gateHistory, "awaiting_clarification");
    return {
      case: caseView,
      clarification,
      draftRevision: body.draftRevision,
      gateHistory,
      phase: "awaiting_clarification",
      portal: null,
      requestId,
    };
  }

  if (body.phase === "review") {
    if (
      caseView.state !== "verifying" ||
      caseView.portalState !== "review" ||
      body.clarification !== null
    ) {
      throw invalidResponse("Review phase does not match the verifying case boundary.");
    }
    const portal = parsePortal(body.portal, caseView.caseId);
    assertGateOutcome(gateHistory, "review");
    return {
      case: caseView,
      clarification: null,
      draftRevision: body.draftRevision,
      gateHistory,
      phase: "review",
      portal,
      requestId,
    };
  }
  throw invalidResponse("The workflow response has an unknown phase.");
}

function parseCaseView(value: unknown): CaseView {
  const body = asRecord(value);
  if (!body) throw invalidResponse("The case response must be an object.");
  const caseId = requireIdentifier(body.caseId, "caseId");
  if (typeof body.version !== "number" || !Number.isSafeInteger(body.version) || body.version < 1) {
    throw invalidResponse("The case version is invalid.");
  }
  if (!isCaseState(body.state) || !isPortalState(body.portalState)) {
    throw invalidResponse("The case state is invalid.");
  }
  return {
    activeClarification: requireNullableRecord(body.activeClarification, "activeClarification"),
    caseId,
    claimPacket: requireNullableRecord(body.claimPacket, "claimPacket"),
    createdAt: requireNonemptyString(body.createdAt, "createdAt"),
    intakeSummary: requireNullableRecord(body.intakeSummary, "intakeSummary"),
    portalState: body.portalState,
    redactedMetadata: requireStringRecord(body.redactedMetadata, "redactedMetadata"),
    state: body.state,
    updatedAt: requireNonemptyString(body.updatedAt, "updatedAt"),
    version: body.version,
  };
}

function parseClarification(value: unknown): ClarificationView {
  const body = asRecord(value);
  if (
    !body ||
    body.field !== "incident_time" ||
    typeof body.question !== "string" ||
    body.question.trim() === "" ||
    typeof body.expectedVersion !== "number" ||
    !Number.isSafeInteger(body.expectedVersion)
  ) {
    throw invalidResponse("The clarification payload is invalid.");
  }
  const round = body.round;
  if (round !== undefined && (round !== 1 || !Number.isSafeInteger(round))) {
    throw invalidResponse("Only one clarification round is allowed.");
  }
  return {
    clarificationId: requireIdentifier(body.clarificationId, "clarificationId"),
    expectedVersion: body.expectedVersion,
    field: "incident_time",
    question: body.question,
    ...(round === undefined ? {} : { round: 1 }),
  };
}

function parsePortal(value: unknown, caseId: string): PortalReviewView {
  const body = asRecord(value);
  const renderedValues = asRecord(body?.renderedValues);
  if (
    !body ||
    typeof body.reviewUrl !== "string" ||
    body.verificationState !== "pending" ||
    !renderedValues
  ) {
    throw invalidResponse("The portal review payload is invalid.");
  }
  let url: URL;
  try {
    url = new URL(body.reviewUrl);
  } catch {
    throw invalidResponse("The portal review URL is invalid.");
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw invalidResponse("The portal review URL uses an unsafe protocol.");
  }
  let allowedOrigin: URL;
  try {
    allowedOrigin = new URL(claimDonePortalOrigin());
  } catch {
    throw invalidResponse("The configured portal origin is invalid.");
  }
  const expectedPath = `/sandbox/A/cases/${encodeURIComponent(caseId)}`;
  if (
    url.origin !== allowedOrigin.origin ||
    url.username !== "" ||
    url.password !== "" ||
    url.pathname !== expectedPath ||
    url.search !== "" ||
    url.hash !== ""
  ) {
    throw invalidResponse("The portal review URL is outside the approved sandbox route.");
  }
  return {
    renderedValues,
    reviewUrl: url.toString(),
    verificationState: "pending",
  };
}

function parseGateHistory(value: unknown): readonly GateDecision[] {
  if (!Array.isArray(value) || value.length !== FLOW_GATE_IDS.length) {
    throw invalidResponse("The workflow must return exactly G0 through G5.");
  }
  return value.map((item, index) => {
    const gate = asRecord(item);
    const expectedGateId = FLOW_GATE_IDS[index];
    if (
      !gate ||
      gate.gateId !== expectedGateId ||
      gate.contractVersion !== "1.0.0" ||
      typeof gate.decidedAt !== "string" ||
      !Array.isArray(gate.evidenceRefs) ||
      !gate.evidenceRefs.every((reference) => typeof reference === "string") ||
      typeof gate.passed !== "boolean" ||
      typeof gate.deterministicPassed !== "boolean" ||
      typeof gate.modelBlocked !== "boolean" ||
      (gate.passed && (!gate.deterministicPassed || gate.modelBlocked)) ||
      !Array.isArray(gate.reasonCodes) ||
      !gate.reasonCodes.every(isGateReasonCode)
    ) {
      throw invalidResponse(`The ${expectedGateId ?? "unknown"} gate decision is invalid.`);
    }
    return gate as unknown as GateDecision;
  });
}

function assertGateOutcome(
  gates: readonly GateDecision[],
  phase: IntakeFlowResponse["phase"],
): void {
  const last = gates.at(-1);
  const earlierPassed = gates.slice(0, -1).every((gate) => gate.passed);
  const expectedG5Passed = phase === "review";
  const earlierDeterministicPassed = gates
    .slice(0, -1)
    .every((gate) => gate.deterministicPassed && !gate.modelBlocked);
  const expectedG5Reason = phase === "awaiting_clarification"
    ? last?.reasonCodes.includes("G5_REQUIRED_FIELD_MISSING")
    : last?.reasonCodes.length === 0;
  if (
    !earlierPassed ||
    !earlierDeterministicPassed ||
    last?.gateId !== "G5" ||
    last.passed !== expectedG5Passed ||
    last.deterministicPassed !== expectedG5Passed ||
    last.modelBlocked ||
    !expectedG5Reason
  ) {
    throw invalidResponse("Gate history does not authorize the returned workflow phase.");
  }
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
  if (!IDENTIFIER_PATTERN.test(value)) throw clientInputError(`${label} is invalid.`);
}

function requireIdentifier(value: unknown, label: string): string {
  if (typeof value !== "string" || !IDENTIFIER_PATTERN.test(value)) {
    throw invalidResponse(`${label} is invalid.`);
  }
  return value;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function requireNullableRecord(
  value: unknown,
  label: string,
): Readonly<Record<string, unknown>> | null {
  if (value === null) return null;
  const record = asRecord(value);
  if (!record) throw invalidResponse(`${label} must be an object or null.`);
  return record;
}

function requireStringRecord(
  value: unknown,
  label: string,
): Readonly<Record<string, string>> {
  const record = asRecord(value);
  if (!record || Object.values(record).some((item) => typeof item !== "string")) {
    throw invalidResponse(`${label} must contain only string values.`);
  }
  return record as Readonly<Record<string, string>>;
}

function requireNonemptyString(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw invalidResponse(`${label} must be a non-empty string.`);
  }
  return value;
}

function isCaseState(value: unknown): value is CaseState {
  return [
    "created",
    "disclosed",
    "analyzing",
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
  ].includes(value as CaseState);
}

function isPortalState(value: unknown): value is PortalState {
  return ["draft", "review", "human_approved", "receipt"].includes(value as PortalState);
}

function isGateReasonCode(value: unknown): value is GateReasonCode {
  return typeof value === "string" && /^G(?:[0-9]|1[01])_[A-Z0-9_]+$/.test(value);
}

function normalizeConfiguredOrigin(value: string, label: string): string {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw clientInputError(`The configured ${label} origin is invalid.`);
  }
  if (
    (url.protocol !== "http:" && url.protocol !== "https:") ||
    url.username !== "" ||
    url.password !== "" ||
    (url.pathname !== "" && url.pathname !== "/") ||
    url.search !== "" ||
    url.hash !== ""
  ) {
    throw clientInputError(`The configured ${label} value must be an exact HTTP origin.`);
  }
  return url.origin;
}

// These imports are deliberately type-checked against the canonical contract.
void (FLOW_GATE_IDS satisfies readonly GateId[]);
void ("incident_time" satisfies RequiredClaimField);
