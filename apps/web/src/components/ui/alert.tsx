import type { HTMLAttributes, ReactNode } from "react";

import { classNames } from "./class-names";
import { CheckIcon, CircleInfoIcon, WarningIcon } from "./icons";

export type AlertTone = "info" | "success" | "warning" | "error" | "blocked";

type AlertProps = HTMLAttributes<HTMLDivElement> & {
  children: ReactNode;
  title: string;
  tone?: AlertTone;
};

export function Alert({ children, className, title, tone = "info", ...props }: AlertProps) {
  const isUrgent = tone === "error" || tone === "blocked";
  const Icon = tone === "success" ? CheckIcon : isUrgent || tone === "warning" ? WarningIcon : CircleInfoIcon;

  return (
    <div
      aria-live={isUrgent ? "assertive" : undefined}
      className={classNames("alert", `alert--${tone}`, className)}
      role={isUrgent ? "alert" : "note"}
      {...props}
    >
      <Icon className="alert__icon" />
      <div>
        <p className="alert__title">{title}</p>
        <div className="alert__body">{children}</div>
      </div>
    </div>
  );
}
