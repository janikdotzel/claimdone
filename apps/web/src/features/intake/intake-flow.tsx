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
  MAX_AUDIO_SECONDS,
  MAX_IMAGE_BYTES,
  PreviewUrlRegistry,
  REQUIRED_IMAGE_COUNT,
  evaluateIntakeGates,
  imageFingerprint,
  initialIntakeState,
  inspectAudioDuration,
  inspectImageFile,
  intakeReducer,
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
    description: "Local G0 and G1 checks passed",
    id: "ready",
    label: "Ready",
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
  const previewRegistry = useRef<PreviewUrlRegistry | null>(null);
  const activeAudioPreview = useRef<string | null>(null);
  const sourceFiles = useRef(new Map<string, File>());
  const nextId = useRef(0);
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
      sourceFiles.current.clear();
    },
    [],
  );

  useEffect(() => {
    dispatch({ errors: backendErrors, type: "SET_BACKEND_ERRORS" });
  }, [backendErrors]);

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
    if (!file.type.startsWith("audio/")) {
      const id = allocateId("audio");
      dispatch({
        audio: {
          durationSeconds: null,
          error: "Choose a supported audio file.",
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
    dispatch({ type: "REMOVE_AUDIO" });
  };

  const reset = () => {
    previewRegistry.current?.releaseAll();
    activeAudioPreview.current = null;
    sourceFiles.current.clear();
    dispatch({ type: "RESET" });
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    dispatch({ type: "ADVANCE_TO_READY" });
  };

  const imageSectionError =
    state.clientErrors.images ?? gateResult.fieldErrors.images;
  const statementError =
    gateResult.fieldErrors.statement ??
    gateResult.fieldErrors[
      state.statementMode === "text" ? "statement.text" : "statement.audio"
    ];
  const currentIndex = state.stage === "disclosure" ? 0 : state.stage === "intake" ? 1 : 2;
  const g0Status = gateResult.g0.passed ? "passed" : "pending";
  const g1Status = gateResult.g1.passed ? "passed" : "pending";
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
              <CardTitle>Local intake gates</CardTitle>
            </CardHeader>
            <CardContent className="stack stack--medium">
              <GateBadge
                gateId="G0"
                label="Intake"
                reason={
                  gateResult.g0.reasonCodes
                    .map((code) => gateReasonLabels[code] ?? code)
                    .join("; ") || undefined
                }
                status={g0Status}
              />
              <GateBadge
                gateId="G1"
                label="Privacy"
                reason={
                  gateResult.g1.reasonCodes
                    .map((code) => gateReasonLabels[code] ?? code)
                    .join("; ") || undefined
                }
                status={g1Status}
              />
              <p className="aside-note">
                These UI checks preview the deterministic server gates. They cannot
                override a backend failure.
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
                      Choose one audio file. Its duration is checked locally and must not
                      exceed {MAX_AUDIO_SECONDS} seconds.
                    </p>
                    <input
                      accept="audio/*"
                      aria-describedby={
                        statementError === undefined ? "audio-help" : "audio-help audio-error"
                      }
                      aria-invalid={statementError === undefined ? undefined : true}
                      id="claim-audio"
                      onChange={handleAudioInput}
                      type="file"
                    />
                    <span className="visually-hidden" id="audio-help">
                      One audio memo, maximum 60 seconds.
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
                  <span>G0 {gateResult.g0.passed ? "passed" : "pending"}</span>
                  <span>G1 {gateResult.g1.passed ? "passed" : "pending"}</span>
                </div>
                <Button
                  aria-describedby="continue-requirements"
                  disabled={!gateResult.canContinue}
                  leadingIcon={<ArrowRightIcon />}
                  type="submit"
                >
                  Continue to analysis
                </Button>
                <span className="visually-hidden" id="continue-requirements">
                  Continue is available only after deterministic G0 and G1 preflight
                  checks pass.
                </span>
              </CardFooter>
            </Card>
          </form>
        ) : null}

        {state.stage === "ready" ? (
          <div className="stack stack--medium">
            <StateView
              description="Exactly three images, one statement mode, all consents, and a privacy choice per image passed the local preflight. Backend gates remain authoritative."
              title="Intake is ready for analysis"
              variant="success"
            />
            <Card>
              <CardContent className="ready-summary">
                <div>
                  <span>Images</span>
                  <strong>{state.images.length} verified locally</strong>
                </div>
                <div>
                  <span>Statement</span>
                  <strong>{state.statementMode === "text" ? "Written text" : "Audio memo"}</strong>
                </div>
                <div>
                  <span>Privacy</span>
                  <strong>
                    {state.images.filter((image) => image.decision === "strip").length} strip ·{" "}
                    {state.images.filter((image) => image.decision === "retain").length} retain
                  </strong>
                </div>
              </CardContent>
              <CardFooter>
                <Button onClick={reset} variant="secondary">
                  Start over
                </Button>
              </CardFooter>
            </Card>
          </div>
        ) : null}
      </div>
    </PageShell>
  );
}
