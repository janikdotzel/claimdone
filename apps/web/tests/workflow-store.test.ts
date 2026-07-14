import { describe, expect, it } from "vitest";

import type {
  WorkflowEventEnvelope,
  WorkflowSnapshot,
} from "../../../contracts/generated/claimdone";
import {
  CREATED_SNAPSHOT,
  QUOTA_EVENT,
  REVIEW_SNAPSHOT,
  SHOWCASE_EVENTS,
} from "../src/features/workflow/fixtures";
import {
  INITIAL_WORKFLOW_EVENT_STORE,
  reconnectCursor,
  reduceWorkflowEventStore,
  summarizeWorkflowEvent,
  type WorkflowEventStore,
} from "../src/features/workflow/store";
import {
  parseWorkflowEventEnvelope,
  parseWorkflowSnapshot,
} from "../src/features/workflow/validation";

describe("authoritative snapshot and redacted event reducer", () => {
  it("accepts gaps, stores the reconnect cursor, and refreshes after every accepted event", () => {
    const first = reduceWorkflowEventStore(INITIAL_WORKFLOW_EVENT_STORE, {
      envelope: SHOWCASE_EVENTS[0],
      type: "EVENT_RECEIVED",
    });
    expect(first.needsSnapshotRefresh).toBe(true);
    const gap = eventAtCursor(SHOWCASE_EVENTS[1], 4);
    const second = reduceWorkflowEventStore(first, {
      envelope: gap,
      type: "EVENT_RECEIVED",
    });
    expect(second.events.map((event) => event.cursor)).toEqual([1, 4]);
    expect(reconnectCursor(second)).toBe(4);

    const state = stateEvent(6);
    const observed = reduceWorkflowEventStore(second, {
      envelope: state,
      type: "EVENT_RECEIVED",
    });
    expect(observed.needsSnapshotRefresh).toBe(true);
    expect(observed.snapshot).toBeNull();
    const requested = startSnapshotRequest(observed, CREATED_SNAPSHOT.case.caseId, 1);
    const refreshed = receivePendingSnapshot(requested, CREATED_SNAPSHOT);
    expect(refreshed.snapshot).toBe(CREATED_SNAPSHOT);
    expect(refreshed.needsSnapshotRefresh).toBe(false);
  });

  it("requests a snapshot after same-state gate, clarification, fill, and verification events", () => {
    const authoritativeEvents = [
      SHOWCASE_EVENTS[3],
      clarificationEvent(8),
      SHOWCASE_EVENTS[4],
      SHOWCASE_EVENTS[5],
      stateEvent(9),
    ];

    for (const envelope of authoritativeEvents) {
      const observed = reduceWorkflowEventStore(INITIAL_WORKFLOW_EVENT_STORE, {
        envelope,
        type: "EVENT_RECEIVED",
      });
      expect(observed.needsSnapshotRefresh).toBe(true);

      const requested = startSnapshotRequest(observed, CREATED_SNAPSHOT.case.caseId, 1);
      const refreshed = receivePendingSnapshot(requested, CREATED_SNAPSHOT);
      expect(refreshed.needsSnapshotRefresh).toBe(false);
      expect(refreshed.failedClosed).toBeNull();
    }
  });

  it("ignores an exact latest duplicate and fails closed on collision or backwards cursors", () => {
    const received = reduceWorkflowEventStore(INITIAL_WORKFLOW_EVENT_STORE, {
      envelope: SHOWCASE_EVENTS[0],
      type: "EVENT_RECEIVED",
    });
    expect(
      reduceWorkflowEventStore(received, {
        envelope: structuredClone(SHOWCASE_EVENTS[0]),
        type: "EVENT_RECEIVED",
      }),
    ).toBe(received);

    const collision = cloneRecord(SHOWCASE_EVENTS[0]);
    collision.eventId = "different-event";
    const poisoned = reduceWorkflowEventStore(received, {
      envelope: parseWorkflowEventEnvelope(collision),
      type: "EVENT_RECEIVED",
    });
    expect(poisoned.failedClosed).toMatch(/same cursor/);
    expect(poisoned.needsSnapshotRefresh).toBe(true);
    expect(poisoned.pendingSnapshotRequest).toBeNull();
    expect(poisoned.refreshGeneration).toBe(received.refreshGeneration + 1);

    const requested = startSnapshotRequest(poisoned, REVIEW_SNAPSHOT.case.caseId, 1);
    const snapshotDoesNotHeal = receivePendingSnapshot(requested, REVIEW_SNAPSHOT);
    expect(snapshotDoesNotHeal.failedClosed).toMatch(/same cursor/);
    expect(snapshotDoesNotHeal.needsSnapshotRefresh).toBe(true);
    expect(reconnectCursor(snapshotDoesNotHeal)).toBe(1);

    const reset = reduceWorkflowEventStore(snapshotDoesNotHeal, { type: "STREAM_RESET" });
    expect(reset.failedClosed).toBeNull();
    expect(reset.lastCursor).toBeNull();
    expect(reset.events).toEqual([]);
    expect(reset.snapshot).toBe(REVIEW_SNAPSHOT);
    expect(reset.needsSnapshotRefresh).toBe(true);

    const later = reduceWorkflowEventStore(INITIAL_WORKFLOW_EVENT_STORE, {
      envelope: eventAtCursor(SHOWCASE_EVENTS[1], 5),
      type: "EVENT_RECEIVED",
    });
    const backwards = reduceWorkflowEventStore(later, {
      envelope: eventAtCursor(SHOWCASE_EVENTS[0], 3),
      type: "EVENT_RECEIVED",
    });
    expect(backwards.failedClosed).toMatch(/backwards/);
    expect(backwards.needsSnapshotRefresh).toBe(true);
    expect(backwards.refreshGeneration).toBe(later.refreshGeneration + 1);
  });

  it("invalidates pending snapshot responses on stream failure and cursor poison", () => {
    const caseId = SHOWCASE_EVENTS[0].caseId;
    const cleanFailure = reduceWorkflowEventStore(INITIAL_WORKFLOW_EVENT_STORE, {
      message: "redacted transport detail",
      type: "STREAM_FAILED",
    });
    expect(cleanFailure.failedClosed).toMatch(/event stream failed/);
    expect(cleanFailure.needsSnapshotRefresh).toBe(true);
    expect(cleanFailure.pendingSnapshotRequest).toBeNull();
    expect(cleanFailure.refreshGeneration).toBe(1);

    const pendingFailure = startSnapshotRequest(
      INITIAL_WORKFLOW_EVENT_STORE,
      caseId,
      1,
    );
    const failed = reduceWorkflowEventStore(pendingFailure, {
      message: "redacted transport detail",
      type: "STREAM_FAILED",
    });
    expect(failed.pendingSnapshotRequest).toBeNull();
    expect(failed.refreshGeneration).toBe(pendingFailure.refreshGeneration + 1);
    const resetFailure = reduceWorkflowEventStore(failed, { type: "STREAM_RESET" });
    const lateAfterFailure = reduceWorkflowEventStore(resetFailure, {
      refreshGeneration: pendingFailure.refreshGeneration,
      requestToken: 1,
      snapshot: createdSnapshot(caseId, 8),
      type: "SNAPSHOT_RECEIVED",
    });
    expect(lateAfterFailure).toBe(resetFailure);
    expect(lateAfterFailure.snapshot).toBeNull();
    expect(lateAfterFailure.needsSnapshotRefresh).toBe(true);

    const received = reduceWorkflowEventStore(INITIAL_WORKFLOW_EVENT_STORE, {
      envelope: SHOWCASE_EVENTS[0],
      type: "EVENT_RECEIVED",
    });
    const pendingCollision = startSnapshotRequest(received, caseId, 1);
    const collision = cloneRecord(SHOWCASE_EVENTS[0]);
    collision.eventId = "different-event";
    const poisoned = reduceWorkflowEventStore(pendingCollision, {
      envelope: parseWorkflowEventEnvelope(collision, caseId),
      type: "EVENT_RECEIVED",
    });
    expect(poisoned.failedClosed).toMatch(/same cursor/);
    expect(poisoned.pendingSnapshotRequest).toBeNull();
    expect(poisoned.refreshGeneration).toBe(
      pendingCollision.refreshGeneration + 1,
    );
    const resetCollision = reduceWorkflowEventStore(poisoned, {
      type: "STREAM_RESET",
    });
    const lateAfterCollision = reduceWorkflowEventStore(resetCollision, {
      refreshGeneration: pendingCollision.refreshGeneration,
      requestToken: 1,
      snapshot: createdSnapshot(caseId, 8),
      type: "SNAPSHOT_RECEIVED",
    });
    expect(lateAfterCollision).toBe(resetCollision);
    expect(lateAfterCollision.snapshot).toBeNull();
    expect(lateAfterCollision.needsSnapshotRefresh).toBe(true);
  });

  it("resets stream identity for a new snapshot case and rejects foreign active-case events", () => {
    const caseA = SHOWCASE_EVENTS[0].caseId;
    const caseB = "case-happy-002";
    const receivedA = reduceWorkflowEventStore(INITIAL_WORKFLOW_EVENT_STORE, {
      envelope: SHOWCASE_EVENTS[0],
      type: "EVENT_RECEIVED",
    });
    const collision = cloneRecord(SHOWCASE_EVENTS[0]);
    collision.eventId = "different-event";
    const poisonedA = reduceWorkflowEventStore(receivedA, {
      envelope: parseWorkflowEventEnvelope(collision, caseA),
      type: "EVENT_RECEIVED",
    });
    expect(poisonedA.failedClosed).toMatch(/same cursor/);

    const snapshotB = createdSnapshot(caseB, 1);
    const requestedB = startSnapshotRequest(poisonedA, caseB, 1);
    const switchedToB = receivePendingSnapshot(requestedB, snapshotB);
    expect(switchedToB.activeCaseId).toBe(caseB);
    expect(switchedToB.snapshot).toBe(snapshotB);
    expect(switchedToB.events).toEqual([]);
    expect(switchedToB.lastCursor).toBeNull();
    expect(switchedToB.lastEnvelopeDigest).toBeNull();
    expect(switchedToB.failedClosed).toBeNull();
    expect(switchedToB.needsSnapshotRefresh).toBe(false);

    const firstB = reduceWorkflowEventStore(switchedToB, {
      envelope: eventForCase(SHOWCASE_EVENTS[0], caseB, 1),
      type: "EVENT_RECEIVED",
    });
    expect(firstB.events.map((event) => event.cursor)).toEqual([1]);
    expect(firstB.lastCursor).toBe(1);
    expect(firstB.failedClosed).toBeNull();

    const pendingB = startSnapshotRequest(firstB, caseB, 2);
    const foreignA = reduceWorkflowEventStore(pendingB, {
      envelope: eventForCase(SHOWCASE_EVENTS[1], caseA, 2),
      type: "EVENT_RECEIVED",
    });
    expect(foreignA.failedClosed).toMatch(/another case/);
    expect(foreignA.activeCaseId).toBe(caseB);
    expect(foreignA.events).toEqual(firstB.events);
    expect(foreignA.lastCursor).toBe(1);
    expect(foreignA.needsSnapshotRefresh).toBe(true);
    expect(foreignA.pendingSnapshotRequest).toBeNull();
    expect(foreignA.refreshGeneration).toBe(pendingB.refreshGeneration + 1);
  });

  it("binds snapshot responses to the latest requested case and request token", () => {
    const caseA = SHOWCASE_EVENTS[0].caseId;
    const caseB = "case-happy-002";
    const snapshotA = createdSnapshot(caseA, 1);
    const requestedA = startSnapshotRequest(INITIAL_WORKFLOW_EVENT_STORE, caseA, 1);
    const activeA = receivePendingSnapshot(requestedA, snapshotA);
    const requestedB = startSnapshotRequest(activeA, caseB, 2);

    const lateA = reduceWorkflowEventStore(requestedB, {
      refreshGeneration: requestedA.refreshGeneration,
      requestToken: 1,
      snapshot: createdSnapshot(caseA, 2),
      type: "SNAPSHOT_RECEIVED",
    });
    expect(lateA).toBe(requestedB);
    expect(lateA.activeCaseId).toBe(caseA);
    expect(lateA.needsSnapshotRefresh).toBe(true);
    expect(lateA.pendingSnapshotRequest).toEqual({
      caseId: caseB,
      refreshGeneration: requestedB.refreshGeneration,
      requestToken: 2,
    });

    const snapshotB = createdSnapshot(caseB, 1);
    const activeB = receivePendingSnapshot(lateA, snapshotB);
    expect(activeB.activeCaseId).toBe(caseB);
    expect(activeB.snapshot).toBe(snapshotB);
    expect(activeB.needsSnapshotRefresh).toBe(false);
    expect(activeB.pendingSnapshotRequest).toBeNull();

    const exactRequestB = startSnapshotRequest(activeB, caseB, 3);
    const wrongCaseForExactRequest = reduceWorkflowEventStore(exactRequestB, {
      refreshGeneration: exactRequestB.refreshGeneration,
      requestToken: 3,
      snapshot: createdSnapshot(caseA, 2),
      type: "SNAPSHOT_RECEIVED",
    });
    expect(wrongCaseForExactRequest.failedClosed).toMatch(/requested case/);
    expect(wrongCaseForExactRequest.activeCaseId).toBe(caseB);
    expect(wrongCaseForExactRequest.snapshot).toBe(snapshotB);
    expect(wrongCaseForExactRequest.needsSnapshotRefresh).toBe(true);
    expect(wrongCaseForExactRequest.pendingSnapshotRequest).toBeNull();
    expect(wrongCaseForExactRequest.refreshGeneration).toBe(
      exactRequestB.refreshGeneration + 1,
    );
  });

  it("keeps snapshots monotone and ignores responses from an obsolete refresh generation", () => {
    const caseId = SHOWCASE_EVENTS[0].caseId;
    const version8 = createdSnapshot(caseId, 8);
    const version9 = createdSnapshot(caseId, 9);
    const initialRequest = startSnapshotRequest(
      INITIAL_WORKFLOW_EVENT_STORE,
      caseId,
      1,
    );
    const atVersion8 = receivePendingSnapshot(initialRequest, version8);
    const lowerVersionRequest = startSnapshotRequest(atVersion8, caseId, 2);
    const lowerVersion = receivePendingSnapshot(
      lowerVersionRequest,
      createdSnapshot(caseId, 7),
    );
    expect(lowerVersion).not.toBe(lowerVersionRequest);
    expect(lowerVersion.snapshot?.case.version).toBe(8);
    expect(lowerVersion.needsSnapshotRefresh).toBe(true);
    expect(lowerVersion.pendingSnapshotRequest).toBeNull();
    expect(lowerVersion.failedClosed).toBeNull();
    expect(lowerVersion.refreshGeneration).toBe(
      lowerVersionRequest.refreshGeneration,
    );

    const firstEvent = reduceWorkflowEventStore(lowerVersion, {
      envelope: SHOWCASE_EVENTS[0],
      type: "EVENT_RECEIVED",
    });
    const inFlightGeneration = firstEvent.refreshGeneration;
    const inFlightRequest = startSnapshotRequest(firstEvent, caseId, 3);
    const secondEvent = reduceWorkflowEventStore(inFlightRequest, {
      envelope: SHOWCASE_EVENTS[1],
      type: "EVENT_RECEIVED",
    });
    expect(secondEvent.refreshGeneration).toBe(inFlightGeneration + 1);
    expect(secondEvent.pendingSnapshotRequest).toBeNull();

    const obsoleteResponse = reduceWorkflowEventStore(secondEvent, {
      refreshGeneration: inFlightGeneration,
      requestToken: 3,
      snapshot: version9,
      type: "SNAPSHOT_RECEIVED",
    });
    expect(obsoleteResponse).toBe(secondEvent);
    expect(obsoleteResponse.needsSnapshotRefresh).toBe(true);
    expect(obsoleteResponse.snapshot?.case.version).toBe(8);

    const currentRequest = startSnapshotRequest(obsoleteResponse, caseId, 4);
    const currentResponse = receivePendingSnapshot(currentRequest, version9);
    expect(currentResponse.snapshot).toBe(version9);
    expect(currentResponse.needsSnapshotRefresh).toBe(false);

    const lowerCurrentRequest = startSnapshotRequest(currentResponse, caseId, 5);
    const lateCurrentGenerationVersion8 = receivePendingSnapshot(
      lowerCurrentRequest,
      version8,
    );
    expect(lateCurrentGenerationVersion8).not.toBe(lowerCurrentRequest);
    expect(lateCurrentGenerationVersion8.snapshot).toBe(version9);
    expect(lateCurrentGenerationVersion8.needsSnapshotRefresh).toBe(true);
    expect(lateCurrentGenerationVersion8.pendingSnapshotRequest).toBeNull();
    expect(lateCurrentGenerationVersion8.failedClosed).toBeNull();
  });

  it("preserves an open snapshot refresh across a stream reset", () => {
    const caseId = SHOWCASE_EVENTS[0].caseId;
    const version8 = createdSnapshot(caseId, 8);
    const initialRequest = startSnapshotRequest(
      INITIAL_WORKFLOW_EVENT_STORE,
      caseId,
      1,
    );
    const withSnapshot = receivePendingSnapshot(initialRequest, version8);
    const pendingRefresh = reduceWorkflowEventStore(withSnapshot, {
      envelope: SHOWCASE_EVENTS[0],
      type: "EVENT_RECEIVED",
    });
    const refreshRequest = startSnapshotRequest(pendingRefresh, caseId, 2);
    const reset = reduceWorkflowEventStore(refreshRequest, { type: "STREAM_RESET" });
    expect(reset.activeCaseId).toBe(caseId);
    expect(reset.snapshot).toBe(version8);
    expect(reset.refreshGeneration).toBe(pendingRefresh.refreshGeneration);
    expect(reset.needsSnapshotRefresh).toBe(true);
    expect(reset.pendingSnapshotRequest).toEqual(
      refreshRequest.pendingSnapshotRequest,
    );
    expect(reset.events).toEqual([]);
    expect(reset.lastCursor).toBeNull();

    const refreshed = receivePendingSnapshot(reset, createdSnapshot(caseId, 9));
    expect(refreshed.needsSnapshotRefresh).toBe(false);
    expect(refreshed.snapshot?.case.version).toBe(9);
  });

  it("fails closed on divergent equal-version snapshot authority", () => {
    const caseId = SHOWCASE_EVENTS[0].caseId;
    const version8 = createdSnapshot(caseId, 8);
    const initialRequest = startSnapshotRequest(
      INITIAL_WORKFLOW_EVENT_STORE,
      caseId,
      1,
    );
    const current = receivePendingSnapshot(initialRequest, version8);
    const divergent = cloneRecord(version8);
    record(divergent.case).updatedAt = "2026-07-14T12:00:30Z";
    divergent.requestId = "request-divergent-same-version";
    const parsedDivergent = parseWorkflowSnapshot(divergent, caseId);
    const divergentRequest = startSnapshotRequest(current, caseId, 2);
    const poisoned = receivePendingSnapshot(divergentRequest, parsedDivergent);

    expect(poisoned.snapshot).toBe(version8);
    expect(poisoned.failedClosed).toMatch(/same case version/);
    expect(poisoned.needsSnapshotRefresh).toBe(true);
    expect(poisoned.pendingSnapshotRequest).toBeNull();
    expect(poisoned.refreshGeneration).toBe(
      divergentRequest.refreshGeneration + 1,
    );
  });

  it("uses closed labels and treats quota/billing as terminal external-limit failures", () => {
    const quota = summarizeWorkflowEvent(QUOTA_EVENT);
    expect(quota.label).toContain("insufficient_quota");
    expect(quota.label).toContain("€10 OpenAI project limit");
    expect(quota.label).toContain("No automatic retry");
    expect(quota.label).not.toContain("prompt");

    const gate = summarizeWorkflowEvent(failedGateEvent(8));
    expect(gate.label).toContain("A required claim field is missing");
  });
});

