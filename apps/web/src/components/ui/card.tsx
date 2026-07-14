import type { HTMLAttributes, ReactNode } from "react";

import { classNames } from "./class-names";

type CardProps = HTMLAttributes<HTMLElement> & {
  children: ReactNode;
  tone?: "default" | "soft" | "accent";
};

export function Card({ children, className, tone = "default", ...props }: CardProps) {
  return (
    <section className={classNames("card", `card--${tone}`, className)} {...props}>
      {children}
    </section>
  );
}

export function CardHeader({ children, className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={classNames("card__header", className)} {...props}>
      {children}
    </div>
  );
}

export function CardTitle({ children, className, ...props }: HTMLAttributes<HTMLHeadingElement>) {
  return (
    <h2 className={classNames("card__title", className)} {...props}>
      {children}
    </h2>
  );
}

export function CardDescription({
  children,
  className,
  ...props
}: HTMLAttributes<HTMLParagraphElement>) {
  return (
    <p className={classNames("card__description", className)} {...props}>
      {children}
    </p>
  );
}

export function CardContent({ children, className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={classNames("card__content", className)} {...props}>
      {children}
    </div>
  );
}

export function CardFooter({ children, className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={classNames("card__footer", className)} {...props}>
      {children}
    </div>
  );
}
