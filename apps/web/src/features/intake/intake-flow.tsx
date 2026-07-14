"use client";

import Image from "next/image";
import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
  type FormEvent,
} from "react";

import {
  Alert,
  ArrowRightIcon,
  Button,
  Card,
  CardContent,
  CardFooter,
  CardHeader,
  CardTitle,
  CheckboxField,
  GateBadge,
  HumanBoundaryCard,
  PageShell,
  StateView,
  Stepper,
  TextArea,
} from "../../components/ui";
import {
  ClaimDoneApiError,
  ClaimDonePendingCleanupError,
  MAX_AUDIO_SECONDS,
  MAX_IMAGE_BYTES,
  PreviewUrlRegistry,
  REQUIRED_IMAGE_COUNT,
  answerClarification,
  createAndSubmitIntake,
  deleteAuthoritativeCase,
  evaluateIntakeGates,
  imageFingerprint,
  initialIntakeState,
  inspectAudioDuration,
  inspectImageFile,
  intakeReducer,
  isWavFile,
  isSupportedImageMime,
  validateAudioDuration,
  type BackendValidationError,
  type IntakeImage,
  type StatementMode,
} from ".";

const intakeSteps = [
  {
    description: "Understand the sandbox boundary",
    id: "disclosure",
    label: "Disclosure",
  },
  {
    description: "Add staged evidence and choices",
    id: "intake",
    label: "Intake",
  },
  {
    description: "One bounded server question",
    id: "clarification",
    label: "Clarify",
  },
  {
    description: "Portal A awaits human review",
    id: "review",
    label: "Review",
  },
] as const;

const NO_BACKEND_ERRORS: ReadonlyArray<BackendValidationError> = [];
const gateReasonLabels: Readonly<Record<string, string>> = {
  G0_AUDIO_TOO_LONG: "Audio is longer than 60 seconds",
  G0_CONSENT_MISSING: "Required consent is missing",
  G0_IMAGE_COUNT_INVALID: "Exactly three images are required",
  G0_IMAGE_TOO_LARGE: "An image is larger than 10 MB",
  G0_IMAGE_TYPE_INVALID: "An image type or signature is invalid",
  G0_INPUT_MODE_INVALID: "Choose valid text or audio input",
  G1_EXIF_UNREVIEWED: "Review metadata for every image",
};

function formatBytes(bytes: number) {
  return bytes >= 1024 * 1024
    ? `${(bytes / (1024 * 1024)).toFixed(1)} MB`
    : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function FieldErrorList({
  errors,
  label,
}: Readonly<{ errors: ReadonlyArray<string>; label: string }>) {
  if (errors.length === 0) return null;
  return (
    <div className="backend-error-list" role="alert">
      <p>{label}</p>
      <ul>
        {[...new Set(errors)].map((error) => (
          <li key={error}>{error}</li>
        ))}
      </ul>
    </div>
  );
}

function disclosureView({
  accepted,
  onAcceptedChange,
  onContinue,
}: Readonly<{
  accepted: boolean;
  onAcceptedChange: (value: boolean) => void;
  onContinue: () => void;
}>) {
  return (
    <Card aria-labelledby="disclosure-title">
      <CardHeader>
        <p className="section-heading__eyebrow">Step 1 · Disclosure</p>
        <CardTitle id="disclosure-title">Before you add any evidence</CardTitle>
      </CardHeader>
      <CardContent className="stack stack--large">
        <Alert title="This is a local sandbox" tone="info">
          ClaimDone prepares a staged draft. It does not contact an insurer, submit a
          claim, approve a payment, or perform a real-world action.
        </Alert>
        <div className="disclosure-grid">
          <div>
            <h3>What happens here</h3>
            <ul className="check-list">
              <li>Three staged images are checked locally before analysis.</li>
              <li>Your statement is preserved exactly as entered.</li>
              <li>Every image gets its own metadata privacy choice.</li>
            </ul>
          </div>
          <div>
            <h3>Where automation stops</h3>
            <ul className="check-list">
              <li>Deterministic gates can stop the flow at any time.</li>
              <li>The agent can prepare a draft only up to verified review.</li>
              <li>A separate human action is required for approval.</li>
            </ul>
          </div>
        </div>
        <CheckboxField
          checked={accepted}
          description="You will provide the required consents again with the staged evidence."
          id="disclosure-acknowledgement"
          label="I understand the sandbox and human-approval boundary"
          onChange={(event) => onAcceptedChange(event.currentTarget.checked)}
        />
      </CardContent>
      <CardFooter>
        <Button
          disabled={!accepted}
          leadingIcon={<ArrowRightIcon />}
          onClick={onContinue}
        >
          Continue to intake
        </Button>
      </CardFooter>
    </Card>
  );
}

interface ClarificationCardProps {
  readonly busy: boolean;
  readonly clarification: Readonly<{
    clarificationId: string;
    expectedVersion: number;
    field: "incident_time";
    question: string;
  }>;
  readonly error: string | null;
  readonly onAnswerChange: (value: string) => void;
  readonly onReset: () => void;
  readonly onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  readonly resetting: boolean;
  readonly value: string;
}

export function ClarificationCard({
  busy,
  clarification,
  error,
  onAnswerChange,
  onReset,
  onSubmit,
  resetting,
  value,
}: ClarificationCardProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    inputRef.current?.focus();
  }, [clarification.clarificationId, error]);

  return (
    <Card aria-labelledby="clarification-title">
      <form onSubmit={onSubmit}>
        <CardHeader>
          <p className="section-heading__eyebrow">One bounded clarification</p>
          <CardTitle id="clarification-title">One detail is still required</CardTitle>
          <p className="card__description">
            The authoritative server gates stopped before portal fill. Answer this one
            question to trigger a full deterministic G0–G5 rerun.
          </p>
        </CardHeader>
        <CardContent className="stack stack--medium">
          <Alert title="Server question" tone="info">
            {clarification.question}
          </Alert>
          <div className="field">
            <div className="field__label-row">
              <label className="field__label" htmlFor="clarification-incident-time">
                Incident time
              </label>
            </div>
            <p className="field__description" id="clarification-time-help">
              Use 24-hour time in HH:MM format. Press Enter to continue.
            </p>
            <input
              aria-describedby={
                error === null
                  ? "clarification-time-help"
                  : "clarification-time-help clarification-time-error"
              }
              aria-invalid={error === null ? undefined : true}
              autoComplete="off"
              className="text-input"
              disabled={busy || resetting}
              id="clarification-incident-time"
              onChange={(event) => onAnswerChange(event.currentTarget.value)}
              ref={inputRef}
              required
              step={60}
              type="time"
              value={value}
            />
            {error !== null ? (
              <p className="field__error" id="clarification-time-error" role="alert">
                {error}
              </p>
            ) : null}
          </div>
          <p className="server-binding">
            Bound to server version {clarification.expectedVersion} · field {clarification.field}
          </p>
        </CardContent>
        <CardFooter>
          <Button
            disabled={busy || resetting || value.length === 0}
            leadingIcon={<ArrowRightIcon />}
            type="submit"
          >
            {busy ? "Checking and filling Portal A…" : "Answer and continue"}
          </Button>
          <Button
            disabled={busy || resetting}
            onClick={onReset}
            type="button"
            variant="secondary"
          >
            {resetting ? "Deleting server case…" : "Start over"}
          </Button>
        </CardFooter>
      </form>
    </Card>
  );
}

