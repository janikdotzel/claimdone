import {
  MAX_AUDIO_SECONDS,
  REQUIRED_IMAGE_COUNT,
  type BackendValidationError,
  type IntakeAction,
  type IntakeGateResult,
  type IntakeState,
} from "./types";
import type { GateReasonCode } from "../../../../../contracts/generated/claimdone";

export const initialIntakeState: IntakeState = {
  audio: null,
  backendErrors: {},
  clientErrors: {},
  consents: {
    dataProcessing: false,
    imageRights: false,
    sandbox: false,
  },
  disclosureAccepted: false,
  images: [],
  inputRevision: 0,
  pendingCaseId: null,
  serverAuthority: null,
  serverError: null,
  serverRequest: null,
  stage: "disclosure",
  statementMode: "text",
  textStatement: "",
};

function isInputMutation(action: IntakeAction): boolean {
  return [
    "SET_DISCLOSURE_ACCEPTED",
    "BEGIN_INTAKE",
    "ADD_IMAGES",
    "REMOVE_IMAGE",
    "COMPLETE_IMAGE_INSPECTION",
    "SET_EXIF_DECISION",
    "SET_STATEMENT_MODE",
    "SET_TEXT_STATEMENT",
    "SET_AUDIO",
    "COMPLETE_AUDIO_INSPECTION",
    "REMOVE_AUDIO",
    "SET_CONSENT",
    "RESET",
  ].includes(action.type);
}

function withInputMutation(
  state: IntakeState,
  mutation: Partial<IntakeState>,
): IntakeState {
  return {
    ...state,
    ...mutation,
    inputRevision: state.inputRevision + 1,
    serverAuthority: null,
    serverError: null,
    serverRequest: null,
    stage: state.stage === "disclosure" ? "disclosure" : "intake",
  };
}

function omitErrorPrefixes(
  errors: Readonly<Record<string, string>>,
  prefixes: ReadonlyArray<string>,
) {
  return Object.fromEntries(
    Object.entries(errors).filter(
      ([field]) => !prefixes.some((prefix) => field === prefix || field.startsWith(`${prefix}.`)),
    ),
  );
}

export function mapBackendValidationErrors(
  errors: ReadonlyArray<BackendValidationError>,
): Readonly<Record<string, string>> {
  return Object.fromEntries(
    errors
      .filter(({ field, message }) => field.trim().length > 0 && message.trim().length > 0)
      .map(({ field, message }) => [field, message]),
  );
}

