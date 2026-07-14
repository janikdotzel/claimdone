import type { ReactNode } from "react";

import { classNames } from "./class-names";
import { CheckIcon, CircleInfoIcon, LockIcon, WarningIcon } from "./icons";

export type GateStatus = "passed" | "blocked" | "pending";

type GateBadgeProps = {
  gateId: string;
  label: string;
  reason?: string;
  status: GateStatus;
};

export function GateBadge({ gateId, label, reason, status }: GateBadgeProps) {
  const stateLabel =
    status === "passed" ? "Passed" : status === "blocked" ? "Blocked" : "Pending";

  return (
    <span
      aria-label={`${gateId} ${label}: ${stateLabel}${reason ? `. ${reason}` : ""}`}
      className={`gate-badge gate-badge--${status}`}
      title={reason}
    >
      <span className="gate-badge__id">{gateId}</span>
      <span>{label}</span>
      <span aria-hidden="true" className="gate-badge__dot" />
      <span>{stateLabel}</span>
    </span>
  );
}

type ProvenanceStatus = "observed" | "user_stated" | "unknown" | "not_supported";

type ProvenanceChipProps = {
  confidence?: number;
  source: string;
  status: ProvenanceStatus;
};

const provenanceLabels: Record<ProvenanceStatus, string> = {
  not_supported: "Not supported",
  observed: "Observed",
  unknown: "Unknown",
  user_stated: "User stated",
};

function formatConfidence(confidence: number) {
  const bounded = Math.max(0, Math.min(1, confidence));
  const label =
    bounded >= 0.9 ? "High confidence" : bounded >= 0.8 ? "Good confidence" : "Low confidence";
  return `${label} · ${Math.round(bounded * 100)}%`;
}

export function ProvenanceChip({ confidence, source, status }: ProvenanceChipProps) {
  const confidenceLabel = confidence === undefined ? null : formatConfidence(confidence);
  const label = `${provenanceLabels[status]} · ${source}${confidenceLabel ? ` · ${confidenceLabel}` : ""}`;

  return (
    <span
      aria-label={label}
      className={classNames("provenance-chip", `provenance-chip--${status}`)}
    >
      <span aria-hidden="true" className="provenance-chip__dot" />
      <span>{provenanceLabels[status]}</span>
      <span aria-hidden="true">·</span>
      <span>{source}</span>
      {confidenceLabel ? (
        <>
          <span aria-hidden="true">·</span>
          <span>{confidenceLabel}</span>
        </>
      ) : null}
    </span>
  );
}

export type StateViewVariant = "empty" | "loading" | "error" | "blocked" | "success";

type StateViewProps = {
  action?: ReactNode;
  description: string;
  title: string;
  variant: StateViewVariant;
};

export function StateView({ action, description, title, variant }: StateViewProps) {
  const isAlert = variant === "error" || variant === "blocked";
  const Icon =
    variant === "success"
      ? CheckIcon
      : variant === "blocked"
        ? LockIcon
        : variant === "error"
          ? WarningIcon
          : CircleInfoIcon;

  return (
    <div
      aria-busy={variant === "loading" ? true : undefined}
      aria-live={isAlert ? "assertive" : "polite"}
      className={`state-view state-view--${variant}`}
      role={isAlert ? "alert" : "status"}
    >
      <span className="state-view__icon">
        {variant === "loading" ? (
          <span aria-hidden="true" className="state-view__spinner" />
        ) : (
          <Icon />
        )}
      </span>
      <div className="state-view__copy">
        <h2 className="state-view__title">{title}</h2>
        <p className="state-view__description">{description}</p>
        {action ? <div className="state-view__action">{action}</div> : null}
      </div>
    </div>
  );
}
