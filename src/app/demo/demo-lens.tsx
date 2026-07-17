"use client";

import Image from "next/image";
import Link from "next/link";
import {
  type CSSProperties,
  useEffect,
  useMemo,
  useState,
} from "react";

import type { StatementMode } from "@/lib/analysis-schema";
import type {
  AgentActivity,
  AgentActivityEvent,
} from "@/lib/demo-analysis-schema";
import type {
  ComputerUseReplay,
  ComputerUseReplayStep,
} from "@/lib/portal-handoff-schema";

import styles from "./demo.module.css";

export type DemoFlowState =
  | "input"
  | "analyzing"
  | "needs_information"
  | "ready";

type DemoPhoto = {
  alt: string;
  src: string;
};

type DemoLensProps = {
  activity: AgentActivity | null;
  flowState: DemoFlowState;
  isPreparingPortal: boolean;
  photoCount: number;
  photos: readonly DemoPhoto[];
  portalError: boolean;
  replay: ComputerUseReplay | null;
  statementMode: StatementMode;
};

const fieldLabels = {
  attachedPhotos: "Attached photos",
  damage: "Damage",
  dateTime: "Date and time",
  location: "Location",
  whatHappened: "What happened",
} as const;

const demoStages = [
  { id: "evidence", label: "Evidence" },
  { id: "agent", label: "Agent review" },
  { id: "portal", label: "Portal handoff" },
] as const;

type DemoStage = (typeof demoStages)[number]["id"];

type ActivityItemStyle = CSSProperties & {
  "--activity-delay": string;
};

function sourceLabel(event: AgentActivityEvent): string {
  switch (event.source.kind) {
    case "photo":
      return `Photo ${event.source.photoIndex}`;
    case "statement":
      return event.source.mode === "voice" ? "Voice memo" : "Description";
    case "follow_up":
      return "Customer answer";
    case "system":
      return "Claim agent";
  }
}

function replayTitle(step: ComputerUseReplayStep): string {
  switch (step.kind) {
    case "opened":
      return "Opened Demo Mutual home";
    case "navigated":
      return step.destination === "claims"
        ? "Clicked “View claims”"
        : "Clicked “Start a motor claim”";
    case "field_filled":
      return `Filled ${fieldLabels[step.field]}`;
    case "verified":
      return "Verified all approved values";
  }
}

function replayAlt(step: ComputerUseReplayStep): string {
  switch (step.kind) {
    case "opened":
      return "Demo Mutual sandbox home page in the isolated browser";
    case "navigated":
      return step.destination === "claims"
        ? "Demo Mutual claims page after Computer Use selected View claims"
        : "Empty motor claim form after Computer Use selected Start a motor claim";
    case "field_filled":
      return `Demo Mutual insurer form after Computer Use filled ${fieldLabels[step.field]}`;
    case "verified":
      return "Completed Demo Mutual insurer form after all approved values were verified";
  }
}

function replayAddress(step: ComputerUseReplayStep): string {
  if (step.kind === "opened") {
    return "demo-mutual.local";
  }

  if (step.kind === "navigated" && step.destination === "claims") {
    return "demo-mutual.local/claims";
  }

  return "demo-mutual.local/claims/new";
}

