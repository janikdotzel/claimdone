import Link from "next/link";
import type { ReactNode } from "react";

import { Alert } from "./alert";
import { Card, CardContent, CardHeader, CardTitle } from "./card";
import { LockIcon, ShieldIcon, SparkIcon } from "./icons";

export function SandboxBanner() {
  return (
    <div className="sandbox-banner" role="note">
      <div className="sandbox-banner__inner">
        <span className="sandbox-banner__badge">
          <ShieldIcon />
          Sandbox only
        </span>
        <p>
          This is a staged demo. Nothing is sent to an insurer, and no real claim is submitted.
        </p>
        <span className="sandbox-banner__boundary">
          <LockIcon />
          Human approval required
        </span>
      </div>
    </div>
  );
}

export function ProductChrome({ children }: { children: ReactNode }) {
  return (
    <div className="product-chrome">
      <header className="product-header">
        <div className="product-header__inner">
          <Link aria-label="ClaimDone home" className="brand" href="/">
            <span aria-hidden="true" className="brand__mark">
              <SparkIcon />
            </span>
            <span>ClaimDone</span>
          </Link>
          <nav aria-label="Primary navigation" className="product-nav">
            <Link href="/">Overview</Link>
            <Link href="/components">Components</Link>
            <Link className="product-nav__cta" href="/claim/new">
              Start sandbox claim
            </Link>
          </nav>
        </div>
      </header>
      <SandboxBanner />
      {children}
    </div>
  );
}

type PageShellProps = {
  aside: ReactNode;
  children: ReactNode;
  description: string;
  eyebrow?: string;
  title: string;
};

export function PageShell({
  aside,
  children,
  description,
  eyebrow,
  title,
}: PageShellProps) {
  return (
    <main className="page-shell" id="main-content">
      <header className="page-heading">
        {eyebrow ? <p className="page-heading__eyebrow">{eyebrow}</p> : null}
        <h1>{title}</h1>
        <p>{description}</p>
      </header>
      <div className="page-shell__grid">
        <div className="page-shell__main">{children}</div>
        <aside aria-label="Context and safety" className="page-shell__aside">
          {aside}
        </aside>
      </div>
    </main>
  );
}

export function HumanBoundaryCard() {
  return (
    <Card className="boundary-card" tone="accent">
      <CardHeader>
        <span className="boundary-card__icon">
          <LockIcon />
        </span>
        <CardTitle>Approval stays with you</CardTitle>
      </CardHeader>
      <CardContent>
        <p>
          ClaimDone can prepare and verify a sandbox draft. It cannot approve, submit, send, or pay anything.
        </p>
        <ul className="boundary-list">
          <li>Agent access stops at review</li>
          <li>A separate human action is always required</li>
          <li>Every value keeps its evidence source</li>
        </ul>
      </CardContent>
    </Card>
  );
}

export function MismatchBoundaryNotice() {
  return (
    <Alert title="Review blocked" tone="blocked">
      A rendered field differs from the verified claim value. Review remains unavailable until the mismatch is repaired and checked again.
    </Alert>
  );
}
