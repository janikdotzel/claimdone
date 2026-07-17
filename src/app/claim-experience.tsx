"use client";

import Image from "next/image";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  type ChangeEvent,
  type FormEvent,
  type SyntheticEvent,
  useEffect,
  useRef,
  useState,
} from "react";

import {
  requestDemoAnalysis,
  requestAnalysis,
  type ClientAnalysisInput,
} from "@/lib/analysis-client";
import {
  getMissingClaimDetailFields,
  isClaimDetailMissing,
  type AnalyzeResponse,
  type Claim,
  type ClaimDetailField,
  type MissingField,
  type StatementMode,
} from "@/lib/analysis-schema";
import type {
  AgentActivity,
  DemoAnalyzeResponse,
} from "@/lib/demo-analysis-schema";
import {
  requestDemoPortalHandoff,
  requestPortalHandoff,
} from "@/lib/portal-handoff-client";
import type {
  ComputerUseReplay,
  DemoPortalHandoffSuccess,
  PortalHandoffSuccess,
} from "@/lib/portal-handoff-schema";
import { DemoLens } from "./demo/demo-lens";
import demoStyles from "./demo/demo.module.css";
import styles from "./page.module.css";
import { usePortalHandoff } from "./portal/portal-handoff-context";

const MAX_PHOTO_BYTES = 8 * 1024 * 1024;
const MAX_VOICE_BYTES = 10 * 1024 * 1024;
const PHOTO_TYPES = new Set(["image/jpeg", "image/png"]);
const VOICE_TYPES = new Set([
  "audio/mp4",
  "audio/x-m4a",
  "audio/mpeg",
  "audio/wav",
  "audio/x-wav",
  "audio/webm",
]);
const GENERIC_FILE_TYPES = new Set(["", "application/octet-stream"]);
const VOICE_EXTENSIONS = /\.(m4a|mp3|wav|webm)$/i;
const PHOTO_LABELS = ["Overview", "Damage", "Context"] as const;
const CLAIM_DETAIL_LABELS: Record<ClaimDetailField, string> = {
  damage: "Damage",
  dateTime: "Date and time",
  location: "Location",
  whatHappened: "What happened",
};
const PHOTO_VALIDATION_ERRORS = new Set([
  "Add at least one accident photo.",
  "You can add up to three photos.",
  "Use JPG or PNG photos.",
  "Each photo must be 8 MB or smaller.",
]);
const VOICE_VALIDATION_ERRORS = new Set([
  "Add a voice memo.",
  "Use an M4A, MP3, WAV or WebM recording.",
  "Keep the voice memo under 60 seconds and 10 MB.",
]);

const initialStatement =
  "I was stopped at a red light at Alexanderplatz in Berlin when another car hit the front-left side of my car while turning.";

type EvidencePhoto = {
  alt: string;
  file?: File;
  id: string;
  src: string;
};

type FlowState = "input" | "analyzing" | "needs_information" | "ready";

export type ExperienceAnalysisInput = ClientAnalysisInput & {
  photoCount: 1 | 2 | 3;
};

const samplePhotos: EvidencePhoto[] = [
  {
    alt: "Two vehicles after a minor collision at a city intersection",
    id: "sample-overview",
    src: "/images/claim-flow/accident-overview.jpg",
  },
  {
    alt: "Close-up of a dented and scratched front bumper",
    id: "sample-damage",
    src: "/images/claim-flow/accident-damage.jpg",
  },
  {
    alt: "Intersection context with traffic lights, road markings, and both vehicles",
    id: "sample-context",
    src: "/images/claim-flow/accident-context.jpg",
  },
];

type Analyzer = (
  input: ExperienceAnalysisInput,
) => AnalyzeResponse | Promise<AnalyzeResponse>;

type DemoAnalyzer = (
  input: ExperienceAnalysisInput,
) => DemoAnalyzeResponse | Promise<DemoAnalyzeResponse>;

type PortalPreparer = (claim: Claim) => Promise<PortalHandoffSuccess>;
type DemoPortalPreparer = (
  claim: Claim,
) => Promise<DemoPortalHandoffSuccess>;

type ClaimExperienceProps = {
  analyze?: Analyzer;
  analyzeDemo?: DemoAnalyzer;
  analysisDelayMs?: number;
  prepareDemoPortal?: DemoPortalPreparer;
  preparePortal?: PortalPreparer;
  variant?: "standard" | "presenter";
};