export function evaluateIntakeGates(state: IntakeState): IntakeGateResult {
  const fieldErrors: Record<string, string> = {};
  const g0Reasons: GateReasonCode[] = [];
  const g1Reasons: GateReasonCode[] = [];
  let g0Pending = false;

  if (state.images.length !== REQUIRED_IMAGE_COUNT) {
    g0Reasons.push("G0_IMAGE_COUNT_INVALID");
    fieldErrors.images = `Add exactly ${REQUIRED_IMAGE_COUNT} JPG or PNG images. ${state.images.length} selected.`;
  }

  const invalidImages = state.images.filter(
    (image) =>
      image.inspectionStatus === "error" ||
      (image.inspectionStatus === "complete" &&
        (image.signature === null || image.error !== null)),
  );
  const checkingImages = state.images.filter(
    (image) => image.inspectionStatus === "checking",
  );

  if (invalidImages.length > 0) {
    g0Reasons.push(
      invalidImages.some((image) => image.error?.includes("10 MB"))
        ? "G0_IMAGE_TOO_LARGE"
        : "G0_IMAGE_TYPE_INVALID",
    );
    for (const image of invalidImages) {
      fieldErrors[`images.${image.id}.file`] =
        image.error ?? "This image could not be validated.";
    }
  }
  if (checkingImages.length > 0) {
    g0Pending = true;
    fieldErrors.images = "Wait for local image checks to finish.";
  }

  if (state.statementMode === "text") {
    if (state.textStatement.trim().length === 0) {
      g0Reasons.push("G0_INPUT_MODE_INVALID");
      fieldErrors["statement.text"] = "Enter a written statement or choose an audio memo.";
    }
    if (state.audio !== null) {
      g0Reasons.push("G0_INPUT_MODE_INVALID");
      fieldErrors.statement = "Use either written text or audio, never both.";
    }
  } else {
    if (state.textStatement.length > 0) {
      g0Reasons.push("G0_INPUT_MODE_INVALID");
      fieldErrors.statement = "Use either written text or audio, never both.";
    }
    if (state.audio === null) {
      g0Reasons.push("G0_INPUT_MODE_INVALID");
      fieldErrors["statement.audio"] = "Add one audio memo of 60 seconds or less.";
    } else if (
      state.audio.status !== "complete" ||
      state.audio.durationSeconds === null ||
      state.audio.durationSeconds <= 0 ||
      state.audio.durationSeconds > MAX_AUDIO_SECONDS ||
      state.audio.error !== null
    ) {
      if (state.audio.status === "checking") {
        g0Pending = true;
      } else {
        g0Reasons.push(
          state.audio.durationSeconds !== null &&
            state.audio.durationSeconds > MAX_AUDIO_SECONDS
            ? "G0_AUDIO_TOO_LONG"
            : "G0_INPUT_MODE_INVALID",
        );
      }
      fieldErrors["statement.audio"] =
        state.audio.error ??
        (state.audio.status === "checking"
          ? "Wait for the audio duration check to finish."
          : "Audio must be 60 seconds or less.");
    }
  }

  for (const [consent, value] of Object.entries(state.consents)) {
    if (!value) {
      g0Reasons.push("G0_CONSENT_MISSING");
      fieldErrors[`consents.${consent}`] = "This consent is required.";
    }
  }

  for (const image of state.images) {
    if (image.inspectionStatus !== "complete") {
      g1Reasons.push("G1_EXIF_UNREVIEWED");
    } else if (image.decision === null) {
      g1Reasons.push("G1_EXIF_UNREVIEWED");
      fieldErrors[`images.${image.id}.metadataDecision`] =
        "Choose whether to strip or retain metadata for this image.";
    }
  }

  for (const [field, message] of Object.entries(state.backendErrors)) {
    fieldErrors[field] = message;
    if (field.includes("metadata") || field.startsWith("privacy")) {
      g1Reasons.push("G1_EXIF_UNREVIEWED");
    } else {
      g0Reasons.push("G0_INPUT_MODE_INVALID");
    }
  }

  const g0 = {
    passed: !g0Pending && g0Reasons.length === 0,
    reasonCodes: [...new Set(g0Reasons)],
  };
  const g1 = {
    passed:
      state.images.length === REQUIRED_IMAGE_COUNT &&
      g1Reasons.length === 0 &&
      state.images.every((image) => image.decision !== null),
    reasonCodes: [...new Set(g1Reasons)],
  };

  return {
    canContinue: state.stage === "intake" && g0.passed && g1.passed,
    fieldErrors,
    g0,
    g1,
  };
}