function startSnapshotRequest(
  state: WorkflowEventStore,
  caseId: string,
  requestToken: number,
): WorkflowEventStore {
  return reduceWorkflowEventStore(state, {
    caseId,
    refreshGeneration: state.refreshGeneration,
    requestToken,
    type: "SNAPSHOT_REQUESTED",
  });
}

function receivePendingSnapshot(
  state: WorkflowEventStore,
  snapshot: WorkflowSnapshot,
): WorkflowEventStore {
  const pending = state.pendingSnapshotRequest;
  if (pending === null) throw new Error("Test requires a pending snapshot request");
  return reduceWorkflowEventStore(state, {
    refreshGeneration: pending.refreshGeneration,
    requestToken: pending.requestToken,
    snapshot,
    type: "SNAPSHOT_RECEIVED",
  });
}

function eventAtCursor(
  source: WorkflowEventEnvelope,
  cursor: number,
): WorkflowEventEnvelope {
  return eventForCase(source, source.caseId, cursor);
}

function eventForCase(
  source: WorkflowEventEnvelope,
  caseId: string,
  cursor: number,
): WorkflowEventEnvelope {
  const item = cloneRecord(source);
  item.caseId = caseId;
  item.cursor = cursor;
  item.sourceAuditSequence = cursor;
  item.eventId = `workflow-event-${cursor}`;
  item.sourceAuditEventId = `audit-event-${cursor}`;
  return parseWorkflowEventEnvelope(item, caseId);
}

