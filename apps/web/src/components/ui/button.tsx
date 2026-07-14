import Link from "next/link";
import type {
  ButtonHTMLAttributes,
  ComponentProps,
  ReactNode,
} from "react";

import { classNames } from "./class-names";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "default" | "small";

type SharedButtonProps = {
  children: ReactNode;
  className?: string;
  leadingIcon?: ReactNode;
  size?: ButtonSize;
  variant?: ButtonVariant;
};

export type ButtonProps = SharedButtonProps &
  ButtonHTMLAttributes<HTMLButtonElement> & {
    isLoading?: boolean;
    loadingLabel?: string;
  };

export function Button({
  children,
  className,
  disabled,
  isLoading = false,
  leadingIcon,
  loadingLabel = "Working…",
  size = "default",
  type = "button",
  variant = "primary",
  ...props
}: ButtonProps) {
  return (
    <button
      className={classNames("button", `button--${variant}`, `button--${size}`, className)}
      disabled={disabled || isLoading}
      type={type}
      {...props}
    >
      {isLoading ? <span aria-hidden="true" className="button__spinner" /> : leadingIcon}
      <span>{isLoading ? loadingLabel : children}</span>
    </button>
  );
}

type ButtonLinkProps = SharedButtonProps &
  Omit<ComponentProps<typeof Link>, "children" | "className" | "href"> & {
    href: string;
  };

export function ButtonLink({
  children,
  className,
  href,
  leadingIcon,
  size = "default",
  variant = "primary",
  ...props
}: ButtonLinkProps) {
  return (
    <Link
      className={classNames("button", `button--${variant}`, `button--${size}`, className)}
      href={href}
      {...props}
    >
      {leadingIcon}
      <span>{children}</span>
    </Link>
  );
}
