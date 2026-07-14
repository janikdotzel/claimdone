import type {
  ClarificationAnswerRequest,
  ClarificationView,
  GateReasonCode,
  WorkflowEventEnvelope,
  WorkflowSnapshot,
} from "../../../../../contracts/generated/claimdone";

import {
  isKnownGateReasonCode,
  parseWorkflowEventEnvelope,
  parseWorkflowSnapshot,
  validateGateDecisionBoundary,
  WorkflowPayloadError,
} from "./validation";

export type WorkflowFetch = (
  input: string,
  init?: RequestInit,
) => Promise<Response>;

export interface WorkflowFieldError {
  readonly field: string;
  readonly message: string;
  readonly reasonCode: GateReasonCode | null;
}

export interface WorkflowErrorDetail {
  readonly code: string;
  readonly currentVersion: number | null;
  readonly fieldErrors: readonly WorkflowFieldError[];
  readonly message: string;
  readonly reasonCodes: readonly GateReasonCode[];
}

export class WorkflowApiError extends Error {
  constructor(
    readonly detail: WorkflowErrorDetail,
    readonly status: number,
  ) {
    super(detail.message);
    this.name = "WorkflowApiError";
  }
}

export interface WorkflowReadTransport {
  getSnapshot(caseId: string): Promise<WorkflowSnapshot>;
  /**
   * Native EventSource cannot set Last-Event-ID manually. A caller persists the
   * reducer cursor and passes it here; the server resumes strictly after it.
   */
  eventStreamUrl(caseId: string, afterCursor: number | null): string;
  subscribeEvents(
    caseId: string,
    afterCursor: number | null,
    callbacks: WorkflowEventCallbacks,
  ): WorkflowEventSubscription;
}

export interface WorkflowEventCallbacks {
  readonly onEnvelope: (envelope: WorkflowEventEnvelope) => void;
  readonly onFailure: (error: WorkflowStreamError) => void;
}

export interface WorkflowEventSubscription {
  close(): void;
}

export interface WorkflowEventSourcePort {
  close(): void;
  onerror: ((event: Event) => void) | null;
  onmessage:
    | ((event: Pick<MessageEvent<string>, "data" | "lastEventId">) => void)
    | null;
}

export type WorkflowEventSourceFactory = (
  url: string,
) => WorkflowEventSourcePort;

export class WorkflowStreamError extends Error {
  constructor(readonly code: "STREAM_UNAVAILABLE" | "STREAM_INVALID_EVENT") {
    super(
      code === "STREAM_UNAVAILABLE"
        ? "The redacted workflow event stream is unavailable."
        : "The redacted workflow event stream returned an invalid event.",
    );
    this.name = "WorkflowStreamError";
  }
}

export interface WorkflowTransportOptions {
  readonly apiOrigin?: string;
  readonly eventSourceFactory?: WorkflowEventSourceFactory;
  readonly fetcher?: WorkflowFetch;
}

export function createWorkflowReadTransport(
  options: WorkflowTransportOptions = {},
): WorkflowReadTransport {
  const origin = normalizeOrigin(options.apiOrigin ?? defaultApiOrigin());
  const fetcher = options.fetcher ?? fetch;
  const streamUrl = (caseId: string, afterCursor: number | null): string => {
    assertIdentifier(caseId, "caseId");
    if (
      afterCursor !== null &&
      (!Number.isSafeInteger(afterCursor) || afterCursor < 1)
    ) {
      throw clientInputError("The event cursor must be a positive safe integer.");
    }
    const url = new URL(
      `${origin}/api/cases/${encodeURIComponent(caseId)}/events`,
    );
    if (afterCursor !== null) url.searchParams.set("after", String(afterCursor));
    return url.toString();
  };
  const transport: WorkflowReadTransport = {
    async getSnapshot(caseId) {
      assertIdentifier(caseId, "caseId");
      const body = await requestJson(
        fetcher,
        `${origin}/api/cases/${encodeURIComponent(caseId)}`,
      );
      try {
        return parseWorkflowSnapshot(body, caseId);
      } catch (error) {
        if (error instanceof WorkflowPayloadError) {
          throw invalidResponse(`The workflow snapshot is invalid (${error.message}).`);
        }
        throw error;
      }
    },
    eventStreamUrl(caseId, afterCursor) {
      return streamUrl(caseId, afterCursor);
    },
    subscribeEvents(caseId, afterCursor, callbacks) {
      const factory = options.eventSourceFactory ?? browserEventSourceFactory;
      const source = factory(streamUrl(caseId, afterCursor));
      let closed = false;
      let lastDeliveredCursor = afterCursor;
      const fail = (code: WorkflowStreamError["code"]): void => {
        if (closed) return;
        closed = true;
        source.onmessage = null;
        source.onerror = null;
        source.close();
        callbacks.onFailure(new WorkflowStreamError(code));
      };
      source.onmessage = (message) => {
        if (closed) return;
        try {
          const raw: unknown = JSON.parse(message.data);
          const envelope = parseWorkflowEventEnvelope(raw, caseId);
          if (!/^[1-9]\d*$/.test(message.lastEventId)) {
            fail("STREAM_INVALID_EVENT");
            return;
          }
          const lastEventId = Number(message.lastEventId);
          if (!Number.isSafeInteger(lastEventId) || lastEventId !== envelope.cursor) {
            fail("STREAM_INVALID_EVENT");
            return;
          }
          if (
            lastDeliveredCursor !== null &&
            envelope.cursor <= lastDeliveredCursor
          ) {
            fail("STREAM_INVALID_EVENT");
            return;
          }
          lastDeliveredCursor = envelope.cursor;
          callbacks.onEnvelope(envelope);
        } catch {
          fail("STREAM_INVALID_EVENT");
        }
      };
      source.onerror = () => fail("STREAM_UNAVAILABLE");
      return {
        close() {
          if (closed) return;
          closed = true;
          source.onmessage = null;
          source.onerror = null;
          source.close();
        },
      };
    },
  };
  return transport;
}

