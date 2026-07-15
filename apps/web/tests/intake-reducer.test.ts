import { describe, expect, it } from "vitest";

import {
  evaluateIntakeGates,
  initialIntakeState,
  intakeReducer,
  isInt002ClarificationSnapshot,
  isWorkflowSnapshotState,
  mapBackendValidationErrors,
  type AwaitingClarificationResponse,
  type IntakeImage,
  type IntakeState,
  type ReadyToFillResponse,
} from "../src/features/intake";
import {
  CLARIFICATION_SNAPSHOT,
  READY_SNAPSHOT,
} from "../src/features/workflow/fixtures";
import { parseWorkflowSnapshot } from "../src/features/workflow/validation";

function image(id: string, decision: "strip" | "retain" | null = null): IntakeImage {
  return {
    decision,
    error: null,
    fingerprint: `fixture-${id}`,
    id,
    inspectionStatus: "complete",
    metadataFound: id === "two",
    metadataSummary: id === "two" ? "EXIF detected" : "No EXIF detected",
    mimeType: "image/jpeg",
    name: `${id}.jpg`,
    previewUrl: `blob:${id}`,
    signature: "jpeg",
    size: 1024,
  };
}

function beginIntake() {
  const accepted = intakeReducer(initialIntakeState, {
    type: "SET_DISCLOSURE_ACCEPTED",
    value: true,
  });
  return intakeReducer(accepted, { type: "BEGIN_INTAKE" });
}

function validTextState(): IntakeState {
  let state = beginIntake();
  state = intakeReducer(state, {
    error: null,
    images: [image("one"), image("two"), image("three")],
    type: "ADD_IMAGES",
  });
  state = intakeReducer(state, {
    type: "SET_TEXT_STATEMENT",
    value: "Ich stand an der Ampel, als das andere Fahrzeug auffuhr.",
  });
  for (const consent of ["sandbox", "imageRights", "dataProcessing"] as const) {
    state = intakeReducer(state, { consent, type: "SET_CONSENT", value: true });
  }
  for (const current of state.images) {
    state = intakeReducer(state, {
      decision: current.id === "two" ? "retain" : "strip",
      id: current.id,
      type: "SET_EXIF_DECISION",
    });
  }
  return state;
}

function awaitingResponse(): AwaitingClarificationResponse {
  const body = structuredClone(CLARIFICATION_SNAPSHOT) as MutableRecord;
  record(body.case).caseId = "case-intake-001";
  record(body.case).version = 4;
  record(body.claimPacket).caseId = "case-intake-001";
  const claim = record(record(body.claimPacket).claim);
  const readyClaim = record(record(structuredClone(READY_SNAPSHOT).claimPacket).claim);
  claim.incidentDate = readyClaim.incidentDate;
  claim.incidentTime = null;
  claim.missingRequiredFields = ["incident_time"];
  claim.fieldProvenance = array(readyClaim.fieldProvenance).filter(
    (entry) => record(entry).field !== "incident_time",
  );
  const clarification = record(body.clarification);
  clarification.caseId = "case-intake-001";
  clarification.clarificationId = "clarification-001";
  clarification.expectedVersion = 4;
  clarification.field = "incident_time";
  clarification.question = "Wann ereignete sich der Vorfall?";
  body.requestId = "request-intake-001";
  const parsed = parseWorkflowSnapshot(body, "case-intake-001");
  if (!isInt002ClarificationSnapshot(parsed)) {
    throw new Error("Expected canonical INT-002 clarification fixture");
  }
  return parsed;
}

function readyResponse(): ReadyToFillResponse {
  const body = structuredClone(READY_SNAPSHOT) as MutableRecord;
  record(body.case).caseId = "case-intake-001";
  record(body.case).version = 5;
  record(body.case).updatedAt = "2026-07-14T12:00:21Z";
  record(body.claimPacket).caseId = "case-intake-001";
  body.requestId = "request-ready-intake-001";
  const parsed = parseWorkflowSnapshot(body, "case-intake-001");
  if (!isWorkflowSnapshotState(parsed, "ready_to_fill")) {
    throw new Error("Expected canonical ready fixture");
  }
  return parsed;
}

