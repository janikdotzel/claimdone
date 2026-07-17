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
  answerThenRunToReview,
  createAndSubmitIntake,
  deleteAuthoritativeCase,
  evaluateIntakeGates,
  imageFingerprint,
  initialIntakeState,
  inspectAudioDuration,
  inspectImageFile,
  intakeReducer,
  isInt002ClarificationSnapshot,
  isWorkflowSnapshotState,
  isWavFile,
  isSupportedImageMime,
  portalAReviewUrl,
  runClaimToReview,
  validateAudioDuration,
  type BackendValidationError,
  type AwaitingClarificationResponse,
  type IntakeFlowResponse,
  type IntakeImage,
  type ReadyToFillResponse,
  type ReviewResponse,
  type StatementMode,
} from ".";
import {
  createWorkflowReadTransport,
  INITIAL_WORKFLOW_EVENT_STORE,
  reduceWorkflowEventStore,
  WorkflowExperience,
} from "../workflow";

const intakeSteps = [
  {
    description: "Three photos and a short statement",
    id: "intake",
    label: "Add evidence",
  },
  {
    description: "We organize, check, and fill gaps",
    id: "clarification",
    label: "Claim Agent",
  },
  {
    description: "Your complete claim, ready to check",
    id: "review",
    label: "Review",
  },
] as const;