function browserEventSourceFactory(url: string): WorkflowEventSourcePort {
  if (typeof EventSource === "undefined") {
    throw new WorkflowStreamError("STREAM_UNAVAILABLE");
  }
  const source = new EventSource(url);
  const port: WorkflowEventSourcePort = {
    close() {
      source.close();
    },
    onerror: null,
    onmessage: null,
  };
  source.onmessage = (event) => {
    port.onmessage?.({
      data: typeof event.data === "string" ? event.data : "",
      lastEventId: event.lastEventId,
    });
  };
  source.onerror = (event) => port.onerror?.(event);
  return port;
}

/**
 * Constructs only the typed command payload. Transport and mutation authority
 * intentionally live outside this frontend work package.
 */
export function buildClarificationAnswerRequest(
  clarification: ClarificationView,
  answer: string,
): ClarificationAnswerRequest {
  if (answer.length < 1 || answer.length > 4_000 || answer.trim().length === 0) {
    throw clientInputError("The clarification answer must contain 1 to 4,000 characters.");
  }
  // Do not trim or normalize: backend deterministic normalization happens later.
  return {
    answer,
    caseId: clarification.caseId,
    clarificationId: clarification.clarificationId,
    contractVersion: "3.0.0",
    expectedVersion: clarification.expectedVersion,
    field: clarification.field,
    round: clarification.round,
  };
}

async function requestJson(fetcher: WorkflowFetch, url: string): Promise<unknown> {
  let response: Response;
  try {
    response = await fetcher(url, {
      cache: "no-store",
      headers: { Accept: "application/json" },
      method: "GET",
    });
  } catch {
    throw new WorkflowApiError(
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

function parseErrorEnvelope(value: unknown, status: number): WorkflowApiError {
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
      !/^[A-Z][A-Z0-9_]{0,127}$/.test(detail.code) ||
      !isSafeMessage(detail.message)
    ) {
      throw new Error("invalid error identity");
    }
    if (
      !Array.isArray(detail.reasonCodes) ||
      !detail.reasonCodes.every(isKnownGateReasonCode) ||
      new Set(detail.reasonCodes).size !== detail.reasonCodes.length
    ) {
      throw new Error("invalid reason codes");
    }
    if (!Array.isArray(detail.fieldErrors)) throw new Error("invalid field errors");
    const fieldErrors = detail.fieldErrors.map(parseClosedFieldError);
    if (detail.gateDecision !== null) {
      validateGateDecisionBoundary(detail.gateDecision);
    }
    const currentVersion = parseCurrentVersion(detail.currentVersion);
    return new WorkflowApiError(
      {
        code: detail.code,
        currentVersion,
        fieldErrors,
        message: detail.message,
        reasonCodes: detail.reasonCodes,
      },
      status,
    );
  } catch {
    return invalidResponse("The ClaimDone API returned an invalid error envelope.", status);
  }
}

function parseClosedFieldError(value: unknown): WorkflowFieldError {
  const item = exactRecord(value, ["field", "reasonCode", "message"]);
  if (
    typeof item.field !== "string" ||
    item.field.length < 1 ||
    item.field.length > 256 ||
    !isSafeMessage(item.message) ||
    (item.reasonCode !== null && !isKnownGateReasonCode(item.reasonCode))
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

function isSafeMessage(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 512 &&
    !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(value)
  );
}

function invalidResponse(message: string, status = 502): WorkflowApiError {
  return new WorkflowApiError(
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

function clientInputError(message: string): WorkflowApiError {
  return new WorkflowApiError(
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
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value)) {
    throw clientInputError(`${label} is invalid.`);
  }
}

function defaultApiOrigin(): string {
  return process.env.NEXT_PUBLIC_CLAIMDONE_API_ORIGIN ?? "http://127.0.0.1:8000";
}

function normalizeOrigin(value: string): string {
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    throw clientInputError("The ClaimDone API origin is invalid.");
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw clientInputError("The ClaimDone API origin must use HTTP or HTTPS.");
  }
  parsed.pathname = parsed.pathname.replace(/\/$/, "");
  parsed.search = "";
  parsed.hash = "";
  return parsed.toString().replace(/\/$/, "");
}
