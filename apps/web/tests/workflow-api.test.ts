import { afterEach, describe, expect, it, vi } from "vitest";

import {
  buildClarificationAnswerRequest,
  createWorkflowReadTransport,
  WorkflowApiError,
  type WorkflowEventSourcePort,
} from "../src/features/workflow/api";
import {
  CLARIFICATION_SNAPSHOT,
  REVIEW_SNAPSHOT,
  SHOWCASE_EVENTS,
} from "../src/features/workflow/fixtures";

afterEach(() => {
  vi.unstubAllGlobals();
  FakeBrowserEventSource.reset();
});

describe("workflow read transport", () => {
  it("fetches and parses the canonical snapshot without legacy FlowResponse", async () => {
    let observedUrl = "";
    const transport = createWorkflowReadTransport({
      apiOrigin: "http://127.0.0.1:8000/",
      fetcher: async (url) => {
        observedUrl = url;
        return Response.json(REVIEW_SNAPSHOT);
      },
    });
    const snapshot = await transport.getSnapshot("case-happy-001");
    expect(snapshot.case.state).toBe("review");
    expect(observedUrl).toBe("http://127.0.0.1:8000/api/cases/case-happy-001");
  });

  it("builds EventSource-compatible query resume URLs from a persisted cursor", () => {
    const transport = createWorkflowReadTransport({
      apiOrigin: "http://127.0.0.1:8000",
    });
    expect(transport.eventStreamUrl("case-happy-001", null)).toBe(
      "http://127.0.0.1:8000/api/cases/case-happy-001/events",
    );
    expect(transport.eventStreamUrl("case-happy-001", 42)).toBe(
      "http://127.0.0.1:8000/api/cases/case-happy-001/events?after=42",
    );
    expect(() => transport.eventStreamUrl("case-happy-001", 0)).toThrow(
      /positive safe integer/,
    );
  });

  it("fails closed on malformed or extended ErrorEnvelope payloads", async () => {
    const validDetail = {
      code: "CASE_VERSION_CONFLICT",
      currentVersion: 8,
      fieldErrors: [
        {
          field: "expectedVersion",
          message: "The case version is stale.",
          reasonCode: null,
        },
      ],
      gateDecision: null,
      message: "The case version is stale.",
      reasonCodes: [],
    };
    const valid = createWorkflowReadTransport({
      fetcher: async () => Response.json({ error: validDetail }, { status: 409 }),
    });
    await expect(valid.getSnapshot("case-happy-001")).rejects.toMatchObject({
      detail: {
        code: "CASE_VERSION_CONFLICT",
        currentVersion: 8,
        fieldErrors: [{ field: "expectedVersion" }],
      },
      status: 409,
    });

    for (const detail of [
      { ...validDetail, currentVersion: true },
      { ...validDetail, debug: "raw provider response" },
      { ...validDetail, reasonCodes: ["G5_NOT_A_REAL_REASON"] },
      { ...validDetail, fieldErrors: [{ ...validDetail.fieldErrors[0], raw: "secret" }] },
    ]) {
      const transport = createWorkflowReadTransport({
        fetcher: async () => Response.json({ error: detail }, { status: 409 }),
      });
      try {
        await transport.getSnapshot("case-happy-001");
        throw new Error("Expected malformed error envelope to fail");
      } catch (error) {
        expect(error).toBeInstanceOf(WorkflowApiError);
        expect((error as WorkflowApiError).detail.code).toBe("CLIENT_INVALID_RESPONSE");
      }
    }
  });

  it("strictly consumes SSE JSON in order and exposes explicit cleanup", () => {
    const fake = new FakeEventSource();
    const observed: number[] = [];
    const failures: string[] = [];
    let openedUrl = "";
    const transport = createWorkflowReadTransport({
      eventSourceFactory: (url) => {
        openedUrl = url;
        return fake;
      },
    });
    const subscription = transport.subscribeEvents("case-happy-001", null, {
      onEnvelope: (envelope) => observed.push(envelope.cursor),
      onFailure: (error) => failures.push(error.code),
    });
    fake.emit(SHOWCASE_EVENTS[0], "1");
    fake.emit(SHOWCASE_EVENTS[1], "2");
    expect(observed).toEqual([1, 2]);
    expect(failures).toEqual([]);
    expect(openedUrl).not.toContain("after=");
    subscription.close();
    subscription.close();
    expect(fake.closeCount).toBe(1);
  });

  it("uses only the native named workflow event and removes its listener on close", () => {
    vi.stubGlobal("EventSource", FakeBrowserEventSource);
    const observed: number[] = [];
    const failures: string[] = [];
    const transport = createWorkflowReadTransport({
      apiOrigin: "http://127.0.0.1:8000",
    });

    const subscription = transport.subscribeEvents("case-happy-001", 5, {
      onEnvelope: (envelope) => observed.push(envelope.cursor),
      onFailure: (error) => failures.push(error.code),
    });
    const source = FakeBrowserEventSource.onlyInstance();

    expect(source.url).toBe(
      "http://127.0.0.1:8000/api/cases/case-happy-001/events?after=5",
    );
    expect(source.listenerCount("workflow")).toBe(1);
    expect(source.listenerCount("message")).toBe(0);

    source.emitMessage("message", SHOWCASE_EVENTS[5], "6");
    expect(observed).toEqual([]);
    source.emitMessage("workflow", SHOWCASE_EVENTS[5], "6");
    expect(observed).toEqual([6]);
    expect(failures).toEqual([]);

    subscription.close();
    subscription.close();
    expect(source.closeCount).toBe(1);
    expect(source.listenerCount("workflow")).toBe(0);
    expect(source.listenerCount("error")).toBe(0);
    expect(source.removeCount("workflow")).toBe(1);
    expect(source.removeCount("error")).toBe(1);

    source.emitMessage("workflow", SHOWCASE_EVENTS[5], "6");
    source.emitError();
    expect(observed).toEqual([6]);
    expect(failures).toEqual([]);
  });

  it("removes native listeners and closes once when the EventSource fails", () => {
    vi.stubGlobal("EventSource", FakeBrowserEventSource);
    const observed: number[] = [];
    const failures: string[] = [];
    const transport = createWorkflowReadTransport();

    transport.subscribeEvents("case-happy-001", null, {
      onEnvelope: (envelope) => observed.push(envelope.cursor),
      onFailure: (error) => failures.push(error.code),
    });
    const source = FakeBrowserEventSource.onlyInstance();

    source.emitError();
    source.emitError();
    source.emitMessage("workflow", SHOWCASE_EVENTS[0], "1");

    expect(observed).toEqual([]);
    expect(failures).toEqual(["STREAM_UNAVAILABLE"]);
    expect(source.closeCount).toBe(1);
    expect(source.listenerCount("workflow")).toBe(0);
    expect(source.listenerCount("error")).toBe(0);
    expect(source.removeCount("workflow")).toBe(1);
    expect(source.removeCount("error")).toBe(1);
  });

  it("requires a positive SSE lastEventId equal to the database cursor", () => {
    const fake = new FakeEventSource();
    const observed: number[] = [];
    const failures: string[] = [];
    const transport = createWorkflowReadTransport({
      eventSourceFactory: () => fake,
    });
    transport.subscribeEvents("case-happy-001", null, {
      onEnvelope: (envelope) => observed.push(envelope.cursor),
      onFailure: (error) => failures.push(error.code),
    });
    fake.emit(SHOWCASE_EVENTS[0], "");
    expect(observed).toEqual([]);
    expect(failures).toEqual(["STREAM_INVALID_EVENT"]);
    expect(fake.closeCount).toBe(1);
  });

  it("closes and fails on case/cursor tamper, invalid JSON, or stream errors", () => {
    const cases: readonly ((fake: FakeEventSource) => void)[] = [
      (fake) => {
        const event = cloneRecord(SHOWCASE_EVENTS[0]);
        event.caseId = "case-other";
        fake.emit(event, "1");
      },
      (fake) => fake.emit(SHOWCASE_EVENTS[0], "2"),
      (fake) => fake.emitRaw("{not-json", ""),
      (fake) => fake.fail(),
    ];
    for (const trigger of cases) {
      const fake = new FakeEventSource();
      const failures: string[] = [];
      const transport = createWorkflowReadTransport({
        eventSourceFactory: () => fake,
      });
      transport.subscribeEvents("case-happy-001", null, {
        onEnvelope: () => {
          throw new Error("Tampered events must not be delivered");
        },
        onFailure: (error) => failures.push(error.code),
      });
      trigger(fake);
      expect(failures).toHaveLength(1);
      expect(fake.closeCount).toBe(1);
    }
  });

  it("fails closed when an SSE cursor moves backwards after delivery", () => {
    const fake = new FakeEventSource();
    const observed: number[] = [];
    const failures: string[] = [];
    const transport = createWorkflowReadTransport({
      eventSourceFactory: () => fake,
    });
    transport.subscribeEvents("case-happy-001", null, {
      onEnvelope: (envelope) => observed.push(envelope.cursor),
      onFailure: (error) => failures.push(error.code),
    });
    fake.emit(SHOWCASE_EVENTS[1], "2");
    fake.emit(SHOWCASE_EVENTS[0], "1");
    expect(observed).toEqual([2]);
    expect(failures).toEqual(["STREAM_INVALID_EVENT"]);
    expect(fake.closeCount).toBe(1);
  });

  it("does not deliver a verification mismatch event without a mismatch signal", () => {
    const fake = new FakeEventSource();
    const observed: number[] = [];
    const failures: string[] = [];
    const transport = createWorkflowReadTransport({
      eventSourceFactory: () => fake,
    });
    transport.subscribeEvents("case-happy-001", null, {
      onEnvelope: (envelope) => observed.push(envelope.cursor),
      onFailure: (error) => failures.push(error.code),
    });
    const unsupportedMismatch = cloneRecord(SHOWCASE_EVENTS[5]);
    const event = cloneRecord(unsupportedMismatch.event);
    event.status = "mismatch";
    unsupportedMismatch.event = event;
    fake.emit(unsupportedMismatch, "6");

    expect(observed).toEqual([]);
    expect(failures).toEqual(["STREAM_INVALID_EVENT"]);
    expect(fake.closeCount).toBe(1);
  });
});

