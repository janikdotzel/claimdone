"use client";

import Image from "next/image";
import { useEffect, useState } from "react";

import {
  ArrowRightIcon,
  CheckIcon,
  ShieldIcon,
} from "../components/ui/icons";
import {
  BadgeIcon,
  CalendarIcon,
  CameraIcon,
  CarIcon,
  FileCheckIcon,
  LinkIcon,
  MapPinIcon,
  MessageIcon,
  MicIcon,
  RouteIcon,
  SendIcon,
} from "./landing-icons";
import styles from "./landing.module.css";

const photos = [
  {
    alt: "Two vehicles after a minor collision at a city intersection",
    label: "Overview",
    src: "/images/claim-flow/accident-overview.jpg",
  },
  {
    alt: "Close-up of a dented and scratched front bumper",
    label: "Damage",
    src: "/images/claim-flow/accident-damage.jpg",
  },
  {
    alt: "Intersection context with traffic lights, road markings, and both vehicles",
    label: "Context",
    src: "/images/claim-flow/accident-context.jpg",
  },
] as const;

const claimFields = [
  { Icon: CalendarIcon, label: "Time", value: "July 16, 2026 · 08:42" },
  { Icon: MapPinIcon, label: "Location", value: "Intersection · identified from photo" },
  { Icon: CarIcon, label: "Damage", value: "Front left · bumper" },
  { Icon: BadgeIcon, label: "Registration", value: "B · CD 2048" },
  { Icon: ShieldIcon, label: "Injuries", value: "None" },
  {
    Icon: RouteIcon,
    label: "What happened",
    value: "The other vehicle struck the front-left side while turning.",
  },
] as const;

const defaultStatement =
  "I was stopped at the light. The other vehicle struck the front-left side of my car while turning. No one was injured.";

export function LandingClaimDemo() {
  const [mode, setMode] = useState<"memo" | "text">("memo");
  const [statement, setStatement] = useState(defaultStatement);
  const [isWorking, setIsWorking] = useState(false);
  const [progress, setProgress] = useState(100);
  const [runId, setRunId] = useState(0);

  useEffect(() => {
    if (runId === 0) {
      return undefined;
    }

    const timers = [
      window.setTimeout(() => {
        setProgress(38);
      }, 420),
      window.setTimeout(() => {
        setProgress(64);
      }, 820),
      window.setTimeout(() => {
        setProgress(84);
      }, 1220),
      window.setTimeout(() => {
        setProgress(100);
        setIsWorking(false);
      }, 1680),
    ];

    return () => timers.forEach((timer) => window.clearTimeout(timer));
  }, [runId]);

  function generateClaim() {
    setProgress(12);
    setIsWorking(true);
    setRunId((value) => value + 1);
  }

  return (
    <section aria-label="From incident photos to a complete insurance claim" className={styles.demo}>
      <div className={styles.inputRow}>
        <div>
          <div className={styles.photoGrid}>
            {photos.map((photo, index) => (
              <article className={styles.photoCard} key={photo.label}>
                <div className={styles.photoFrame}>
                  <Image
                    alt={photo.alt}
                    fill
                    priority={index === 0}
                    sizes="(max-width: 560px) 100vw, (max-width: 880px) 30vw, 220px"
                    src={photo.src}
                  />
                </div>
                <div className={styles.photoCaption}>
                  <span className={styles.photoNumber}>{index + 1}</span>
                  <span>{photo.label}</span>
                  <CheckIcon />
                </div>
              </article>
            ))}
          </div>
          <p className={styles.inputMeta}>
            <CameraIcon />
            3 example photos added
          </p>
        </div>

        <span aria-hidden="true" className={styles.plus}>
          +
        </span>

        <div className={styles.noteCard}>
          <div aria-label="Statement format" className={styles.modeSwitch} role="group">
            <button
              aria-pressed={mode === "memo"}
              className={styles.modeButton}
              onClick={() => setMode("memo")}
              type="button"
            >
              <MicIcon />
              Memo
            </button>
            <button
              aria-pressed={mode === "text"}
              className={styles.modeButton}
              onClick={() => setMode("text")}
              type="button"
            >
              <MessageIcon />
              Text
            </button>
          </div>

          {mode === "memo" ? (
            <div aria-hidden="true" className={styles.waveform}>
              {Array.from({ length: 18 }, (_, index) => (
                <span key={index} />
              ))}
            </div>
          ) : (
            <div aria-hidden="true" className={styles.textPreview}>
              <MessageIcon />
              Short statement
            </div>
          )}

          <label className={styles.noteLabel} htmlFor="landing-claim-statement">
            What happened?
          </label>
          <textarea
            aria-describedby="landing-claim-statement-meta"
            className={styles.noteInput}
            id="landing-claim-statement"
            onChange={(event) => setStatement(event.target.value)}
            rows={5}
            value={statement}
          />
          <div className={styles.noteMeta} id="landing-claim-statement-meta">
            <span>{mode === "memo" ? "Voice memo · 18 sec" : "Text · short statement"}</span>
            <span>
              <CheckIcon />
              Added
            </span>
          </div>
        </div>
      </div>

      <div aria-busy={isWorking} className={styles.demoAction}>
        <button
          className={styles.generateButton}
          disabled={isWorking}
          onClick={generateClaim}
          type="button"
        >
          {isWorking ? "Creating your claim…" : "Create insurance claim"}
          {!isWorking ? <ArrowRightIcon /> : null}
        </button>
      </div>

      <article className={styles.claimCard} data-working={isWorking}>
        <div className={styles.claimHeader}>
          <div className={styles.claimIdentity}>
            <span className={styles.claimDocumentIcon}>
              <FileCheckIcon />
            </span>
            <span>
              <small>INSURANCE CLAIM</small>
              <strong>Claim #CD-2048</strong>
            </span>
          </div>
          <span className={styles.completeBadge}>
            <CheckIcon />
            {isWorking ? "In progress" : "Complete"}
          </span>
        </div>

        <div className={styles.completeness}>
          <div>
            <span>Completeness</span>
            <strong>{progress}%</strong>
          </div>
          <div
            aria-label={`Completeness ${progress} percent`}
            aria-valuemax={100}
            aria-valuemin={0}
            aria-valuenow={progress}
            className={styles.progressTrack}
            role="progressbar"
          >
            <span style={{ width: `${progress}%` }} />
          </div>
        </div>

        <dl className={styles.claimFields}>
          {claimFields.map(({ Icon, label, value }) => (
            <div key={label}>
              <dt>
                <Icon />
                {label}
              </dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>

        <div className={styles.claimFooter}>
          <span>
            <LinkIcon />
            Created from 3 photos + {mode === "memo" ? "voice memo" : "text"}
          </span>
          <strong>
            <SendIcon />
            Ready to review
          </strong>
        </div>
      </article>

      <p className={styles.demoNotice}>
        <ShieldIcon />
        Example using AI-generated images. No data is submitted.
      </p>
    </section>
  );
}