function ActivityLedger({
  activity,
  flowState,
  photoCount,
  photos,
  statementMode,
}: Pick<
  DemoLensProps,
  "activity" | "flowState" | "photoCount" | "photos" | "statementMode"
>) {
  if (!activity) {
    if (flowState === "input") {
      return (
        <p className={styles.agentEmpty}>
          Activity will appear when you start the analysis.
        </p>
      );
    }

    return (
      <ol className={styles.pendingLedger}>
        <li className={styles.ledgerActive}>
          <span aria-hidden="true" className={styles.ledgerDot} />
          <div>
            <strong>Reviewing evidence…</strong>
            <p>
              The claim agent is checking {photoCount} {" "}
              {photoCount === 1 ? "photo" : "photos"} and the {" "}
              {statementMode === "voice" ? "voice memo" : "description"}.
            </p>
          </div>
        </li>
      </ol>
    );
  }

  return (
    <ol className={styles.activityLedger}>
      {activity.events.map((event) => {
        const photo =
          event.source.kind === "photo"
            ? photos[event.source.photoIndex - 1]
            : undefined;
        const itemStyle: ActivityItemStyle = {
          "--activity-delay": `${event.sequence * 90}ms`,
        };

        return (
          <li
            className={
              event.status === "attention"
                ? styles.activityAttention
                : styles.activityComplete
            }
            key={`${event.sequence}-${event.title}`}
            style={itemStyle}
          >
            <span aria-hidden="true" className={styles.ledgerDot}>
              {event.status === "attention" ? "!" : "✓"}
            </span>
            <div className={styles.activityCopy}>
              <div className={styles.activityTitleRow}>
                <strong>{event.title}</strong>
                <span>{sourceLabel(event)}</span>
              </div>
              <p>{event.detail}</p>
              {photo ? (
                <div className={styles.activityPhoto}>
                  <Image
                    alt={photo.alt}
                    fill
                    sizes="72px"
                    src={photo.src}
                    unoptimized
                  />
                </div>
              ) : null}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function ComputerUseReplay({ replay }: { replay: ComputerUseReplay }) {
  const [stepIndex, setStepIndex] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const lastIndex = replay.steps.length - 1;
  const step = replay.steps[stepIndex] ?? replay.steps[0];

  useEffect(() => {
    const reduceMotion =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const timeout = window.setTimeout(() => {
      setStepIndex(0);
      setIsPlaying(!reduceMotion);
    }, 0);

    return () => window.clearTimeout(timeout);
  }, [replay]);

  useEffect(() => {
    if (!isPlaying || stepIndex >= lastIndex) return;

    const timeout = window.setTimeout(() => {
      const nextStep = Math.min(stepIndex + 1, lastIndex);
      setStepIndex(nextStep);
      if (nextStep >= lastIndex) setIsPlaying(false);
    }, 900);

    return () => window.clearTimeout(timeout);
  }, [isPlaying, lastIndex, stepIndex]);

  const latestAnnouncement = useMemo(
    () => (step ? replayTitle(step) : "Computer Use replay ready"),
    [step],
  );

  if (!step) return null;

  return (
    <div className={styles.replay}>
      <div className={styles.replayMeta}>
        <span>Captured from this run</span>
        <span>
          {stepIndex + 1}/{replay.steps.length}
        </span>
      </div>

      <div className={styles.browserFrame}>
        <div className={styles.browserBar}>
          <span aria-hidden="true" className={styles.browserDots}>
            <i />
            <i />
            <i />
          </span>
          <span className={styles.browserAddress}>{replayAddress(step)}</span>
          <span className={styles.isolatedBadge}>Isolated</span>
        </div>
        <div className={styles.replayImage}>
          <Image
            alt={replayAlt(step)}
            fill
            priority
            sizes="(max-width: 900px) 100vw, 380px"
            src={step.screenshotDataUrl}
            unoptimized
          />
        </div>
      </div>

      <p aria-live="polite" className={styles.replayCurrent}>
        <span aria-hidden="true">{step.kind === "verified" ? "✓" : "→"}</span>
        {latestAnnouncement}
      </p>

      <div className={styles.replayControls}>
        <button
          aria-label="Previous Computer Use step"
          disabled={stepIndex === 0}
          onClick={() => {
            setIsPlaying(false);
            setStepIndex((current) => Math.max(0, current - 1));
          }}
          type="button"
        >
          ←
        </button>
        <button
          aria-label={isPlaying ? "Pause Computer Use replay" : "Play Computer Use replay"}
          onClick={() => {
            if (!isPlaying && stepIndex >= lastIndex) setStepIndex(0);
            setIsPlaying((current) => !current);
          }}
          type="button"
        >
          {isPlaying ? "Pause" : stepIndex >= lastIndex ? "Replay" : "Play"}
        </button>
        <button
          aria-label="Next Computer Use step"
          disabled={stepIndex >= lastIndex}
          onClick={() => {
            setIsPlaying(false);
            setStepIndex((current) => Math.min(lastIndex, current + 1));
          }}
          type="button"
        >
          →
        </button>
      </div>

      <details className={styles.replayHistory}>
        <summary>
          <span>Full browser action log</span>
          <strong>{replay.steps.length} captured steps</strong>
        </summary>
        <ol className={styles.replaySteps}>
          {replay.steps.map((replayStep, index) => (
            <li
              aria-current={index === stepIndex ? "step" : undefined}
              className={index <= stepIndex ? styles.replayStepComplete : undefined}
              key={`${replayStep.sequence}-${replayStep.kind}`}
            >
              <span aria-hidden="true">
                {index < stepIndex ? "✓" : index + 1}
              </span>
              {replayTitle(replayStep)}
            </li>
          ))}
        </ol>
      </details>

      <div className={styles.stopBoundary}>
        <span aria-hidden="true">■</span>
        <div>
          <strong>Stopped before submission</strong>
          <p>The sandbox form was filled and verified. Nothing was submitted.</p>
        </div>
      </div>

      <Link className={styles.portalLink} href="/portal/sandbox/claims/new">
        Open filled Demo Mutual portal
        <span aria-hidden="true">↗</span>
      </Link>
    </div>
  );
}

export function DemoLens({
  activity,
  flowState,
  isPreparingPortal,
  photoCount,
  photos,
  portalError,
  replay,
  statementMode,
}: DemoLensProps) {
  const latestEvent = activity?.events.at(-1);
  const latestActivity = latestEvent?.title;
  const portalStageActive = Boolean(isPreparingPortal || replay || portalError);
  const activeStage: DemoStage = portalStageActive
    ? "portal"
    : flowState === "input"
      ? "evidence"
      : "agent";
  const activeStageIndex = demoStages.findIndex(
    (stage) => stage.id === activeStage,
  );
  const outcome =
    flowState === "analyzing"
      ? activity
        ? "Reviewing the customer update…"
        : "Reviewing evidence…"
      : latestActivity
        ? latestActivity
        : flowState === "needs_information"
          ? "One more detail needed"
          : flowState === "ready"
            ? "Claim ready for review"
            : "Waiting for analysis";
  const outcomeTone =
    flowState === "analyzing"
      ? "active"
      : latestEvent?.status === "attention" || flowState === "needs_information"
        ? "attention"
        : latestEvent || flowState === "ready"
          ? "complete"
          : "idle";

  return (
    <aside aria-labelledby="agent-activity-title" className={styles.panel}>
      <div className={styles.panelHeader}>
        <div>
          <p className={styles.eyebrow}>Presenter view</p>
          <h2 id="agent-activity-title">Agent activity</h2>
        </div>
        <span className={styles.observerBadge}>
          <span aria-hidden="true" />
          Observable
        </span>
      </div>
      <p className={styles.panelIntro}>
        Observable checks and decisions — not private model reasoning.
      </p>

      <ol aria-label="Demo progress" className={styles.stageRail}>
        {demoStages.map((stage, index) => {
          const isActive = index === activeStageIndex;
          const isComplete = index < activeStageIndex;
          const stageStatus = isActive
            ? "Current step"
            : isComplete
              ? "Complete"
              : "Upcoming";

          return (
            <li
              aria-current={isActive ? "step" : undefined}
              className={`${styles.stageItem} ${
                isActive
                  ? styles.stageActive
                  : isComplete
                    ? styles.stageComplete
                    : styles.stageUpcoming
              }`}
              data-stage={stage.id}
              key={stage.id}
            >
              <span aria-hidden="true" className={styles.stageMark}>
                {isComplete ? "✓" : index + 1}
              </span>
              <span className={styles.stageLabel}>{stage.label}</span>
              <span className={styles.visuallyHidden}>{stageStatus}</span>
            </li>
          );
        })}
      </ol>

      <section aria-labelledby="claim-agent-title" className={styles.agentSection}>
        <div className={styles.sectionHeading}>
          <span aria-hidden="true" className={styles.agentGlyph}>✦</span>
          <div>
            <p>Claim agent</p>
            <h3 id="claim-agent-title">Evidence to decision</h3>
          </div>
        </div>
        <div
          aria-live="polite"
          className={`${styles.agentOutcome} ${
            outcomeTone === "attention"
              ? styles.agentOutcomeAttention
              : outcomeTone === "active"
                ? styles.agentOutcomeActive
                : outcomeTone === "complete"
                  ? styles.agentOutcomeComplete
                  : styles.agentOutcomeIdle
          }`}
        >
          <span aria-hidden="true" className={styles.outcomeMark} />
          <div>
            <span>Current outcome</span>
            <strong>{outcome}</strong>
          </div>
        </div>

        {portalStageActive && activity ? (
          <details className={styles.agentHistory}>
            <summary>
              <span>Agent review complete</span>
              <strong>{activity.events.length} observable checks</strong>
            </summary>
            <ActivityLedger
              activity={activity}
              flowState={flowState}
              photoCount={photoCount}
              photos={photos}
              statementMode={statementMode}
            />
          </details>
        ) : (
          <ActivityLedger
            activity={activity}
            flowState={flowState}
            photoCount={photoCount}
            photos={photos}
            statementMode={statementMode}
          />
        )}
      </section>

      <section aria-labelledby="computer-use-title" className={styles.computerSection}>
        <div className={styles.sectionHeading}>
          <span aria-hidden="true" className={styles.computerGlyph}>↗</span>
          <div>
            <p>Computer Use</p>
            <h3 id="computer-use-title">Claim to insurer portal</h3>
          </div>
        </div>

        {replay ? <ComputerUseReplay replay={replay} /> : null}

        {!replay && isPreparingPortal ? (
          <div aria-live="polite" className={styles.computerWaiting} role="status">
            <span aria-hidden="true" className={styles.computerPulse} />
            <div>
              <strong>Operating an isolated browser…</strong>
              <p>The captured run will appear here after verification.</p>
            </div>
          </div>
        ) : null}

        {!replay && !isPreparingPortal && portalError ? (
          <div className={styles.computerError} role="alert">
            <strong>Browser run stopped</strong>
            <p>The claim remains ready. Retry from the claim card.</p>
          </div>
        ) : null}

        {!replay && !isPreparingPortal && !portalError ? (
          <p className={styles.computerEmpty}>
            {flowState === "ready"
              ? "Ready when you choose Fill insurer portal sandbox."
              : "Starts after the claim is complete and reviewed."}
          </p>
        ) : null}
      </section>
    </aside>
  );
}