function wait(milliseconds: number): Promise<void> {
  if (milliseconds <= 0) {
    return Promise.resolve();
  }

  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

function toPhotoCount(count: number): 1 | 2 | 3 {
  if (count === 1 || count === 2 || count === 3) {
    return count;
  }

  throw new Error("Invalid photo count");
}

function pluralizePhotos(count: number): string {
  return `${count} ${count === 1 ? "photo" : "photos"}`;
}

function formatClaimDetailLabels(fields: ClaimDetailField[]): string {
  const labels = fields.map((field) => CLAIM_DETAIL_LABELS[field]);

  if (labels.length <= 1) {
    return labels[0] ?? "The missing detail";
  }

  return `${labels.slice(0, -1).join(", ")} and ${labels.at(-1)}`;
}

function ClaimDisplayValue({ value }: { value: string }) {
  if (!isClaimDetailMissing(value)) {
    return value;
  }

  return (
    <span className={styles.missingClaimValue}>
      Not provided
      <span className={styles.requiredBadge}>Required</span>
    </span>
  );
}

export function ClaimExperience({
  analyze,
  analyzeDemo,
  analysisDelayMs = 850,
  prepareDemoPortal,
  preparePortal,
  variant = "standard",
}: ClaimExperienceProps) {
  const router = useRouter();
  const isPresenter = variant === "presenter";
  const { setPreparedHandoff } = usePortalHandoff();
  const [flowState, setFlowState] = useState<FlowState>("input");
  const [photos, setPhotos] = useState<EvidencePhoto[]>(() => [...samplePhotos]);
  const [statementMode, setStatementMode] = useState<StatementMode>("text");
  const [statementText, setStatementText] = useState(initialStatement);
  const [voiceFile, setVoiceFile] = useState<File | null>(null);
  const [voicePreviewUrl, setVoicePreviewUrl] = useState<string | null>(null);
  const [questionPrompt, setQuestionPrompt] = useState("Where did the accident happen?");
  const [questionField, setQuestionField] = useState<MissingField | null>(null);
  const [questionAnswer, setQuestionAnswer] = useState("");
  const [claim, setClaim] = useState<Claim | null>(null);
  const [draftClaim, setDraftClaim] = useState<Claim | null>(null);
  const [isEditingClaim, setIsEditingClaim] = useState(false);
  const [claimEditError, setClaimEditError] = useState(false);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [analysisError, setAnalysisError] = useState(false);
  const [isPreparingPortal, setIsPreparingPortal] = useState(false);
  const [portalError, setPortalError] = useState(false);
  const [agentActivity, setAgentActivity] = useState<AgentActivity | null>(null);
  const [computerReplay, setComputerReplay] =
    useState<ComputerUseReplay | null>(null);
  const objectUrlsRef = useRef(new Set<string>());
  const stateHeadingRef = useRef<HTMLHeadingElement>(null);
  const analysisErrorRef = useRef<HTMLDivElement>(null);
  const missingClaimFields = claim
    ? getMissingClaimDetailFields(claim)
    : [];
  const hasMissingClaimDetails = missingClaimFields.length > 0;
  const isUsingSampleEvidence =
    photos.length === samplePhotos.length &&
    photos.every((photo) => photo.id.startsWith("sample-"));

  useEffect(() => {
    if (flowState === "needs_information" || flowState === "ready") {
      window.requestAnimationFrame(() => stateHeadingRef.current?.focus());
    }
  }, [flowState, isEditingClaim]);

  useEffect(() => {
    if (!isPresenter || flowState === "input") {
      return;
    }

    const frame = window.requestAnimationFrame(() => {
      const heading = stateHeadingRef.current;

      if (!heading || typeof heading.scrollIntoView !== "function") {
        return;
      }

      const reduceMotion =
        typeof window.matchMedia === "function" &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      heading.scrollIntoView({
        behavior: reduceMotion ? "auto" : "smooth",
        block: "start",
      });
    });

    return () => window.cancelAnimationFrame(frame);
  }, [flowState, isPresenter]);

  useEffect(() => {
    if (analysisError) {
      window.requestAnimationFrame(() => analysisErrorRef.current?.focus());
    }
  }, [analysisError]);

  useEffect(
    () => () => {
      objectUrlsRef.current.forEach((url) => URL.revokeObjectURL(url));
    },
    [],
  );

  function replacePhotos(files: File[]) {
    photos.forEach((photo) => {
      if (photo.file) {
        URL.revokeObjectURL(photo.src);
        objectUrlsRef.current.delete(photo.src);
      }
    });

    const nextPhotos = files.map((file, index) => {
      const src = URL.createObjectURL(file);
      objectUrlsRef.current.add(src);

      return {
        alt: `Preview of ${file.name}`,
        file,
        id: `${file.name}-${file.lastModified}-${index}`,
        src,
      };
    });

    setPhotos(nextPhotos);
  }

  function handlePhotoChange(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.currentTarget.files ?? []);
    event.currentTarget.value = "";

    if (files.length === 0) {
      return;
    }

    if (files.length > 3) {
      setValidationError("You can add up to three photos.");
      return;
    }

    if (files.some((file) => !PHOTO_TYPES.has(file.type))) {
      setValidationError("Use JPG or PNG photos.");
      return;
    }

    if (files.some((file) => file.size > MAX_PHOTO_BYTES)) {
      setValidationError("Each photo must be 8 MB or smaller.");
      return;
    }

    setAnalysisError(false);
    setValidationError(null);
    replacePhotos(files);
  }

  function removePhoto(photoId: string) {
    setPhotos((currentPhotos) => {
      const removedPhoto = currentPhotos.find((photo) => photo.id === photoId);

      if (removedPhoto?.file) {
        URL.revokeObjectURL(removedPhoto.src);
        objectUrlsRef.current.delete(removedPhoto.src);
      }

      return currentPhotos.filter((photo) => photo.id !== photoId);
    });
    setValidationError(null);
  }

  function selectStatementMode(mode: StatementMode) {
    setStatementMode(mode);
    setValidationError(null);
    setAnalysisError(false);
  }

  function handleVoiceChange(event: ChangeEvent<HTMLInputElement>) {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = "";

    if (!file) {
      return;
    }

    const supportedVoiceType =
      VOICE_TYPES.has(file.type) ||
      (GENERIC_FILE_TYPES.has(file.type) && VOICE_EXTENSIONS.test(file.name));

    if (!supportedVoiceType) {
      setValidationError("Use an M4A, MP3, WAV or WebM recording.");
      return;
    }

    if (file.size > MAX_VOICE_BYTES) {
      setValidationError("Keep the voice memo under 60 seconds and 10 MB.");
      return;
    }

    if (voicePreviewUrl) {
      URL.revokeObjectURL(voicePreviewUrl);
      objectUrlsRef.current.delete(voicePreviewUrl);
    }

    const nextPreviewUrl = URL.createObjectURL(file);
    objectUrlsRef.current.add(nextPreviewUrl);
    setVoiceFile(file);
    setVoicePreviewUrl(nextPreviewUrl);
    setValidationError(null);
    setAnalysisError(false);
  }

  function validateEvidence(): boolean {
    if (photos.length === 0) {
      setValidationError("Add at least one accident photo.");
      return false;
    }

    if (photos.length > 3) {
      setValidationError("You can add up to three photos.");
      return false;
    }

    if (statementMode === "text" && statementText.trim().length === 0) {
      setValidationError("Add a short description.");
      return false;
    }

    if (statementMode === "voice" && !voiceFile) {
      setValidationError("Add a voice memo.");
      return false;
    }

    setValidationError(null);
    return true;
  }

  function buildAnalysisInput(answer?: string): ExperienceAnalysisInput {
    const input: ExperienceAnalysisInput = {
      photos: photos.map((photo) => ({
        ...(photo.file ? { file: photo.file } : {}),
        src: photo.src,
      })),
      photoCount: toPhotoCount(photos.length),
      statementMode,
      statementText,
      voiceFile,
    };

    if (answer && questionField) {
      input.questionField = questionField;
      input.questionAnswer = answer;
    }

    return input;
  }

  async function performAnalysis(answer?: string) {
    setAnalysisError(false);
    if (!answer) {
      setAgentActivity(null);
    }
    setComputerReplay(null);
    setFlowState("analyzing");

    try {
      await wait(analysisDelayMs);
      const input = buildAnalysisInput(answer);
      let result: AnalyzeResponse;

      if (isPresenter) {
        const demoResult = await (analyzeDemo ?? requestDemoAnalysis)(input);
        setAgentActivity(demoResult.activity);
        result = demoResult.result;
      } else {
        result = await (analyze ?? requestAnalysis)(input);
      }

      if (answer && result.status === "needs_information") {
        throw new Error("The analyzer returned a second question");
      }

      if (result.status === "needs_information") {
        setQuestionField(result.question.field);
        setQuestionPrompt(result.question.prompt);
        setQuestionAnswer("");
        setFlowState("needs_information");
        return;
      }

      setClaim(result.claim);
      setDraftClaim(null);
      setIsEditingClaim(false);
      setQuestionField(null);
      setFlowState("ready");
    } catch {
      setAnalysisError(true);
      setFlowState("input");
    }
  }

  function handleAnalyze() {
    if (validateEvidence()) {
      void performAnalysis();
    }
  }

  function handleQuestionSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const answer = questionAnswer.trim();

    if (!answer) {
      return;
    }

    void performAnalysis(answer);
  }

  function handleVoiceMetadata(event: SyntheticEvent<HTMLAudioElement>) {
    if (event.currentTarget.duration <= 60) {
      return;
    }

    if (voicePreviewUrl) {
      URL.revokeObjectURL(voicePreviewUrl);
      objectUrlsRef.current.delete(voicePreviewUrl);
    }

    setVoiceFile(null);
    setVoicePreviewUrl(null);
    setValidationError("Keep the voice memo under 60 seconds and 10 MB.");
  }

  function beginClaimEditing() {
    if (!claim) {
      return;
    }

    setPortalError(false);
    setClaimEditError(false);
    setDraftClaim({
      ...claim,
      damage: isClaimDetailMissing(claim.damage) ? "" : claim.damage,
      dateTime: isClaimDetailMissing(claim.dateTime) ? "" : claim.dateTime,
      location: isClaimDetailMissing(claim.location) ? "" : claim.location,
      whatHappened: isClaimDetailMissing(claim.whatHappened)
        ? ""
        : claim.whatHappened,
    });
    setIsEditingClaim(true);
  }

  function updateDraftClaim(field: ClaimDetailField, value: string) {
    setClaimEditError(false);
    setDraftClaim((currentDraft) =>
      currentDraft ? { ...currentDraft, [field]: value } : currentDraft,
    );
  }

  function cancelClaimEditing() {
    setClaimEditError(false);
    setDraftClaim(null);
    setIsEditingClaim(false);
  }

  async function handlePortalHandoff() {
    if (!claim || hasMissingClaimDetails || isPreparingPortal) {
      return;
    }

    setPortalError(false);
    setComputerReplay(null);
    setPreparedHandoff(null);
    setIsPreparingPortal(true);

    try {
      if (isPresenter) {
        const result = await (
          prepareDemoPortal ?? requestDemoPortalHandoff
        )(claim);
        setPreparedHandoff({ ...result, claim });
        setComputerReplay(result.replay);
        setIsPreparingPortal(false);
        return;
      }

      const result = await (preparePortal ?? requestPortalHandoff)(claim);
      setPreparedHandoff({ ...result, claim });
      router.push("/portal");
    } catch {
      setPortalError(true);
      setIsPreparingPortal(false);
    }
  }

  function handleClaimSave(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    if (!claim || !draftClaim) {
      return;
    }

    const previouslyMissingFields = getMissingClaimDetailFields(claim);

    const normalizedClaim = {
      damage: draftClaim.damage.trim(),
      dateTime: draftClaim.dateTime.trim(),
      location: draftClaim.location.trim(),
      whatHappened: draftClaim.whatHappened.trim(),
    };

    if (Object.values(normalizedClaim).some(isClaimDetailMissing)) {
      setClaimEditError(true);
      return;
    }

    setClaim({
      ...claim,
      ...normalizedClaim,
    });
    if (isPresenter && previouslyMissingFields.length > 0) {
      setAgentActivity((currentActivity) => {
        if (!currentActivity) {
          return currentActivity;
        }

        const firstSequence = currentActivity.events.length;
        const fieldLabels = formatClaimDetailLabels(previouslyMissingFields);
        const oneField = previouslyMissingFields.length === 1;

        return {
          events: [
            ...currentActivity.events,
            {
              detail: `${fieldLabels} ${oneField ? "was" : "were"} supplied and ${oneField ? "the previously missing claim detail is" : "the previously missing claim details are"} now confirmed.`,
              phase: "completeness",
              sequence: firstSequence,
              source: { kind: "system" },
              status: "complete",
              title: "Customer update checked",
            },
            {
              detail:
                "All four required claim details are complete. The claim can continue to the insurer portal sandbox.",
              phase: "decision",
              sequence: firstSequence + 1,
              source: { kind: "system" },
              status: "complete",
              title: "Decision: Claim ready",
            },
          ],
        };
      });
    }
    setComputerReplay(null);
    setPreparedHandoff(null);
    setClaimEditError(false);
    setDraftClaim(null);
    setIsEditingClaim(false);
  }

  const hasPhotoValidationError =
    validationError !== null && PHOTO_VALIDATION_ERRORS.has(validationError);
  const hasTextValidationError = validationError === "Add a short description.";
  const hasVoiceValidationError =
    validationError !== null && VOICE_VALIDATION_ERRORS.has(validationError);

  return (
    <div
      className={`${styles.page} ${
        isPresenter ? demoStyles.presenterPage : ""
      }`}
    >
      <header className={styles.header}>
        <div
          className={`${styles.headerInner} ${
            isPresenter ? demoStyles.presenterHeader : ""
          }`}
        >
          <Link
            aria-label={isPresenter ? "ClaimDone presenter home" : "ClaimDone home"}
            className={styles.brand}
            href={isPresenter ? "/demo" : "/"}
          >
            <span aria-hidden="true" className={styles.brandMark}>
              ✓
            </span>
            ClaimDone
          </Link>
          {isPresenter ? (
            <div className={demoStyles.presenterMeta}>
              <span className={demoStyles.presenterBadge}>Presenter view</span>
              <Link className={demoStyles.standardLink} href="/">
                Standard view
              </Link>
            </div>
          ) : null}
        </div>
      </header>

      <main
        className={`${styles.main} ${
          isPresenter ? demoStyles.presenterMain : ""
        }`}
        id="main-content"
      >
        <div
          className={
            isPresenter ? demoStyles.workspace : demoStyles.standardWorkspace
          }
        >
          <section
            aria-labelledby="page-title"
            className={`${styles.hero} ${
              isPresenter && flowState !== "input"
                ? demoStyles.presenterFlowActive
                : ""
            }`}
          >
            <div className={styles.intro}>
              <h1 id="page-title">Turn accident photos into a claim.</h1>
              <p className={styles.lead}>
                Add 1–3 photos and tell us what happened. ClaimDone will prepare a
                clear claim for you to review.
              </p>
            </div>

            <div className={styles.flowPreview}>
              {flowState === "input" ? (
                <section
                  aria-labelledby="evidence-title"
                  className={styles.evidenceCard}
                >
                <div className={styles.cardHeader}>
                  <h2 id="evidence-title">Add evidence</h2>
                  <div className={styles.fileAction}>
                    <input
                      accept="image/jpeg,image/png"
                      aria-describedby={`photo-requirements${
                        hasPhotoValidationError ? " evidence-validation-error" : ""
                      }`}
                      aria-invalid={hasPhotoValidationError || undefined}
                      aria-label={
                        isUsingSampleEvidence
                          ? "Use your own accident photos"
                          : photos.length > 0
                            ? "Replace accident photos"
                            : "Add accident photos"
                      }
                      className={styles.fileInput}
                      id="accident-photos"
                      multiple
                      onChange={handlePhotoChange}
                      type="file"
                    />
                    <label className={styles.addPhotos} htmlFor="accident-photos">
                      <span aria-hidden="true">+</span>
                      {isUsingSampleEvidence
                        ? "Use your own photos"
                        : photos.length > 0
                          ? "Replace photos"
                          : "Add photos"}
                    </label>
                  </div>
                </div>

                <div className={styles.fieldHeading}>
                  <div className={styles.fieldTitleRow}>
                    <h3>Accident photos</h3>
                    {isUsingSampleEvidence ? (
                      <span className={styles.sampleEvidenceBadge}>
                        Sample evidence
                      </span>
                    ) : null}
                  </div>
                  <p id="photo-requirements">1–3 photos · JPG or PNG · 8 MB each</p>
                </div>

                {photos.length > 0 ? (
                  <div className={styles.photoGrid} data-count={photos.length}>
                    {photos.map((photo, index) => {
                      const label = PHOTO_LABELS[index] ?? `Photo ${index + 1}`;

                      return (
                        <figure className={styles.photoCard} key={photo.id}>
                          <div className={styles.photoFrame}>
                            <Image
                              alt={photo.alt}
                              fill
                              priority={!photo.file && index === 0}
                              sizes="(max-width: 700px) 28vw, (max-width: 1100px) 24vw, 180px"
                              src={photo.src}
                              unoptimized={Boolean(photo.file)}
                            />
                            <button
                              aria-label={`Remove ${label} photo`}
                              className={styles.removePhoto}
                              onClick={() => removePhoto(photo.id)}
                              type="button"
                            >
                              ×
                            </button>
                          </div>
                          <figcaption>
                            <span className={styles.photoNumber}>{index + 1}</span>
                            <span>{label}</span>
                            <span aria-hidden="true" className={styles.photoCheck}>
                              ✓
                            </span>
                          </figcaption>
                        </figure>
                      );
                    })}
                  </div>
                ) : (
                  <div className={styles.photoEmpty}>No photos added yet.</div>
                )}

                <div className={styles.statementSection}>
                  <div className={styles.statementHeader}>
                    <h3>Tell us what happened</h3>
                    <div
                      aria-label="Description format"
                      className={styles.modeSwitch}
                      role="group"
                    >
                      <button
                        aria-pressed={statementMode === "text"}
                        className={statementMode === "text" ? styles.modeActive : undefined}
                        onClick={() => selectStatementMode("text")}
                        type="button"
                      >
                        Text
                      </button>
                      <button
                        aria-pressed={statementMode === "voice"}
                        className={statementMode === "voice" ? styles.modeActive : undefined}
                        onClick={() => selectStatementMode("voice")}
                        type="button"
                      >
                        Voice memo
                      </button>
                    </div>
                  </div>

                  {statementMode === "text" ? (
                    <>
                      <label className={styles.textLabel} htmlFor="accident-statement">
                        Short description
                      </label>
                      <textarea
                        aria-describedby={`statement-hint${
                          hasTextValidationError ? " evidence-validation-error" : ""
                        }`}
                        aria-invalid={hasTextValidationError || undefined}
                        className={styles.statementInput}
                        id="accident-statement"
                        maxLength={1500}
                        onChange={(event) => {
                          setStatementText(event.currentTarget.value);
                          setValidationError(null);
                          setAnalysisError(false);
                        }}
                        placeholder="Example: I was stopped at a red light when another car hit the front-left side of my car."
                        rows={4}
                        value={statementText}
                      />
                      <p className={styles.inputHint} id="statement-hint">
                        A few sentences is enough.
                      </p>
                    </>
                  ) : (
                    <div className={styles.voiceField}>
                      <input
                        accept=".m4a,.mp3,.wav,.webm,audio/mp4,audio/mpeg,audio/wav,audio/webm"
                        aria-describedby={`voice-hint${
                          hasVoiceValidationError ? " evidence-validation-error" : ""
                        }`}
                        aria-invalid={hasVoiceValidationError || undefined}
                        aria-label="Add voice memo"
                        className={styles.fileInput}
                        id="voice-memo"
                        onChange={handleVoiceChange}
                        type="file"
                      />
                      <label className={styles.voiceAction} htmlFor="voice-memo">
                        <span aria-hidden="true">＋</span>
                        {voiceFile ? "Choose another memo" : "Add voice memo"}
                      </label>
                      {voiceFile ? (
                        <p className={styles.voiceFileName}>
                          <span aria-hidden="true">✓</span>
                          {voiceFile.name}
                        </p>
                      ) : null}
                      {voicePreviewUrl ? (
                        <audio
                          onLoadedMetadata={handleVoiceMetadata}
                          preload="metadata"
                          src={voicePreviewUrl}
                        />
                      ) : null}
                      <p className={styles.inputHint} id="voice-hint">
                        Record on your phone or choose an audio file. Up to 60 seconds and
                        10 MB.
                      </p>
                    </div>
                  )}
                </div>

                {validationError ? (
                  <p
                    className={styles.validationError}
                    id="evidence-validation-error"
                    role="alert"
                  >
                    {validationError}
                  </p>
                ) : null}

                {analysisError ? (
                  <div
                    className={styles.analysisError}
                    ref={analysisErrorRef}
                    role="alert"
                    tabIndex={-1}
                  >
                    <strong>We couldn’t analyze these photos</strong>
                    <p>
                      Try a clearer damage photo or add a short description of what
                      happened.
                    </p>
                  </div>
                ) : null}

                <button className={styles.primaryAction} onClick={handleAnalyze} type="button">
                  {analysisError ? "Try again" : "Analyze accident"}
                  <span aria-hidden="true">→</span>
                </button>
              </section>
            ) : null}

            {flowState === "analyzing" ? (
              <section
                aria-live="polite"
                className={`${styles.stateCard} ${styles.analyzingCard}`}
                role="status"
              >
                <span aria-hidden="true" className={styles.spinner} />
                <h2 ref={stateHeadingRef}>
                  Analyzing your photos and preparing your claim…
                </h2>
              </section>
            ) : null}

            {flowState === "needs_information" ? (
              <section aria-labelledby="question-title" className={styles.stateCard}>
                <p className={styles.claimType}>One detail</p>
                <h2 id="question-title" ref={stateHeadingRef} tabIndex={-1}>
                  We need one more detail
                </h2>
                <p className={styles.stateLead}>
                  Answer this question so we can finish your claim.
                </p>
                <form className={styles.questionForm} onSubmit={handleQuestionSubmit}>
                  <p className={styles.questionPrompt}>{questionPrompt}</p>
                  <label className={styles.textLabel} htmlFor="question-answer">
                    Your answer
                  </label>
                  <input
                    autoComplete="off"
                    className={styles.answerInput}
                    id="question-answer"
                    onChange={(event) => setQuestionAnswer(event.currentTarget.value)}
                    placeholder="Add the missing detail"
                    required
                    value={questionAnswer}
                  />
                  <button className={styles.primaryAction} type="submit">
                    Continue analysis
                    <span aria-hidden="true">→</span>
                  </button>
                </form>
              </section>
            ) : null}

            {flowState === "ready" && claim ? (
              <article
                aria-labelledby="claim-title"
                aria-busy={isPreparingPortal}
                className={`${styles.claimCard} ${
                  hasMissingClaimDetails ? styles.claimCardNeedsDetails : ""
                }`}
                id="claim-preview"
              >
                <div className={styles.claimHeader}>
                  <div>
                    <p className={styles.claimType}>Insurance claim</p>
                    <h2 id="claim-title" ref={stateHeadingRef} tabIndex={-1}>
                      {isEditingClaim
                        ? "Edit claim details"
                        : hasMissingClaimDetails
                          ? "Your claim needs details"
                          : "Your claim is ready"}
                    </h2>
                    {!isEditingClaim ? (
                      <p>
                        {hasMissingClaimDetails
                          ? "Complete the fields marked Required before continuing."
                          : "Review the details before continuing."}
                      </p>
                    ) : null}
                  </div>
                  <span
                    aria-label={`System status: ${
                      hasMissingClaimDetails ? "Needs details" : "Ready"
                    }`}
                    className={`${styles.statusBadge} ${
                      hasMissingClaimDetails ? styles.needsDetailsBadge : ""
                    }`}
                  >
                    <span aria-hidden="true">
                      {hasMissingClaimDetails ? "!" : "✓"}
                    </span>
                    {hasMissingClaimDetails ? "Needs details" : "Ready"}
                  </span>
                </div>

                <form className={styles.claimForm} onSubmit={handleClaimSave}>
                  <dl className={styles.claimFields}>
                    <div>
                      <dt>Damage</dt>
                      <dd>
                        {isEditingClaim && draftClaim ? (
                          <input
                            aria-describedby={claimEditError ? "claim-edit-error" : undefined}
                            aria-invalid={isClaimDetailMissing(draftClaim.damage)}
                            aria-label="Damage"
                            className={`${styles.claimEditInput} ${
                              isClaimDetailMissing(draftClaim.damage)
                                ? styles.claimEditInputRequired
                                : ""
                            }`}
                            maxLength={500}
                            onChange={(event) =>
                              updateDraftClaim("damage", event.currentTarget.value)
                            }
                            placeholder="Required"
                            required
                            value={draftClaim.damage}
                          />
                        ) : (
                          <ClaimDisplayValue value={claim.damage} />
                        )}
                      </dd>
                    </div>
                    <div>
                      <dt>Date and time</dt>
                      <dd>
                        {isEditingClaim && draftClaim ? (
                          <input
                            aria-describedby={claimEditError ? "claim-edit-error" : undefined}
                            aria-invalid={isClaimDetailMissing(draftClaim.dateTime)}
                            aria-label="Date and time"
                            className={`${styles.claimEditInput} ${
                              isClaimDetailMissing(draftClaim.dateTime)
                                ? styles.claimEditInputRequired
                                : ""
                            }`}
                            maxLength={200}
                            onChange={(event) =>
                              updateDraftClaim("dateTime", event.currentTarget.value)
                            }
                            placeholder="Required"
                            required
                            value={draftClaim.dateTime}
                          />
                        ) : (
                          <ClaimDisplayValue value={claim.dateTime} />
                        )}
                      </dd>
                    </div>
                    <div>
                      <dt>Location</dt>
                      <dd>
                        {isEditingClaim && draftClaim ? (
                          <input
                            aria-describedby={claimEditError ? "claim-edit-error" : undefined}
                            aria-invalid={isClaimDetailMissing(draftClaim.location)}
                            aria-label="Location"
                            className={`${styles.claimEditInput} ${
                              isClaimDetailMissing(draftClaim.location)
                                ? styles.claimEditInputRequired
                                : ""
                            }`}
                            maxLength={500}
                            onChange={(event) =>
                              updateDraftClaim("location", event.currentTarget.value)
                            }
                            placeholder="Required"
                            required
                            value={draftClaim.location}
                          />
                        ) : (
                          <ClaimDisplayValue value={claim.location} />
                        )}
                      </dd>
                    </div>
                    <div className={styles.wideField}>
                      <dt>What happened</dt>
                      <dd>
                        {isEditingClaim && draftClaim ? (
                          <textarea
                            aria-describedby={claimEditError ? "claim-edit-error" : undefined}
                            aria-invalid={isClaimDetailMissing(
                              draftClaim.whatHappened,
                            )}
                            aria-label="What happened"
                            className={`${styles.claimEditInput} ${
                              styles.claimEditTextarea
                            } ${
                              isClaimDetailMissing(draftClaim.whatHappened)
                                ? styles.claimEditInputRequired
                                : ""
                            }`}
                            maxLength={1500}
                            onChange={(event) =>
                              updateDraftClaim("whatHappened", event.currentTarget.value)
                            }
                            placeholder="Required"
                            required
                            rows={4}
                            value={draftClaim.whatHappened}
                          />
                        ) : (
                          <ClaimDisplayValue value={claim.whatHappened} />
                        )}
                      </dd>
                    </div>
                  </dl>

                  <div className={styles.attachmentHeader}>
                    <span>Attached photos</span>
                    <strong>{pluralizePhotos(claim.photoCount)}</strong>
                  </div>
                  <div
                    className={styles.attachmentStrip}
                    data-count={photos.length}
                    role="list"
                  >
                    {photos.map((photo, index) => (
                      <div className={styles.attachment} key={photo.id} role="listitem">
                        <Image
                          alt=""
                          fill
                          sizes="80px"
                          src={photo.src}
                          unoptimized={Boolean(photo.file)}
                        />
                        <span>{PHOTO_LABELS[index] ?? `Photo ${index + 1}`}</span>
                      </div>
                    ))}
                  </div>

                  {claimEditError ? (
                    <p
                      className={styles.claimEditError}
                      id="claim-edit-error"
                      role="alert"
                    >
                      Complete all four claim details before saving.
                    </p>
                  ) : null}

                  <div className={styles.claimActions}>
                    {isEditingClaim ? (
                      <>
                        <button
                          className={styles.secondaryAction}
                          onClick={cancelClaimEditing}
                          type="button"
                        >
                          Cancel
                        </button>
                        <button className={styles.primaryAction} type="submit">
                          Save changes
                        </button>
                      </>
                    ) : hasMissingClaimDetails ? (
                      <button
                        className={styles.primaryAction}
                        onClick={beginClaimEditing}
                        type="button"
                      >
                        Add missing details
                        <span aria-hidden="true">→</span>
                      </button>
                    ) : (
                      <>
                        <button
                          className={styles.secondaryAction}
                          disabled={isPreparingPortal}
                          onClick={beginClaimEditing}
                          type="button"
                        >
                          Edit details
                        </button>
                        <button
                          className={styles.primaryAction}
                          disabled={isPreparingPortal}
                          onClick={() => void handlePortalHandoff()}
                          type="button"
                        >
                          {isPreparingPortal
                            ? isPresenter
                              ? "Computer Use is running…"
                              : "Preparing insurer portal…"
                            : portalError
                              ? "Try portal again"
                              : isPresenter
                                ? "Run Computer Use in insurer sandbox"
                                : "Fill insurer portal sandbox"}
                          <span aria-hidden="true">→</span>
                        </button>
                      </>
                    )}
                  </div>

                  {isPreparingPortal ? (
                    <p aria-live="polite" className={styles.portalProgress} role="status">
                      <span aria-hidden="true" className={styles.portalProgressMark} />
                      {isPresenter
                        ? "Computer Use is opening Demo Mutual and filling the form…"
                        : "Opening the insurer portal and preparing the form…"}
                    </p>
                  ) : null}

                  {portalError ? (
                    <div className={styles.portalError} role="alert">
                      <strong>We couldn’t prepare the insurer portal</strong>
                      <p>Your claim is still here. Try the sandbox again.</p>
                    </div>
                  ) : null}

                  <p className={styles.submissionNotice}>
                    <span aria-hidden="true">◇</span>
                    Nothing has been submitted.
                  </p>
                </form>
              </article>
            ) : null}
          </div>
          </section>
          {isPresenter ? (
            <DemoLens
              activity={agentActivity}
              flowState={flowState}
              isPreparingPortal={isPreparingPortal}
              photoCount={photos.length}
              photos={photos}
              portalError={portalError}
              replay={computerReplay}
              statementMode={statementMode}
            />
          ) : null}
        </div>
      </main>
    </div>
  );
}