export function intakeReducer(state: IntakeState, action: IntakeAction): IntakeState {
  if (state.serverRequest !== null && isInputMutation(action)) return state;

  switch (action.type) {
    case "SET_DISCLOSURE_ACCEPTED":
      return state.stage === "disclosure"
        ? withInputMutation(state, { disclosureAccepted: action.value })
        : state;

    case "BEGIN_INTAKE":
      return state.stage === "disclosure" && state.disclosureAccepted
        ? {
            ...state,
            inputRevision: state.inputRevision + 1,
            serverAuthority: null,
            serverError: null,
            serverRequest: null,
            stage: "intake",
          }
        : state;

    case "ADD_IMAGES": {
      if (state.stage !== "intake") return state;
      const existing = new Set(state.images.map((image) => image.fingerprint));
      const additions = [];
      for (const image of action.images) {
        if (
          state.images.length + additions.length >= REQUIRED_IMAGE_COUNT ||
          existing.has(image.fingerprint)
        ) {
          continue;
        }
        additions.push(image);
        existing.add(image.fingerprint);
      }
      return withInputMutation(state, {
        backendErrors: omitErrorPrefixes(state.backendErrors, ["images"]),
        clientErrors: action.error === null ? {} : { images: action.error },
        images: [...state.images, ...additions],
      });
    }

    case "REMOVE_IMAGE":
      return withInputMutation(state, {
        backendErrors: omitErrorPrefixes(state.backendErrors, [
          "images",
          `images.${action.id}`,
        ]),
        clientErrors: {},
        images: state.images.filter((image) => image.id !== action.id),
      });

    case "COMPLETE_IMAGE_INSPECTION":
      return withInputMutation(state, {
        images: state.images.map((image) =>
          image.id === action.id
            ? {
                ...image,
                error: action.error,
                inspectionStatus: action.status,
                metadataFound: action.metadataFound,
                metadataSummary: action.metadataSummary,
                signature: action.signature,
              }
            : image,
        ),
      });

    case "SET_EXIF_DECISION":
      return withInputMutation(state, {
        backendErrors: omitErrorPrefixes(state.backendErrors, [
          `images.${action.id}.metadataDecision`,
          "privacy",
        ]),
        images: state.images.map((image) =>
          image.id === action.id && image.inspectionStatus === "complete"
            ? { ...image, decision: action.decision }
            : image,
        ),
      });

    case "SET_STATEMENT_MODE":
      return withInputMutation(state, {
        audio: action.mode === "text" ? null : state.audio,
        backendErrors: omitErrorPrefixes(state.backendErrors, ["statement"]),
        statementMode: action.mode,
        textStatement: action.mode === "audio" ? "" : state.textStatement,
      });

    case "SET_TEXT_STATEMENT":
      return state.statementMode === "text"
        ? withInputMutation(state, {
            backendErrors: omitErrorPrefixes(state.backendErrors, ["statement"]),
            textStatement: action.value,
          })
        : state;

    case "SET_AUDIO":
      return state.statementMode === "audio"
        ? withInputMutation(state, {
            audio: action.audio,
            backendErrors: omitErrorPrefixes(state.backendErrors, ["statement"]),
          })
        : state;

    case "COMPLETE_AUDIO_INSPECTION":
      return state.audio?.id === action.id
        ? withInputMutation(state, {
            audio: {
              ...state.audio,
              durationSeconds: action.durationSeconds,
              error: action.error,
              status: action.status,
            },
          })
        : state;

    case "REMOVE_AUDIO":
      return withInputMutation(state, {
        audio: null,
        backendErrors: omitErrorPrefixes(state.backendErrors, ["statement.audio"]),
      });

    case "SET_CONSENT":
      return withInputMutation(state, {
        backendErrors: omitErrorPrefixes(state.backendErrors, [
          `consents.${action.consent}`,
        ]),
        consents: { ...state.consents, [action.consent]: action.value },
      });

    case "SET_BACKEND_ERRORS":
      return { ...state, backendErrors: mapBackendValidationErrors(action.errors) };

    case "BEGIN_SERVER_REQUEST":
      if (state.serverRequest !== null) return state;
      if (
        action.kind === "intake" &&
        (state.stage !== "intake" || !evaluateIntakeGates(state).canContinue)
      ) {
        return state;
      }
      if (
        action.kind === "clarification" &&
        (state.stage !== "awaiting_clarification" ||
          state.serverAuthority?.case.state !== "awaiting_clarification")
      ) {
        return state;
      }
      if (
        action.kind === "run" &&
        (state.stage !== "ready_to_fill" ||
          state.serverAuthority?.case.state !== "ready_to_fill")
      ) {
        return state;
      }
      return {
        ...state,
        backendErrors: {},
        serverError: null,
        serverRequest: {
          inputRevision: state.inputRevision,
          kind: action.kind,
          token: action.token,
        },
      };

    case "SERVER_CASE_CREATED":
      if (
        state.serverRequest?.kind !== "intake" ||
        state.serverRequest.token !== action.token ||
        state.pendingCaseId !== null
      ) {
        return state;
      }
      return { ...state, pendingCaseId: action.caseId };

    case "SERVER_CASE_CLEANED":
      return state.pendingCaseId === action.caseId
        ? { ...state, pendingCaseId: null }
        : state;

    case "SERVER_SUCCEEDED": {
      const request = state.serverRequest;
      if (
        request === null ||
        request.token !== action.token ||
        request.inputRevision !== state.inputRevision ||
        (request.kind === "intake" &&
          action.response.case.state !== "awaiting_clarification") ||
        (request.kind === "clarification" &&
          action.response.case.state !== "ready_to_fill") ||
        (request.kind === "run" && action.response.case.state !== "review") ||
        (request.kind === "intake" &&
          state.pendingCaseId !== action.response.case.caseId) ||
        (request.kind !== "intake" &&
          state.serverAuthority?.case.caseId !== action.response.case.caseId)
      ) {
        return state;
      }
      return {
        ...state,
        backendErrors: {},
        pendingCaseId: request.kind === "intake" ? null : state.pendingCaseId,
        serverAuthority: action.response,
        serverError: null,
        serverRequest: null,
        stage: action.response.case.state,
      };
    }

    case "SERVER_FAILED": {
      const request = state.serverRequest;
      if (
        request === null ||
        request.token !== action.token ||
        request.inputRevision !== state.inputRevision
      ) {
        return state;
      }
      return {
        ...state,
        backendErrors: mapBackendValidationErrors(action.errors),
        serverError: {
          code: action.code,
          currentVersion: action.currentVersion,
          message: action.message,
          reasonCodes: action.reasonCodes,
        },
        serverRequest: null,
      };
    }

    case "RESET":
      return state.pendingCaseId === null
        ? { ...initialIntakeState, inputRevision: state.inputRevision + 1 }
        : state;
  }
}