export function IntakeFlow({
  backendErrors = NO_BACKEND_ERRORS,
}: Readonly<{ backendErrors?: ReadonlyArray<BackendValidationError> | undefined }>) {
  const [state, dispatch] = useReducer(
    intakeReducer,
    backendErrors,
    (errors) =>
      errors.length === 0
        ? initialIntakeState
        : intakeReducer(initialIntakeState, {
            errors,
            type: "SET_BACKEND_ERRORS",
          }),
  );
  const [isDragging, setIsDragging] = useState(false);
  const [clarificationAnswer, setClarificationAnswer] = useState("");
  const [clarificationError, setClarificationError] = useState<string | null>(null);
  const [cleanupError, setCleanupError] = useState<string | null>(null);
  const [isResetting, setIsResetting] = useState(false);
  const [pendingCleanupCaseId, setPendingCleanupCaseId] = useState<string | null>(null);
  const previewRegistry = useRef<PreviewUrlRegistry | null>(null);
  const activeAudioPreview = useRef<string | null>(null);
  const audioSourceFile = useRef<File | null>(null);
  const sourceFiles = useRef(new Map<string, File>());
  const nextId = useRef(0);
  const nextRequestToken = useRef(0);
  const cleanupErrorRef = useRef<HTMLDivElement>(null);
  const serverErrorRef = useRef<HTMLDivElement>(null);
  const gateResult = useMemo(() => evaluateIntakeGates(state), [state]);

  const registerPreview = useCallback((file: File) => {
    previewRegistry.current ??= new PreviewUrlRegistry({
      createObjectURL: (blob) => URL.createObjectURL(blob),
      revokeObjectURL: (url) => URL.revokeObjectURL(url),
    });
    return previewRegistry.current.create(file);
  }, []);

  const releasePreview = useCallback((url: string) => {
    previewRegistry.current?.release(url);
  }, []);

  useEffect(
    () => () => {
      previewRegistry.current?.releaseAll();
      activeAudioPreview.current = null;
      audioSourceFile.current = null;
      sourceFiles.current.clear();
    },
    [],
  );

  useEffect(() => {
    dispatch({ errors: backendErrors, type: "SET_BACKEND_ERRORS" });
  }, [backendErrors]);

  useEffect(() => {
    if (state.serverError !== null) serverErrorRef.current?.focus();
  }, [state.serverError]);

  useEffect(() => {
    if (cleanupError !== null) cleanupErrorRef.current?.focus();
  }, [cleanupError]);

  const allocateId = useCallback((prefix: string) => {
    nextId.current += 1;
    return `${prefix}-${nextId.current}`;
  }, []);

  const addImages = useCallback(
    (files: ReadonlyArray<File>) => {
      const existing = new Set(
        [...sourceFiles.current.values()].map((file) => imageFingerprint(file)),
      );
      const accepted: IntakeImage[] = [];
      const errors: string[] = [];
      let available = REQUIRED_IMAGE_COUNT - sourceFiles.current.size;

      for (const file of files) {
        const fingerprint = imageFingerprint(file);
        if (!isSupportedImageMime(file.type)) {
          errors.push(`${file.name}: choose a JPG or PNG image.`);
          continue;
        }
        if (file.size <= 0 || file.size > MAX_IMAGE_BYTES) {
          errors.push(`${file.name}: each image must be between 1 byte and 10 MB.`);
          continue;
        }
        if (existing.has(fingerprint)) {
          errors.push(`${file.name}: this file is already selected.`);
          continue;
        }
        if (available <= 0) {
          errors.push(`Only ${REQUIRED_IMAGE_COUNT} images are allowed.`);
          continue;
        }

        const id = allocateId("image");
        const previewUrl = registerPreview(file);
        accepted.push({
          decision: null,
          error: null,
          fingerprint,
          id,
          inspectionStatus: "checking",
          metadataFound: null,
          metadataSummary: "Checking signature and EXIF metadata locally…",
          mimeType: file.type,
          name: file.name,
          previewUrl,
          signature: null,
          size: file.size,
        });
        sourceFiles.current.set(id, file);
        existing.add(fingerprint);
        available -= 1;
      }

      dispatch({
        error: errors.length > 0 ? errors.join(" ") : null,
        images: accepted,
        type: "ADD_IMAGES",
      });

      for (const image of accepted) {
        const file = sourceFiles.current.get(image.id);
        if (file === undefined) continue;
        void inspectImageFile(file)
          .then((result) => {
            dispatch({
              error: result.error,
              id: image.id,
              metadataFound: result.metadataFound,
              metadataSummary: result.metadataSummary,
              signature: result.signature,
              status: result.error === null ? "complete" : "error",
              type: "COMPLETE_IMAGE_INSPECTION",
            });
          })
          .catch(() => {
            dispatch({
              error: "The local image inspection could not finish.",
              id: image.id,
              metadataFound: null,
              metadataSummary: "Inspection failed",
              signature: null,
              status: "error",
              type: "COMPLETE_IMAGE_INSPECTION",
            });
          });
      }
    },
    [allocateId, registerPreview],
  );

  const handleImageInput = (event: ChangeEvent<HTMLInputElement>) => {
    addImages(Array.from(event.currentTarget.files ?? []));
    event.currentTarget.value = "";
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setIsDragging(false);
    addImages(Array.from(event.dataTransfer.files));
  };

  const removeImage = (image: IntakeImage) => {
    sourceFiles.current.delete(image.id);
    releasePreview(image.previewUrl);
    dispatch({ id: image.id, type: "REMOVE_IMAGE" });
  };

  const changeStatementMode = (mode: StatementMode) => {
    if (mode === "text" && activeAudioPreview.current !== null) {
      releasePreview(activeAudioPreview.current);
      activeAudioPreview.current = null;
      audioSourceFile.current = null;
    }
    dispatch({ mode, type: "SET_STATEMENT_MODE" });
  };

  const handleAudioInput = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = "";
    if (file === undefined) return;

    if (activeAudioPreview.current !== null) {
      releasePreview(activeAudioPreview.current);
      activeAudioPreview.current = null;
    }
    if (!isWavFile(file)) {
      const id = allocateId("audio");
      audioSourceFile.current = null;
      dispatch({
        audio: {
          durationSeconds: null,
          error: "Choose a WAV audio file for this build.",
          id,
          mimeType: file.type,
          name: file.name,
          previewUrl: "",
          status: "error",
        },
        type: "SET_AUDIO",
      });
      return;
    }

    const id = allocateId("audio");
    const previewUrl = registerPreview(file);
    activeAudioPreview.current = previewUrl;
    audioSourceFile.current = file;
    dispatch({
      audio: {
        durationSeconds: null,
        error: null,
        id,
        mimeType: file.type,
        name: file.name,
        previewUrl,
        status: "checking",
      },
      type: "SET_AUDIO",
    });

    void inspectAudioDuration(previewUrl)
      .then((durationSeconds) => {
        const error = validateAudioDuration(durationSeconds);
        dispatch({
          durationSeconds,
          error,
          id,
          status: error === null ? "complete" : "error",
          type: "COMPLETE_AUDIO_INSPECTION",
        });
      })
      .catch(() => {
        dispatch({
          durationSeconds: null,
          error: "The audio duration could not be read.",
          id,
          status: "error",
          type: "COMPLETE_AUDIO_INSPECTION",
        });
      });
  };

  const removeAudio = () => {
    if (activeAudioPreview.current !== null) {
      releasePreview(activeAudioPreview.current);
      activeAudioPreview.current = null;
    }
    audioSourceFile.current = null;
    dispatch({ type: "REMOVE_AUDIO" });
  };

  const clearLocalState = () => {
    nextRequestToken.current += 1;
    previewRegistry.current?.releaseAll();
    activeAudioPreview.current = null;
    audioSourceFile.current = null;
    sourceFiles.current.clear();
    setClarificationAnswer("");
    setClarificationError(null);
    setCleanupError(null);
    setPendingCleanupCaseId(null);
    dispatch({ type: "RESET" });
  };

  const reset = () => {
    const caseId = state.serverAuthority?.case.caseId ?? pendingCleanupCaseId;
    if (caseId === undefined || caseId === null) {
      clearLocalState();
      return;
    }
    setIsResetting(true);
    setCleanupError(null);
    void deleteAuthoritativeCase(caseId)
      .then(() => {
        clearLocalState();
      })
      .catch((error: unknown) => {
        setCleanupError(
          error instanceof Error
            ? error.message
            : "The server case could not be deleted. Local evidence was preserved.",
        );
      })
      .finally(() => setIsResetting(false));
  };

  const dispatchServerError = (token: number, error: unknown) => {
    const apiError =
      error instanceof ClaimDoneApiError
        ? error
        : new ClaimDoneApiError(
            {
              code: "CLIENT_UNEXPECTED_ERROR",
              currentVersion: null,
              fieldErrors: [],
              message: "The workflow could not complete this request.",
              reasonCodes: [],
            },
            0,
          );
    dispatch({
      code: apiError.detail.code,
      currentVersion: apiError.detail.currentVersion,
      errors: apiError.detail.fieldErrors.map(({ field, message }) => ({ field, message })),
      message: apiError.message,
      reasonCodes: apiError.detail.reasonCodes,
      token,
      type: "SERVER_FAILED",
    });
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!gateResult.canContinue) return;
    const images = state.images.flatMap((image) => {
      const file = sourceFiles.current.get(image.id);
      return file === undefined ? [] : [file];
    });
    const exifDecisions = state.images.flatMap((image) =>
      image.decision === null ? [] : [image.decision],
    );
    if (
      images.length !== REQUIRED_IMAGE_COUNT ||
      exifDecisions.length !== REQUIRED_IMAGE_COUNT ||
      (state.statementMode === "audio" && audioSourceFile.current === null)
    ) {
      const token = ++nextRequestToken.current;
      dispatch({ kind: "intake", token, type: "BEGIN_SERVER_REQUEST" });
      dispatchServerError(
        token,
        new ClaimDoneApiError(
          {
            code: "CLIENT_SOURCE_FILE_MISSING",
            currentVersion: null,
            fieldErrors: [],
            message: "One or more selected source files are no longer available.",
            reasonCodes: [],
          },
          0,
        ),
      );
      return;
    }

    const token = ++nextRequestToken.current;
    const cleanupBeforeRetry = pendingCleanupCaseId;
    dispatch({ kind: "intake", token, type: "BEGIN_SERVER_REQUEST" });
    void (async () => {
      if (cleanupBeforeRetry !== null) {
        await deleteAuthoritativeCase(cleanupBeforeRetry);
        setPendingCleanupCaseId(null);
        setCleanupError(null);
      }
      return createAndSubmitIntake(
        {
          audio: state.statementMode === "audio" ? audioSourceFile.current : null,
          dataProcessingApproved: state.consents.dataProcessing,
          exifDecisions,
          imageRightsConfirmed: state.consents.imageRights,
          images,
          sandboxAcknowledged: state.consents.sandbox,
          statementText: state.statementMode === "text" ? state.textStatement : null,
        },
        fetch,
        {
          onCaseCreated: setPendingCleanupCaseId,
          onCaseReleased: () => setPendingCleanupCaseId(null),
        },
      );
    })()
      .then((response) => {
        dispatch({ response, token, type: "SERVER_SUCCEEDED" });
      })
      .catch((error: unknown) => {
        if (error instanceof ClaimDonePendingCleanupError) {
          setPendingCleanupCaseId(error.pendingCaseId);
          setCleanupError(
            error.cleanupError instanceof Error
              ? error.cleanupError.message
              : "The server resources could not be fully deleted.",
          );
          dispatchServerError(token, error.primaryError);
          return;
        }
        if (cleanupBeforeRetry !== null) {
          setCleanupError(
            error instanceof Error
              ? error.message
              : "The pending server case still could not be deleted.",
          );
        }
        dispatchServerError(token, error);
      });
  };

  const submitClarification = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const authority = state.serverAuthority;
    if (authority?.phase !== "awaiting_clarification") return;
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(clarificationAnswer)) {
      setClarificationError("Enter a valid 24-hour time in HH:MM format.");
      return;
    }
    setClarificationError(null);
    const token = ++nextRequestToken.current;
    dispatch({ kind: "clarification", token, type: "BEGIN_SERVER_REQUEST" });
    void answerClarification(
      authority.case.caseId,
      authority.clarification.clarificationId,
      authority.clarification.expectedVersion,
      clarificationAnswer,
    )
      .then((response) => {
        dispatch({ response, token, type: "SERVER_SUCCEEDED" });
      })
      .catch((error: unknown) => dispatchServerError(token, error));
  };

  const imageSectionError =
    state.clientErrors.images ?? gateResult.fieldErrors.images;
  const statementError =
    gateResult.fieldErrors.statement ??
    gateResult.fieldErrors[
      state.statementMode === "text" ? "statement.text" : "statement.audio"
    ];
  const currentIndex =
    state.stage === "disclosure"
      ? 0
      : state.stage === "intake"
        ? 1
        : state.stage === "awaiting_clarification"
          ? 2
          : 3;
  const authoritativeG0 = state.serverAuthority?.gateHistory.find(
    (decision) => decision.gateId === "G0",
  );
  const authoritativeG1 = state.serverAuthority?.gateHistory.find(
    (decision) => decision.gateId === "G1",
  );
  const g0Status = authoritativeG0?.passed ? "passed" : "pending";
  const g1Status = authoritativeG1?.passed ? "passed" : "pending";
  const backendErrorEntries = Object.entries(state.backendErrors);
  const imageBackendErrors = backendErrorEntries
    .filter(([field]) => field === "images" || field.startsWith("images.") || field.startsWith("privacy"))
    .map(([, message]) => message);
  const statementBackendErrors = backendErrorEntries
    .filter(([field]) => field === "statement" || field.startsWith("statement."))
    .map(([, message]) => message);
  const consentBackendErrors = backendErrorEntries
    .filter(([field]) => field === "consents" || field.startsWith("consents."))
    .map(([, message]) => message);
  const knownBackendFields = new Set(
    backendErrorEntries
      .filter(
        ([field]) =>
          field === "images" ||
          field.startsWith("images.") ||
          field.startsWith("privacy") ||
          field === "statement" ||
          field.startsWith("statement.") ||
          field === "consents" ||
          field.startsWith("consents."),
      )
      .map(([field]) => field),
  );
  const generalBackendErrors = backendErrorEntries
    .filter(([field]) => !knownBackendFields.has(field))
    .map(([, message]) => message);

  return (
    <PageShell
      aside={
        <div className="stack stack--medium">
          <HumanBoundaryCard />
          <Card tone="soft">
            <CardHeader>
              <CardTitle>Deterministic gate status</CardTitle>
            </CardHeader>
            <CardContent className="stack stack--medium">
              <GateBadge
                gateId="G0"
                label="Intake"
                reason={
                  authoritativeG0?.passed
                    ? `Confirmed by server request ${state.serverAuthority?.requestId ?? ""}`
                    : gateResult.g0.reasonCodes
                        .map((code) => gateReasonLabels[code] ?? code)
                        .join("; ") ||
                      (gateResult.g0.passed
                        ? "Local preflight passed; awaiting server confirmation"
                        : undefined)
                }
                status={g0Status}
              />
              <GateBadge
                gateId="G1"
                label="Privacy"
                reason={
                  authoritativeG1?.passed
                    ? `Confirmed at case version ${state.serverAuthority?.draftRevision ?? ""}`
                    : gateResult.g1.reasonCodes
                        .map((code) => gateReasonLabels[code] ?? code)
                        .join("; ") ||
                      (gateResult.g1.passed
                        ? "Local preflight passed; awaiting server confirmation"
                        : undefined)
                }
                status={g1Status}
              />
              <p className="aside-note">
                Local checks are preflight only. Passed badges appear only after a
                request-ID and version-bound server response.
              </p>
            </CardContent>
          </Card>
        </div>
      }
      description="Add staged evidence, choose privacy handling per image, and pass the local G0/G1 preflight before analysis can begin."
      eyebrow="Sandbox claim preparation"
      title="Prepare a traceable intake"
    >
      <div className="stack stack--large">
        <Card tone="soft">
          <CardContent>
            <Stepper currentIndex={currentIndex} steps={intakeSteps} />
          </CardContent>
        </Card>

        {cleanupError !== null ? (
          <div
            className="server-error-summary"
            ref={cleanupErrorRef}
            role="alert"
            tabIndex={-1}
          >
            <strong>Server cleanup did not complete.</strong>
            <p>{cleanupError}</p>
            <p>The local view and evidence remain available so you can try again.</p>
          </div>
        ) : null}

        {state.stage === "disclosure"
          ? disclosureView({
              accepted: state.disclosureAccepted,
              onAcceptedChange: (value) =>
                dispatch({ type: "SET_DISCLOSURE_ACCEPTED", value }),
              onContinue: () => dispatch({ type: "BEGIN_INTAKE" }),
            })
          : null}

        {state.stage === "intake" ? (
          <form className="intake-form" noValidate onSubmit={submit}>
            {state.serverError !== null ? (
              <div
                className="server-error-summary"
                id="server-error-summary"
                ref={serverErrorRef}
                role="alert"
                tabIndex={-1}
              >
                <strong>Server workflow blocked this request.</strong>
                <p>{state.serverError.message}</p>
                <p>
                  Code {state.serverError.code}
                  {state.serverError.currentVersion === null
                    ? ""
                    : ` · current version ${state.serverError.currentVersion}`}
                </p>
                {state.serverError.reasonCodes.length > 0 ? (
                  <ul>
                    {state.serverError.reasonCodes.map((reasonCode) => (
                      <li key={reasonCode}>{gateReasonLabels[reasonCode] ?? reasonCode}</li>
                    ))}
                  </ul>
                ) : null}
              </div>
            ) : null}
            <fieldset
              className="intake-fieldset"
              disabled={state.serverRequest?.kind === "intake"}
            >
            <FieldErrorList
              errors={generalBackendErrors}
              label="The server returned additional validation errors:"
            />
            <Card aria-labelledby="images-title">
              <CardHeader>
                <p className="section-heading__eyebrow">Evidence</p>
                <CardTitle id="images-title">Add exactly three images</CardTitle>
                <p className="card__description">
                  JPG or PNG only, up to 10 MB each. File signatures and EXIF markers
                  are checked locally.
                </p>
              </CardHeader>
              <CardContent className="stack stack--medium">
                <div
                  className={`drop-zone${isDragging ? " drop-zone--active" : ""}`}
                  onDragEnter={(event) => {
                    event.preventDefault();
                    setIsDragging(true);
                  }}
                  onDragLeave={() => setIsDragging(false)}
                  onDragOver={(event) => event.preventDefault()}
                  onDrop={handleDrop}
                >
                  <p className="drop-zone__title">Drag staged images here</p>
                  <p className="drop-zone__description">
                    {state.images.length} of {REQUIRED_IMAGE_COUNT} selected
                  </p>
                  <input
                    accept="image/jpeg,image/png,.jpg,.jpeg,.png"
                    aria-describedby={
                      imageSectionError === undefined ? "images-help" : "images-help images-error"
                    }
                    aria-invalid={imageSectionError === undefined ? undefined : true}
                    className="visually-hidden"
                    id="claim-images"
                    multiple
                    onChange={handleImageInput}
                    type="file"
                  />
                  <label className="button button--secondary" htmlFor="claim-images">
                    Choose images
                  </label>
                  <span className="visually-hidden" id="images-help">
                    Select exactly three JPG or PNG files, maximum 10 MB per file.
                  </span>
                </div>

                {imageSectionError !== undefined ? (
                  <p className="field__error" id="images-error" role="alert">
                    {imageSectionError}
                  </p>
                ) : null}
                <FieldErrorList
                  errors={imageBackendErrors}
                  label="The server rejected image or privacy fields:"
                />

                {state.images.length === 0 ? (
                  <StateView
                    description="Choose or drop three staged JPG or PNG images to begin."
                    title="No images selected"
                    variant="empty"
                  />
                ) : (
                  <div className="image-grid">
                    {state.images.map((image, index) => {
                      const fileError =
                        gateResult.fieldErrors[`images.${image.id}.file`];
                      const privacyError =
                        gateResult.fieldErrors[`images.${image.id}.metadataDecision`];
                      return (
                        <article className="image-card" key={image.id}>
                          <div className="image-card__preview">
                            {image.inspectionStatus === "error" ? (
                              <span className="image-card__fallback">Invalid image</span>
                            ) : (
                              <Image
                                alt={`Preview ${index + 1}: ${image.name}`}
                                height={168}
                                src={image.previewUrl}
                                unoptimized
                                width={252}
                              />
                            )}
                          </div>
                          <div className="image-card__body">
                            <div className="image-card__heading">
                              <div>
                                <p className="image-card__name">{image.name}</p>
                                <p className="image-card__meta">
                                  Image {index + 1} · {formatBytes(image.size)}
                                </p>
                              </div>
                              <Button
                                aria-label={`Remove ${image.name}`}
                                onClick={() => removeImage(image)}
                                size="small"
                                variant="ghost"
                              >
                                Remove
                              </Button>
                            </div>
                            <p
                              aria-live="polite"
                              className={`metadata-result metadata-result--${image.inspectionStatus}`}
                            >
                              {image.metadataSummary}
                            </p>
                            {fileError !== undefined ? (
                              <p className="field__error" role="alert">
                                {fileError}
                              </p>
                            ) : null}
                            <fieldset
                              aria-describedby={
                                privacyError === undefined
                                  ? undefined
                                  : `metadata-${image.id}-error`
                              }
                              className="choice-fieldset"
                              disabled={image.inspectionStatus !== "complete"}
                            >
                              <legend>Metadata choice for image {index + 1}</legend>
                              <label>
                                <input
                                  checked={image.decision === "strip"}
                                  name={`metadata-${image.id}`}
                                  onChange={() =>
                                    dispatch({
                                      decision: "strip",
                                      id: image.id,
                                      type: "SET_EXIF_DECISION",
                                    })
                                  }
                                  type="radio"
                                />
                                Strip before analysis
                              </label>
                              <label>
                                <input
                                  checked={image.decision === "retain"}
                                  name={`metadata-${image.id}`}
                                  onChange={() =>
                                    dispatch({
                                      decision: "retain",
                                      id: image.id,
                                      type: "SET_EXIF_DECISION",
                                    })
                                  }
                                  type="radio"
                                />
                                Retain for this sandbox
                              </label>
                            </fieldset>
                            {privacyError !== undefined ? (
                              <p
                                className="field__error"
                                id={`metadata-${image.id}-error`}
                                role="alert"
                              >
                                {privacyError}
                              </p>
                            ) : null}
                            {image.decision === "retain" ? (
                              <p className="privacy-warning">
                                Retained metadata may include device or location details.
                              </p>
                            ) : null}
                          </div>
                        </article>
                      );
                    })}
                  </div>
                )}
              </CardContent>
            </Card>

            <Card aria-labelledby="statement-title">
              <CardHeader>
                <p className="section-heading__eyebrow">Statement</p>
                <CardTitle id="statement-title">Use written text or one audio memo</CardTitle>
              </CardHeader>
              <CardContent className="stack stack--medium">
                <FieldErrorList
                  errors={statementBackendErrors}
                  label="The server rejected the statement:"
                />
                <fieldset className="mode-switch">
                  <legend>Statement format</legend>
                  <label>
                    <input
                      checked={state.statementMode === "text"}
                      name="statement-mode"
                      onChange={() => changeStatementMode("text")}
                      type="radio"
                    />
                    Written text
                  </label>
                  <label>
                    <input
                      checked={state.statementMode === "audio"}
                      name="statement-mode"
                      onChange={() => changeStatementMode("audio")}
                      type="radio"
                    />
                    Audio memo
                  </label>
                </fieldset>

                {state.statementMode === "text" ? (
                  <TextArea
                    description="German and English input is preserved exactly—no client-side translation or rewriting."
                    error={statementError}
                    id="claim-statement"
                    label="What happened?"
                    onChange={(event) =>
                      dispatch({
                        type: "SET_TEXT_STATEMENT",
                        value: event.currentTarget.value,
                      })
                    }
                    placeholder="Describe the staged incident in your own words."
                    rows={7}
                    value={state.textStatement}
                  />
                ) : (
                  <div className="audio-field">
                    <label className="field__label" htmlFor="claim-audio">
                      Audio memo
                    </label>
                    <p className="field__description">
                      Choose one WAV file. Its duration is checked locally and must not
                      exceed {MAX_AUDIO_SECONDS} seconds.
                    </p>
                    <input
                      accept=".wav,audio/wav,audio/x-wav"
                      aria-describedby={
                        statementError === undefined ? "audio-help" : "audio-help audio-error"
                      }
                      aria-invalid={statementError === undefined ? undefined : true}
                      id="claim-audio"
                      onChange={handleAudioInput}
                      type="file"
                    />
                    <span className="visually-hidden" id="audio-help">
                      One WAV audio memo, maximum 60 seconds.
                    </span>
                    {state.audio !== null ? (
                      <div className="audio-preview">
                        <div>
                          <p className="audio-preview__name">{state.audio.name}</p>
                          <p aria-live="polite" className="audio-preview__status">
                            {state.audio.status === "checking"
                              ? "Checking duration…"
                              : state.audio.durationSeconds === null
                                ? state.audio.error
                                : `${state.audio.durationSeconds.toFixed(1)} seconds`}
                          </p>
                        </div>
                        {state.audio.previewUrl.length > 0 ? (
                          <audio controls preload="metadata" src={state.audio.previewUrl}>
                            Your browser does not support audio playback.
                          </audio>
                        ) : null}
                        <Button onClick={removeAudio} size="small" variant="ghost">
                          Remove audio
                        </Button>
                      </div>
                    ) : null}
                    {statementError !== undefined ? (
                      <p className="field__error" id="audio-error" role="alert">
                        {statementError}
                      </p>
                    ) : null}
                  </div>
                )}
              </CardContent>
            </Card>

            <Card aria-labelledby="consents-title">
              <CardHeader>
                <p className="section-heading__eyebrow">Required consents</p>
                <CardTitle id="consents-title">Confirm all three before continuing</CardTitle>
              </CardHeader>
              <CardContent className="consent-list">
                <FieldErrorList
                  errors={consentBackendErrors}
                  label="The server rejected one or more consent fields:"
                />
                <CheckboxField
                  checked={state.consents.sandbox}
                  error={gateResult.fieldErrors["consents.sandbox"]}
                  id="consent-sandbox"
                  label="I understand this is a sandbox and no real claim is submitted"
                  onChange={(event) =>
                    dispatch({
                      consent: "sandbox",
                      type: "SET_CONSENT",
                      value: event.currentTarget.checked,
                    })
                  }
                />
                <CheckboxField
                  checked={state.consents.imageRights}
                  error={gateResult.fieldErrors["consents.imageRights"]}
                  id="consent-image-rights"
                  label="I have permission to use these staged images"
                  onChange={(event) =>
                    dispatch({
                      consent: "imageRights",
                      type: "SET_CONSENT",
                      value: event.currentTarget.checked,
                    })
                  }
                />
                <CheckboxField
                  checked={state.consents.dataProcessing}
                  error={gateResult.fieldErrors["consents.dataProcessing"]}
                  id="consent-data-processing"
                  label="I consent to processing this staged evidence for the demo"
                  onChange={(event) =>
                    dispatch({
                      consent: "dataProcessing",
                      type: "SET_CONSENT",
                      value: event.currentTarget.checked,
                    })
                  }
                />
              </CardContent>
              <CardFooter>
                <div aria-live="polite" className="gate-summary">
                  <span>
                    Local G0 preflight {gateResult.g0.passed ? "passed" : "pending"}
                  </span>
                  <span>
                    Local G1 preflight {gateResult.g1.passed ? "passed" : "pending"}
                  </span>
                </div>
                <Button
                  aria-describedby="continue-requirements"
                  disabled={!gateResult.canContinue || state.serverRequest !== null}
                  leadingIcon={<ArrowRightIcon />}
                  type="submit"
                >
                  {state.serverRequest?.kind === "intake"
                    ? "Running server gates…"
                    : state.serverError === null
                      ? "Continue to analysis"
                      : "Try server again"}
                </Button>
                <span className="visually-hidden" id="continue-requirements">
                  Continue is available only after deterministic G0 and G1 preflight
                  checks pass.
                </span>
              </CardFooter>
            </Card>
            </fieldset>
          </form>
        ) : null}

        {state.stage === "awaiting_clarification" &&
        state.serverAuthority?.phase === "awaiting_clarification" ? (
          <ClarificationCard
            busy={state.serverRequest?.kind === "clarification"}
            clarification={state.serverAuthority.clarification}
            error={clarificationError ?? state.serverError?.message ?? null}
            onAnswerChange={(value) => {
              setClarificationAnswer(value);
              setClarificationError(null);
            }}
            onReset={reset}
            onSubmit={submitClarification}
            resetting={isResetting}
            value={clarificationAnswer}
          />
        ) : null}

        {state.stage === "review" && state.serverAuthority?.phase === "review" ? (
          <div className="stack stack--medium">
            <StateView
              description="The server reran G0–G5, filled Sandbox Portal A, and moved the portal to read-only review. Verification remains pending; no approval or submission occurred."
              title="Portal A is ready for human review"
              variant="success"
            />
            <Alert title="Human boundary preserved" tone="warning">
              Verification state is pending. ClaimDone stopped in backend state verifying and
              portal state review; the agent cannot approve or submit this sandbox claim.
            </Alert>
            <Card>
              <CardContent className="ready-summary server-review-summary">
                <div>
                  <span>Case</span>
                  <strong>{state.serverAuthority.case.caseId}</strong>
                </div>
                <div>
                  <span>Authoritative revision</span>
                  <strong>{state.serverAuthority.draftRevision}</strong>
                </div>
                <div>
                  <span>Server request</span>
                  <strong>{state.serverAuthority.requestId}</strong>
                </div>
                <div>
                  <span>Gate history</span>
                  <strong>{state.serverAuthority.gateHistory.map(({ gateId }) => gateId).join(" → ")}</strong>
                </div>
                <div>
                  <span>Portal state</span>
                  <strong>{state.serverAuthority.case.portalState}</strong>
                </div>
                <div>
                  <span>Verification</span>
                  <strong>{state.serverAuthority.portal.verificationState}</strong>
                </div>
              </CardContent>
              <CardFooter>
                <a
                  className="button button--primary"
                  href={state.serverAuthority.portal.reviewUrl}
                  rel="noopener noreferrer"
                  target="_blank"
                >
                  Open Portal A review
                </a>
                <Button disabled={isResetting} onClick={reset} variant="secondary">
                  {isResetting ? "Deleting server case…" : "Start over"}
                </Button>
              </CardFooter>
            </Card>
          </div>
        ) : null}
      </div>
    </PageShell>
  );
}