class FakeEventSource implements WorkflowEventSourcePort {
  closeCount = 0;
  onerror: ((event: Event) => void) | null = null;
  onmessage:
    | ((event: Pick<MessageEvent<string>, "data" | "lastEventId">) => void)
    | null = null;

  close(): void {
    this.closeCount += 1;
  }

  emit(value: unknown, lastEventId: string): void {
    this.emitRaw(JSON.stringify(value), lastEventId);
  }

  emitRaw(data: string, lastEventId: string): void {
    this.onmessage?.({ data, lastEventId });
  }

  fail(): void {
    this.onerror?.(new Event("error"));
  }
}

class FakeBrowserEventSource {
  static instances: FakeBrowserEventSource[] = [];

  static onlyInstance(): FakeBrowserEventSource {
    expect(FakeBrowserEventSource.instances).toHaveLength(1);
    const instance = FakeBrowserEventSource.instances[0];
    if (instance === undefined) throw new Error("Expected an EventSource instance");
    return instance;
  }

  static reset(): void {
    FakeBrowserEventSource.instances = [];
  }

  closeCount = 0;
  onerror: ((event: Event) => void) | null = null;
  onmessage: ((event: FakeSseMessageEvent) => void) | null = null;
  private readonly listeners = new Map<
    string,
    Set<EventListenerOrEventListenerObject>
  >();
  private readonly removals = new Map<string, number>();