describe("intake reducer authority and validation", () => {
  it("keeps disclosure separate and cannot skip its acknowledgement", () => {
    const skipped = intakeReducer(initialIntakeState, { type: "BEGIN_INTAKE" });
    expect(skipped.stage).toBe("disclosure");

    const started = beginIntake();
    expect(started.stage).toBe("intake");
    expect(evaluateIntakeGates(started).canContinue).toBe(false);
  });

  it("preserves German and English statement text byte-for-byte", () => {
    const statement = "  Ich hörte „Stopp“ — then I braked.\nKeine Übersetzung.  ";
    const state = intakeReducer(beginIntake(), {
      type: "SET_TEXT_STATEMENT",
      value: statement,
    });
    expect(state.textStatement).toBe(statement);
  });

  it("requires exactly three inspected images, all consents, and a per-image G1 choice", () => {
    let state = beginIntake();
    state = intakeReducer(state, {
      error: null,
      images: [image("one"), image("two")],
      type: "ADD_IMAGES",
    });
    state = intakeReducer(state, {
      type: "SET_TEXT_STATEMENT",
      value: "Synthetic statement",
    });
    expect(evaluateIntakeGates(state).g0.reasonCodes).toContain(
      "G0_IMAGE_COUNT_INVALID",
    );

    state = validTextState();
    const result = evaluateIntakeGates(state);
    expect(result.g0).toEqual({ passed: true, reasonCodes: [] });
    expect(result.g1).toEqual({ passed: true, reasonCodes: [] });
    expect(result.canContinue).toBe(true);

    expect(state.stage).toBe("intake");
    const submitting = intakeReducer(state, {
      kind: "intake",
      token: 1,
      type: "BEGIN_SERVER_REQUEST",
    });
    expect(submitting.stage).toBe("intake");
    const owned = intakeReducer(submitting, {
      caseId: "case-intake-001",
      token: 1,
      type: "SERVER_CASE_CREATED",
    });
    const clarified = intakeReducer(owned, {
      response: awaitingResponse(),
      token: 1,
      type: "SERVER_SUCCEEDED",
    });
    expect(clarified.stage).toBe("awaiting_clarification");
    expect(clarified.serverAuthority?.requestId).toBe("request-intake-001");
    expect(clarified.pendingCaseId).toBeNull();
  });

  it("never starts an authoritative request when local G0 or G1 preflight fails", () => {
    const invalid = beginIntake();
    const advanced = intakeReducer(invalid, {
      kind: "intake",
      token: 1,
      type: "BEGIN_SERVER_REQUEST",
    });
    expect(advanced.stage).toBe("intake");
    expect(advanced.serverRequest).toBeNull();

    let missingPrivacy = validTextState();
    missingPrivacy = {
      ...missingPrivacy,
      images: missingPrivacy.images.map((current, index) =>
        index === 0 ? { ...current, decision: null } : current,
      ),
    };
    expect(evaluateIntakeGates(missingPrivacy).g1.passed).toBe(false);
    expect(
      intakeReducer(missingPrivacy, {
        kind: "intake",
        token: 1,
        type: "BEGIN_SERVER_REQUEST",
      }).serverRequest,
    ).toBeNull();
    expect(
      intakeReducer(missingPrivacy, {
        kind: "intake",
        token: 1,
        type: "BEGIN_SERVER_REQUEST",
      }).stage,
    ).toBe("intake");
  });

  it("does not let a local pass override a server failure", () => {
    const valid = validTextState();
    const submitting = intakeReducer(valid, {
      kind: "intake",
      token: 7,
      type: "BEGIN_SERVER_REQUEST",
    });
    const failed = intakeReducer(submitting, {
      code: "GATE_FAILED",
      currentVersion: 3,
      errors: [{ field: "images", message: "Server rejected an image." }],
      message: "Authoritative G0 failed.",
      reasonCodes: ["G0_IMAGE_TYPE_INVALID"],
      token: 7,
      type: "SERVER_FAILED",
    });

    expect(evaluateIntakeGates(valid).canContinue).toBe(true);
    expect(failed.stage).toBe("intake");
    expect(failed.serverAuthority).toBeNull();
    expect(failed.serverError?.code).toBe("GATE_FAILED");
  });

  it("invalidates server authority after any edit", () => {
    const submitting = intakeReducer(validTextState(), {
      kind: "intake",
      token: 3,
      type: "BEGIN_SERVER_REQUEST",
    });
    const owned = intakeReducer(submitting, {
      caseId: "case-intake-001",
      token: 3,
      type: "SERVER_CASE_CREATED",
    });
    const clarified = intakeReducer(owned, {
      response: awaitingResponse(),
      token: 3,
      type: "SERVER_SUCCEEDED",
    });
    const edited = intakeReducer(clarified, {
      type: "SET_TEXT_STATEMENT",
      value: "Edited after the server pass",
    });

    expect(edited.stage).toBe("intake");
    expect(edited.serverAuthority).toBeNull();
  });

  it("rejects intake mutations and a second case until owned cleanup completes", () => {
    const first = intakeReducer(validTextState(), {
      kind: "intake",
      token: 10,
      type: "BEGIN_SERVER_REQUEST",
    });
    const owned = intakeReducer(first, {
      caseId: "case-intake-001",
      token: 10,
      type: "SERVER_CASE_CREATED",
    });
    const edited = intakeReducer(owned, {
      type: "SET_TEXT_STATEMENT",
      value: "A racing edit",
    });
    const dropped = intakeReducer(owned, {
      error: null,
      images: [image("racing-drop")],
      type: "ADD_IMAGES",
    });
    const reset = intakeReducer(owned, { type: "RESET" });
    const secondRequest = intakeReducer(owned, {
      kind: "intake",
      token: 11,
      type: "BEGIN_SERVER_REQUEST",
    });
    const secondCase = intakeReducer(owned, {
      caseId: "case-intake-002",
      token: 10,
      type: "SERVER_CASE_CREATED",
    });
    const stale = intakeReducer(owned, {
      response: awaitingResponse(),
      token: 11,
      type: "SERVER_SUCCEEDED",
    });

    expect(edited).toBe(owned);
    expect(dropped).toBe(owned);
    expect(reset).toBe(owned);
    expect(secondRequest).toBe(owned);
    expect(secondCase).toBe(owned);
    expect(stale.stage).toBe("intake");
    expect(stale.serverRequest?.token).toBe(10);
    expect(stale.pendingCaseId).toBe("case-intake-001");
    expect(stale.serverAuthority).toBeNull();

    const failed = intakeReducer(owned, {
      code: "INTAKE_FAILED",
      currentVersion: 2,
      errors: [],
      message: "The intake failed and cleanup must finish.",
      reasonCodes: ["G0_IMAGE_TYPE_INVALID"],
      token: 10,
      type: "SERVER_FAILED",
    });
    const retry = intakeReducer(failed, {
      kind: "intake",
      token: 11,
      type: "BEGIN_SERVER_REQUEST",
    });
    const prematureSecondCase = intakeReducer(retry, {
      caseId: "case-intake-002",
      token: 11,
      type: "SERVER_CASE_CREATED",
    });
    const cleaned = intakeReducer(retry, {
      caseId: "case-intake-001",
      type: "SERVER_CASE_CLEANED",
    });
    const replacement = intakeReducer(cleaned, {
      caseId: "case-intake-002",
      token: 11,
      type: "SERVER_CASE_CREATED",
    });

    expect(prematureSecondCase.pendingCaseId).toBe("case-intake-001");
    expect(cleaned.pendingCaseId).toBeNull();
    expect(replacement.pendingCaseId).toBe("case-intake-002");
  });

  it("rejects intake mutations while a clarification answer is pending", () => {
    const intakeRequest = intakeReducer(validTextState(), {
      kind: "intake",
      token: 20,
      type: "BEGIN_SERVER_REQUEST",
    });
    const owned = intakeReducer(intakeRequest, {
      caseId: "case-intake-001",
      token: 20,
      type: "SERVER_CASE_CREATED",
    });
    const clarified = intakeReducer(owned, {
      response: awaitingResponse(),
      token: 20,
      type: "SERVER_SUCCEEDED",
    });
    const answering = intakeReducer(clarified, {
      kind: "clarification",
      token: 21,
      type: "BEGIN_SERVER_REQUEST",
    });

    expect(
      intakeReducer(answering, {
        type: "SET_TEXT_STATEMENT",
        value: "A racing clarification edit",
      }),
    ).toBe(answering);
    expect(intakeReducer(answering, { type: "RESET" })).toBe(answering);
  });

  it("processes clarification success before run start and keeps READY for run-only retry", () => {
    const intakeRequest = intakeReducer(validTextState(), {
      kind: "intake",
      token: 30,
      type: "BEGIN_SERVER_REQUEST",
    });
    const owned = intakeReducer(intakeRequest, {
      caseId: "case-intake-001",
      token: 30,
      type: "SERVER_CASE_CREATED",
    });
    const clarified = intakeReducer(owned, {
      response: awaitingResponse(),
      token: 30,
      type: "SERVER_SUCCEEDED",
    });
    const answering = intakeReducer(clarified, {
      kind: "clarification",
      token: 31,
      type: "BEGIN_SERVER_REQUEST",
    });

    const ready = intakeReducer(answering, {
      response: readyResponse(),
      token: 31,
      type: "SERVER_SUCCEEDED",
    });
    expect(ready.stage).toBe("ready_to_fill");
    expect(ready.serverAuthority?.case.state).toBe("ready_to_fill");
    expect(ready.serverRequest).toBeNull();

    const running = intakeReducer(ready, {
      kind: "run",
      token: 32,
      type: "BEGIN_SERVER_REQUEST",
    });
    expect(running.serverRequest?.kind).toBe("run");

    const failedRun = intakeReducer(running, {
      code: "PORTAL_RUN_FAILED",
      currentVersion: 5,
      errors: [],
      message: "Portal run stopped.",
      reasonCodes: ["G6_STATE_INVALID"],
      token: 32,
      type: "SERVER_FAILED",
    });
    expect(failedRun.stage).toBe("ready_to_fill");
    expect(failedRun.serverAuthority?.case.state).toBe("ready_to_fill");
    expect(failedRun.serverError?.code).toBe("PORTAL_RUN_FAILED");

    const retry = intakeReducer(failedRun, {
      kind: "run",
      token: 33,
      type: "BEGIN_SERVER_REQUEST",
    });
    expect(retry.serverRequest?.kind).toBe("run");
    expect(retry.serverRequest?.token).toBe(33);

    const duplicateAnswer = intakeReducer(failedRun, {
      kind: "clarification",
      token: 33,
      type: "BEGIN_SERVER_REQUEST",
    });
    expect(duplicateAnswer).toBe(failedRun);

    const staleClarification = intakeReducer(retry, {
      response: readyResponse(),
      token: 31,
      type: "SERVER_SUCCEEDED",
    });
    expect(staleClarification).toBe(retry);
  });

  it("treats an in-flight image check as pending, not as a fabricated type failure", () => {
    const valid = validTextState();
    const pending: IntakeState = {
      ...valid,
      images: valid.images.map((current, index) =>
        index === 0
          ? {
              ...current,
              inspectionStatus: "checking",
              metadataFound: null,
              signature: null,
            }
          : current,
      ),
    };
    const result = evaluateIntakeGates(pending);
    expect(result.g0.passed).toBe(false);
    expect(result.g0.reasonCodes).not.toContain("G0_IMAGE_TYPE_INVALID");
    expect(result.g1.reasonCodes).toContain("G1_EXIF_UNREVIEWED");
  });

  it("enforces text XOR audio and the 60-second audio limit", () => {
    let state = intakeReducer(beginIntake(), {
      type: "SET_TEXT_STATEMENT",
      value: "Original text",
    });
    state = intakeReducer(state, { mode: "audio", type: "SET_STATEMENT_MODE" });
    expect(state.textStatement).toBe("");

    state = intakeReducer(state, {
      audio: {
        durationSeconds: null,
        error: null,
        id: "audio-1",
        mimeType: "audio/mpeg",
        name: "memo.mp3",
        previewUrl: "blob:audio",
        status: "checking",
      },
      type: "SET_AUDIO",
    });
    state = intakeReducer(state, {
      durationSeconds: 60.01,
      error: "Audio must be 60 seconds or less.",
      id: "audio-1",
      status: "error",
      type: "COMPLETE_AUDIO_INSPECTION",
    });
    expect(evaluateIntakeGates(state).g0.reasonCodes).toContain(
      "G0_AUDIO_TOO_LONG",
    );

    state = intakeReducer(state, { mode: "text", type: "SET_STATEMENT_MODE" });
    expect(state.audio).toBeNull();
  });

  it("ignores duplicate and fourth images instead of weakening the exact-count rule", () => {
    const state = intakeReducer(beginIntake(), {
      error: "Only 3 images are allowed.",
      images: [image("one"), image("two"), image("three"), image("four"), image("one")],
      type: "ADD_IMAGES",
    });
    expect(state.images.map(({ id }) => id)).toEqual(["one", "two", "three"]);
  });

  it("maps backend validation errors to their field paths and blocks continuation", () => {
    const errors = mapBackendValidationErrors([
      { field: "statement.text", message: "Backend rejected the statement." },
      { field: "privacy.images.two", message: "Reconfirm image privacy." },
      { field: " ", message: "ignored" },
    ]);
    expect(errors).toEqual({
      "privacy.images.two": "Reconfirm image privacy.",
      "statement.text": "Backend rejected the statement.",
    });

    const state = intakeReducer(validTextState(), {
      errors: [{ field: "statement.text", message: "Backend rejected the statement." }],
      type: "SET_BACKEND_ERRORS",
    });
    expect(evaluateIntakeGates(state).canContinue).toBe(false);
    expect(evaluateIntakeGates(state).fieldErrors["statement.text"]).toBe(
      "Backend rejected the statement.",
    );
  });
});

type MutableRecord = Record<string, unknown>;

function record(value: unknown): MutableRecord {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("Expected test record");
  }
  return value as MutableRecord;
}

function array(value: unknown): unknown[] {
  if (!Array.isArray(value)) throw new Error("Expected test array");
  return value;
}