const photoRequirements = [
  {
    description: "Show both vehicles and their positions.",
    label: "Overview",
  },
  {
    description: "Move close enough to show the damaged area clearly.",
    label: "Damage",
  },
  {
    description: "Include the road, signs, lights, or surrounding scene.",
    label: "Context",
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

export function DemoAnalysisNotice() {
  return (
    <Alert title="Your evidence stays traceable" tone="info">
      ClaimDone keeps each detail connected to the photo or statement it came from.
      This demo runs locally and does not call an external provider.
    </Alert>
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
          <p className="section-heading__eyebrow">One quick question</p>
          <CardTitle id="clarification-title">We need one detail to complete your claim</CardTitle>
          <p className="card__description">
            ClaimDone paused instead of guessing. Your answer is checked before the
            draft continues.
          </p>
        </CardHeader>
        <CardContent className="stack stack--medium">
          <Alert title="Missing detail" tone="info">
            {clarification.question}
          </Alert>
          <div className="field">
            <div className="field__label-row">
              <label className="field__label" htmlFor="clarification-incident-time">
                Incident time
              </label>
            </div>
            <p className="field__description" id="clarification-time-help">
              Use 24-hour time including seconds, for example 14:30:00. Press Enter
              to continue.
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
              step={1}
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
            Securely bound to claim version {clarification.expectedVersion} · field {clarification.field}
          </p>
        </CardContent>
        <CardFooter>
          <Button
            disabled={busy || resetting || value.length === 0}
            leadingIcon={<ArrowRightIcon />}
            type="submit"
          >
            {busy ? "Checking your answer and completing the draft…" : "Save answer and continue"}
          </Button>
          <Button
            disabled={busy || resetting}
            onClick={onReset}
            type="button"
            variant="secondary"
          >
            {resetting ? "Clearing the claim…" : "Start over"}
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
  const [workflowStore, dispatchWorkflowStore] = useReducer(
    reduceWorkflowEventStore,
    INITIAL_WORKFLOW_EVENT_STORE,
  );
  const workflowTransport = useMemo(() => createWorkflowReadTransport(), []);
  const workflowStoreRef = useRef(workflowStore);
  const nextWorkflowSnapshotToken = useRef(0);
  const previewRegistry = useRef<PreviewUrlRegistry | null>(null);
  const activeAudioPreview = useRef<string | null>(null);
  const audioSourceFile = useRef<File | null>(null);
  const sourceFiles = useRef(new Map<string, File>());
  const nextId = useRef(0);
  const nextRequestToken = useRef(0);
  const requestPendingRef = useRef(false);
  const cleanupErrorRef = useRef<HTMLDivElement>(null);
  const serverErrorRef = useRef<HTMLDivElement>(null);
  const gateResult = useMemo(() => evaluateIntakeGates(state), [state]);

  useEffect(() => {
    workflowStoreRef.current = workflowStore;
  }, [workflowStore]);

  const recordAuthoritativeSnapshot = useCallback(
    (snapshot: IntakeFlowResponse) => {
      const current = workflowStoreRef.current;
      const requestToken = ++nextWorkflowSnapshotToken.current;
      dispatchWorkflowStore({
        caseId: snapshot.case.caseId,
        refreshGeneration: current.refreshGeneration,
        requestToken,
        type: "SNAPSHOT_REQUESTED",
      });
      dispatchWorkflowStore({
        refreshGeneration: current.refreshGeneration,
        requestToken,
        snapshot,
        type: "SNAPSHOT_RECEIVED",
      });
    },
    [],
  );

  const authoritativeCaseId = state.serverAuthority?.case.caseId ?? null;
  useEffect(() => {
    if (authoritativeCaseId === null) return;
    const current = workflowStoreRef.current;
    const afterCursor =
      current.activeCaseId === authoritativeCaseId ? current.lastCursor : null;
    try {
      const subscription = workflowTransport.subscribeEvents(
        authoritativeCaseId,
        afterCursor,
        {
          onEnvelope: (envelope) =>
            dispatchWorkflowStore({ envelope, type: "EVENT_RECEIVED" }),
          onFailure: (error) =>
            dispatchWorkflowStore({
              message: error.message,
              type: "STREAM_FAILED",
            }),
        },
      );
      return () => subscription.close();
    } catch (error) {
      dispatchWorkflowStore({
        message:
          error instanceof Error
            ? error.message
            : "The redacted workflow event stream is unavailable.",
        type: "STREAM_FAILED",
      });
      return;
    }
  }, [authoritativeCaseId, workflowTransport]);

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
    requestPendingRef.current = state.serverRequest !== null;
  }, [state.serverRequest]);

  useEffect(() => {
    if (cleanupError !== null) cleanupErrorRef.current?.focus();
  }, [cleanupError]);

  const allocateId = useCallback((prefix: string) => {
    nextId.current += 1;
    return `${prefix}-${nextId.current}`;
  }, []);

  const addImages = useCallback(
    (files: ReadonlyArray<File>) => {
      if (requestPendingRef.current || state.serverRequest !== null) return;
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
    [allocateId, registerPreview, state.serverRequest],
  );

  const handleImageInput = (event: ChangeEvent<HTMLInputElement>) => {
    addImages(Array.from(event.currentTarget.files ?? []));
    event.currentTarget.value = "";
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setIsDragging(false);
    if (requestPendingRef.current || state.serverRequest !== null) return;
    addImages(Array.from(event.dataTransfer.files));
  };

  const removeImage = (image: IntakeImage) => {
    if (requestPendingRef.current) return;
    sourceFiles.current.delete(image.id);
    releasePreview(image.previewUrl);
    dispatch({ id: image.id, type: "REMOVE_IMAGE" });
  };

  const changeStatementMode = (mode: StatementMode) => {
    if (requestPendingRef.current) return;
    if (mode === "text" && activeAudioPreview.current !== null) {
      releasePreview(activeAudioPreview.current);
      activeAudioPreview.current = null;
      audioSourceFile.current = null;
    }
    dispatch({ mode, type: "SET_STATEMENT_MODE" });
  };

  const handleAudioInput = (event: ChangeEvent<HTMLInputElement>) => {
    if (requestPendingRef.current) return;
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
    if (requestPendingRef.current) return;
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
    dispatch({ type: "RESET" });
  };

  const reset = () => {
    const caseId = state.serverAuthority?.case.caseId ?? state.pendingCaseId;
    if (caseId === undefined || caseId === null) {
      clearLocalState();
      return;
    }
    setIsResetting(true);
    setCleanupError(null);
    void deleteAuthoritativeCase(caseId)
      .then(() => {
        dispatch({ caseId, type: "SERVER_CASE_CLEANED" });
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
    if (requestPendingRef.current || !gateResult.canContinue) return;
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
      requestPendingRef.current = true;
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
    const cleanupBeforeRetry = state.pendingCaseId;
    requestPendingRef.current = true;
    dispatch({ kind: "intake", token, type: "BEGIN_SERVER_REQUEST" });
    void (async () => {
      if (cleanupBeforeRetry !== null) {
        await deleteAuthoritativeCase(cleanupBeforeRetry);
        dispatch({ caseId: cleanupBeforeRetry, type: "SERVER_CASE_CLEANED" });
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
          onCaseCleaned: (caseId) =>
            dispatch({ caseId, type: "SERVER_CASE_CLEANED" }),
          onCaseCreated: (caseId) =>
            dispatch({ caseId, token, type: "SERVER_CASE_CREATED" }),
        },
      );
    })()
      .then((response) => {
        recordAuthoritativeSnapshot(response);
        dispatch({ response, token, type: "SERVER_SUCCEEDED" });
      })
      .catch((error: unknown) => {
        if (error instanceof ClaimDonePendingCleanupError) {
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

  const beginRun = (
    authority: ReadyToFillResponse,
  ) => {
    const token = ++nextRequestToken.current;
    requestPendingRef.current = true;
    dispatch({ kind: "run", token, type: "BEGIN_SERVER_REQUEST" });
    void runClaimToReview(
      authority.case.caseId,
      authority.case.version,
    )
      .then((response) => {
        recordAuthoritativeSnapshot(response);
        dispatch({ response, token, type: "SERVER_SUCCEEDED" });
      })
      .catch((error: unknown) => dispatchServerError(token, error));
  };

  const retryRun = () => {
    if (requestPendingRef.current) return;
    const authority = state.serverAuthority;
    if (!isWorkflowSnapshotState(authority, "ready_to_fill")) return;
    beginRun(authority);
  };

  const submitClarification = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (requestPendingRef.current) return;
    const authority = state.serverAuthority;
    if (!isInt002ClarificationSnapshot(authority)) return;
    if (!/^(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d$/.test(clarificationAnswer)) {
      setClarificationError(
        "Enter a valid 24-hour time in exact HH:MM:SS format.",
      );
      return;
    }
    setClarificationError(null);
    const token = ++nextRequestToken.current;
    requestPendingRef.current = true;
    dispatch({ kind: "clarification", token, type: "BEGIN_SERVER_REQUEST" });
    let runToken: number | null = null;
    void answerThenRunToReview(
      {
        answer: clarificationAnswer,
        caseId: authority.case.caseId,
        clarificationId: authority.clarification.clarificationId,
        contractVersion: "4.0.0",
        expectedVersion: authority.clarification.expectedVersion,
        field: authority.clarification.field,
        round: authority.clarification.round,
      },
      {
        onReady: (response) => {
          recordAuthoritativeSnapshot(response);
          dispatch({ response, token, type: "SERVER_SUCCEEDED" });
          runToken = ++nextRequestToken.current;
          requestPendingRef.current = true;
          dispatch({ kind: "run", token: runToken, type: "BEGIN_SERVER_REQUEST" });
        },
      },
    )
      .then((response) => {
        recordAuthoritativeSnapshot(response);
        if (runToken !== null) {
          dispatch({ response, token: runToken, type: "SERVER_SUCCEEDED" });
        }
      })
      .catch((error: unknown) =>
        dispatchServerError(runToken ?? token, error),
      );
  };

  const imageSectionError =
    state.clientErrors.images ??
    (state.images.length > 0 ? gateResult.fieldErrors.images : undefined);
  const statementStarted =
    state.statementMode === "audio" || state.textStatement.length > 0;
  const statementError = statementStarted
    ? gateResult.fieldErrors.statement ??
      gateResult.fieldErrors[
        state.statementMode === "text" ? "statement.text" : "statement.audio"
      ]
    : undefined;
  const currentIndex =
    state.stage === "intake"
      ? 0
      : state.stage === "awaiting_clarification"
        ? 1
        : 2;
  const authoritativeG0 = state.serverAuthority?.claimPacket?.gateDecisions.find(
    (decision) => decision.gateId === "G0",
  );
  const authoritativeG1 = state.serverAuthority?.claimPacket?.gateDecisions.find(
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
  const clarificationAuthority: AwaitingClarificationResponse | null =
    isInt002ClarificationSnapshot(state.serverAuthority)
      ? state.serverAuthority
      : null;
  const readyAuthority: ReadyToFillResponse | null = isWorkflowSnapshotState(
    state.serverAuthority,
    "ready_to_fill",
  )
    ? state.serverAuthority
    : null;
  const reviewAuthority: ReviewResponse | null = isWorkflowSnapshotState(
    state.serverAuthority,
    "review",
  )
    ? state.serverAuthority
    : null;

  return (
    <PageShell
      aside={
        <div className="stack stack--medium">
          <HumanBoundaryCard />
          <Card tone="soft">
            <CardHeader>
              <CardTitle>Claim checks</CardTitle>
            </CardHeader>
            <CardContent className="stack stack--medium">
              <GateBadge
                gateId="G0"
                label="Evidence"
                reason={
                  authoritativeG0?.passed
                    ? `Confirmed by server request ${state.serverAuthority?.requestId ?? ""}`
                    : gateResult.g0.reasonCodes
                        .map((code) => gateReasonLabels[code] ?? code)
                        .join("; ") ||
                      (gateResult.g0.passed
                        ? "Initial check passed; awaiting final confirmation"
                        : undefined)
                }
                status={g0Status}
              />
              <GateBadge
                gateId="G1"
                label="Photo privacy"
                reason={
                  authoritativeG1?.passed
                    ? `Confirmed at case version ${state.serverAuthority?.case.version ?? ""}`
                    : gateResult.g1.reasonCodes
                        .map((code) => gateReasonLabels[code] ?? code)
                        .join("; ") ||
                      (gateResult.g1.passed
                        ? "Initial check passed; awaiting final confirmation"
                        : undefined)
                }
                status={g1Status}
              />
              <p className="aside-note">
                A check is shown as passed only after the server confirms the exact
                claim version. The agent cannot overrule a failed check.
              </p>
            </CardContent>
          </Card>
        </div>
      }
      description="Add three incident photos and tell us what happened. ClaimDone will organize the details, run the required checks, and prepare one clear claim for your review."
      eyebrow="Three photos + one short statement"
      title="Build your claim in a few clear steps"
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
                <strong>We could not clear the previous claim.</strong>
            <p>{cleanupError}</p>
            {state.pendingCaseId !== null ? (
              <p>Pending cleanup case: {state.pendingCaseId}</p>
            ) : null}
            <p>Your current view and evidence remain available so you can try again.</p>
          </div>
        ) : null}

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
                <strong>ClaimDone paused this request.</strong>
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
              label="Please review these additional details:"
            />
            <Card aria-labelledby="images-title">
              <CardHeader>
                <p className="section-heading__eyebrow">1 · Incident photos</p>
                <CardTitle id="images-title">Add three photos of the accident</CardTitle>
                <p className="card__description">
                  Include an overview, a close-up of the damage, and the surrounding
                  road context. JPG or PNG, up to 10 MB each.
                </p>
              </CardHeader>
              <CardContent className="stack stack--medium">
                <div
                  aria-disabled={state.serverRequest !== null}
                  className={`drop-zone${isDragging ? " drop-zone--active" : ""}${state.serverRequest !== null ? " drop-zone--disabled" : ""}`}
                  onDragEnter={(event) => {
                    event.preventDefault();
                    if (
                      !requestPendingRef.current &&
                      state.serverRequest === null
                    ) {
                      setIsDragging(true);
                    }
                  }}
                  onDragLeave={() => setIsDragging(false)}
                  onDragOver={(event) => event.preventDefault()}
                  onDrop={handleDrop}
                >
                  <p className="drop-zone__title">Drop your three photos here</p>
                  <p className="drop-zone__description">
                    {state.images.length} of {REQUIRED_IMAGE_COUNT} photos added
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
                    Choose photos
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
                  label="Please review these photo or privacy details:"
                />

                {state.images.length === 0 ? (
                  <div className="photo-slot-grid" aria-label="Required incident photos">
                    {photoRequirements.map(({ description, label }, index) => (
                      <article className="photo-slot" key={label}>
                        <span className="photo-slot__number">{index + 1}</span>
                        <strong>{label}</strong>
                        <p>{description}</p>
                      </article>
                    ))}
                  </div>
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
                              <span className="image-card__fallback">Photo could not be read</span>
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
                                <span className="image-card__role">
                                  {photoRequirements[index]?.label ?? `Photo ${index + 1}`}
                                </span>
                                <p className="image-card__name">{image.name}</p>
                                <p className="image-card__meta">
                                  Photo {index + 1} · {formatBytes(image.size)}
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
                              <legend>Photo {index + 1} privacy</legend>
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
                                Remove metadata before checking
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
                                Keep metadata for this demo
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
                                Kept metadata may include device or location details.
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
                <p className="section-heading__eyebrow">2 · What happened?</p>
                <CardTitle id="statement-title">Add a short text or voice memo</CardTitle>
              </CardHeader>
              <CardContent className="stack stack--medium">
                <DemoAnalysisNotice />
                <FieldErrorList
                  errors={statementBackendErrors}
                  label="Please review your statement:"
                />
                <fieldset className="mode-switch">
                  <legend>How would you like to tell us?</legend>
                  <label>
                    <input
                      checked={state.statementMode === "text"}
                      name="statement-mode"
                      onChange={() => changeStatementMode("text")}
                      type="radio"
                    />
                    Text
                  </label>
                  <label>
                    <input
                      checked={state.statementMode === "audio"}
                      name="statement-mode"
                      onChange={() => changeStatementMode("audio")}
                      type="radio"
                    />
                    Voice memo
                  </label>
                </fieldset>

                {state.statementMode === "text" ? (
                  <TextArea
                    description="Write naturally. Your words are kept exactly as entered and remain linked to the claim."
                    error={statementError}
                    id="claim-statement"
                    label="What happened?"
                    onChange={(event) =>
                      dispatch({
                        type: "SET_TEXT_STATEMENT",
                        value: event.currentTarget.value,
                      })
                    }
                    placeholder="For example: I was stopped at the light when the other car turned into the front-left side of my car. No one was injured."
                    rows={7}
                    value={state.textStatement}
                  />
                ) : (
                  <div className="audio-field">
                    <label className="field__label" htmlFor="claim-audio">
                      Voice memo
                    </label>
                    <p className="field__description">
                      Choose one WAV file up to {MAX_AUDIO_SECONDS} seconds. Its duration
                      is checked before processing.
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
                          Remove memo
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
              <CardFooter className="analysis-footer">
                <FieldErrorList
                  errors={consentBackendErrors}
                  label="ClaimDone could not confirm the demo permissions:"
                />
                <p className="analysis-note" id="analysis-notice">
                  By selecting Create my claim, you confirm that you may use these
                  staged photos and allow ClaimDone to process them for this local
                  demo. Nothing is submitted to an insurer.
                </p>
                <Button
                  aria-describedby="analysis-notice continue-requirements"
                  disabled={!gateResult.canContinue || state.serverRequest !== null}
                  leadingIcon={<ArrowRightIcon />}
                  type="submit"
                >
                  {state.serverRequest?.kind === "intake"
                    ? "Checking and organizing your claim…"
                    : state.serverError === null
                      ? "Create my claim"
                      : "Try claim checks again"}
                </Button>
                <span className="visually-hidden" id="continue-requirements">
                  Continue is available only after the fixed local demo acknowledgements
                  and deterministic G0 and G1 preflight checks pass. The canonical
                  server snapshot remains authoritative.
                </span>
              </CardFooter>
            </Card>
            </fieldset>
          </form>
        ) : null}

        {state.stage === "awaiting_clarification" &&
        clarificationAuthority !== null ? (
          <ClarificationCard
            busy={state.serverRequest?.kind === "clarification"}
            clarification={clarificationAuthority.clarification}
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

        {state.stage === "ready_to_fill" &&
        readyAuthority !== null ? (
          <div className="stack stack--medium">
            <StateView
              description="ClaimDone has everything it needs. It will now complete the draft and compare every prepared field with its source."
              title="Your details are ready for the final checks"
              variant="success"
            />
            {state.serverError !== null ? (
              <Alert title="The final checks did not complete" tone="warning">
                {state.serverError.message} Your answer is already saved and will not
                be sent again when you retry.
              </Alert>
            ) : null}
            <Card>
              <CardContent className="ready-summary server-review-summary">
                <div>
                  <span>Claim reference</span>
                  <strong>{readyAuthority.case.caseId}</strong>
                </div>
                <div>
                  <span>Secure claim version</span>
                  <strong>{readyAuthority.case.version}</strong>
                </div>
                <div>
                  <span>Next step</span>
                  <strong>Complete and verify the draft</strong>
                </div>
              </CardContent>
              <CardFooter>
                <Button
                  disabled={state.serverRequest?.kind === "run" || isResetting}
                  onClick={retryRun}
                >
                  {state.serverRequest?.kind === "run"
                    ? "Completing and verifying your claim…"
                    : state.serverError === null
                      ? "Complete my claim"
                      : "Try final checks again"}
                </Button>
                <Button
                  disabled={isResetting || state.serverRequest !== null}
                  onClick={reset}
                  variant="secondary"
                >
                  {isResetting ? "Clearing the claim…" : "Start over"}
                </Button>
              </CardFooter>
            </Card>
          </div>
        ) : null}

        {state.stage === "review" &&
        reviewAuthority !== null ? (
          <div className="stack stack--medium">
            <StateView
              description="Your evidence, required details, privacy choices, and prepared fields passed every required check. Review the claim before anything moves forward."
              title="Your complete claim is ready to review"
              variant="success"
            />
            <Alert title="Nothing has been submitted" tone="warning">
              ClaimDone stops at review. The agent cannot approve or submit this demo
              claim; the next action always belongs to you.
            </Alert>
            {workflowStore.failedClosed === null ? null : (
              <Alert title="Redacted activity stream unavailable" tone="warning">
                {workflowStore.failedClosed} The canonical review snapshot below remains
                the only product authority.
              </Alert>
            )}
            <WorkflowExperience
              events={workflowStore.events}
              mode="ready"
              showSandboxBanner={false}
              snapshot={reviewAuthority}
            />
            <details className="technical-details">
              <summary>Technical claim record</summary>
              <Card>
                <CardContent className="ready-summary server-review-summary">
                <div>
                  <span>Claim reference</span>
                  <strong>{reviewAuthority.case.caseId}</strong>
                </div>
                <div>
                  <span>Verified revision</span>
                  <strong>{reviewAuthority.case.version}</strong>
                </div>
                <div>
                  <span>Verification request</span>
                  <strong>{reviewAuthority.requestId}</strong>
                </div>
                <div>
                  <span>Completed checks</span>
                  <strong>
                    {reviewAuthority.claimPacket.gateDecisions
                      .map(({ gateId }) => gateId)
                      .join(" → ")}
                  </strong>
                </div>
                <div>
                  <span>Claim state</span>
                  <strong>{reviewAuthority.portalSession.state}</strong>
                </div>
                <div>
                  <span>Field comparison</span>
                  <strong>
                    {reviewAuthority.verificationAttempts.attempts.length} attempts ·
                    verified
                  </strong>
                </div>
                </CardContent>
              </Card>
            </details>
            <Card className="review-actions">
              <CardFooter>
                <a
                  className="button button--primary"
                  href={portalAReviewUrl(reviewAuthority.case.caseId)}
                  rel="noopener noreferrer"
                  target="_blank"
                >
                  Open secure claim review
                </a>
                <Button disabled={isResetting} onClick={reset} variant="secondary">
                  {isResetting ? "Clearing the claim…" : "Start over"}
                </Button>
              </CardFooter>
            </Card>
          </div>
        ) : null}
      </div>
    </PageShell>
  );
}