  constructor(readonly url: string) {
    FakeBrowserEventSource.instances.push(this);
  }

  addEventListener(
    type: string,
    listener: EventListenerOrEventListenerObject | null,
  ): void {
    if (listener === null) return;
    const listeners = this.listeners.get(type) ?? new Set();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(
    type: string,
    listener: EventListenerOrEventListenerObject | null,
  ): void {
    if (listener === null) return;
    this.listeners.get(type)?.delete(listener);
    this.removals.set(type, (this.removals.get(type) ?? 0) + 1);
  }

  close(): void {
    this.closeCount += 1;
  }

  emitMessage(type: "message" | "workflow", value: unknown, lastEventId: string): void {
    const event = new FakeSseMessageEvent(type, JSON.stringify(value), lastEventId);
    this.dispatch(type, event);
    if (type === "message") this.onmessage?.(event);
  }

  emitError(): void {
    const event = new Event("error");
    this.dispatch("error", event);
    this.onerror?.(event);
  }

  listenerCount(type: string): number {
    return this.listeners.get(type)?.size ?? 0;
  }

  removeCount(type: string): number {
    return this.removals.get(type) ?? 0;
  }

  private dispatch(type: string, event: Event): void {
    for (const listener of [...(this.listeners.get(type) ?? [])]) {
      if (typeof listener === "function") listener(event);
      else listener.handleEvent(event);
    }
  }
}

class FakeSseMessageEvent extends Event {
  constructor(
    type: string,
    readonly data: string,
    readonly lastEventId: string,
  ) {
    super(type);
  }
}

function cloneRecord(value: unknown): Record<string, unknown> {
  const cloned: unknown = structuredClone(value);
  if (typeof cloned !== "object" || cloned === null || Array.isArray(cloned)) {
    throw new Error("Expected a test object");
  }
  return cloned as Record<string, unknown>;
}

describe("clarification payload builder", () => {
  it("preserves exact answer whitespace and every server-bound identity field", () => {
    const clarification = CLARIFICATION_SNAPSHOT.clarification;
    if (clarification === null) throw new Error("Expected clarification fixture");
    const request = buildClarificationAnswerRequest(
      clarification,
      "  2026-07-14\n",
    );
    expect(request).toEqual({
      answer: "  2026-07-14\n",
      caseId: clarification.caseId,
      clarificationId: clarification.clarificationId,
      contractVersion: "3.0.0",
      expectedVersion: clarification.expectedVersion,
      field: clarification.field,
      round: clarification.round,
    });
  });

  it("rejects whitespace-only answers without normalizing valid input", () => {
    const clarification = CLARIFICATION_SNAPSHOT.clarification;
    if (clarification === null) throw new Error("Expected clarification fixture");
    expect(() => buildClarificationAnswerRequest(clarification, " \n\t ")).toThrow(
      /must contain/,
    );
  });
});