function createdSnapshot(caseId: string, version: number): WorkflowSnapshot {
  const item = cloneRecord(CREATED_SNAPSHOT);
  const caseView = record(item.case);
  caseView.caseId = caseId;
  caseView.version = version;
  item.requestId = `request-${caseId}-${version}`;
  return parseWorkflowSnapshot(item, caseId);
}

function stateEvent(cursor: number): WorkflowEventEnvelope {
  return parseWorkflowEventEnvelope({
    caseId: "case-happy-001",
    contractVersion: "3.0.0",
    cursor,
    event: { actor: "system", fromState: "created", kind: "state", toState: "disclosed" },
    eventId: `workflow-event-${cursor}`,
    occurredAt: "2026-07-14T12:00:20Z",
    sourceAuditEventId: `audit-event-${cursor}`,
    sourceAuditEventType: "case_state_changed",
    sourceAuditSequence: cursor,
  });
}

function clarificationEvent(cursor: number): WorkflowEventEnvelope {
  return parseWorkflowEventEnvelope({
    caseId: "case-happy-001",
    contractVersion: "3.0.0",
    cursor,
    event: {
      field: "incident_date",
      kind: "clarification",
      round: 1,
      status: "requested",
    },
    eventId: `workflow-event-${cursor}`,
    occurredAt: "2026-07-14T12:00:20Z",
    sourceAuditEventId: `audit-event-${cursor}`,
    sourceAuditEventType: "clarification",
    sourceAuditSequence: cursor,
  });
}

function failedGateEvent(cursor: number): WorkflowEventEnvelope {
  return parseWorkflowEventEnvelope({
    caseId: "case-happy-001",
    contractVersion: "3.0.0",
    cursor,
    event: {
      decision: {
        contractVersion: "3.0.0",
        decidedAt: "2026-07-14T12:00:20Z",
        deterministicPassed: false,
        evidenceRefs: [],
        gateId: "G5",
        modelBlocked: false,
        passed: false,
        reasonCodes: ["G5_REQUIRED_FIELD_MISSING"],
      },
      kind: "gate",
    },
    eventId: `workflow-event-${cursor}`,
    occurredAt: "2026-07-14T12:00:20Z",
    sourceAuditEventId: `audit-event-${cursor}`,
    sourceAuditEventType: "gate_decision",
    sourceAuditSequence: cursor,
  });
}

function cloneRecord(value: unknown): Record<string, unknown> {
  return record(structuredClone(value));
}

function record(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("Test fixture is not an object");
  }
  return value as Record<string, unknown>;
}
