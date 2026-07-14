import { describe, expect, it } from "vitest";

import {
  evaluateIntakeGates,
  initialIntakeState,
  intakeReducer,
  mapBackendValidationErrors,
  type IntakeImage,
  type IntakeState,
} from "../src/features/intake";

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

    const advanced = intakeReducer(state, { type: "ADVANCE_TO_READY" });
    expect(advanced.stage).toBe("ready");
  });

  it("never lets an action advance when deterministic G0 or G1 fails", () => {
    const invalid = beginIntake();
    const advanced = intakeReducer(invalid, { type: "ADVANCE_TO_READY" });
    expect(advanced.stage).toBe("intake");

    let missingPrivacy = validTextState();
    missingPrivacy = {
      ...missingPrivacy,
      images: missingPrivacy.images.map((current, index) =>
        index === 0 ? { ...current, decision: null } : current,
      ),
    };
    expect(evaluateIntakeGates(missingPrivacy).g1.passed).toBe(false);
    expect(
      intakeReducer(missingPrivacy, { type: "ADVANCE_TO_READY" }).stage,
    ).toBe("intake");
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
