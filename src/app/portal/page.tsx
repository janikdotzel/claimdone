"use client";

import Image from "next/image";
import Link from "next/link";

import styles from "../page.module.css";
import { usePortalHandoff } from "./portal-handoff-context";

export default function PortalHandoffPage() {
  const { preparedHandoff, setPreparedHandoff } = usePortalHandoff();

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.headerInner}>
          <Link aria-label="ClaimDone home" className={styles.brand} href="/">
            <span aria-hidden="true" className={styles.brandMark}>
              ✓
            </span>
            ClaimDone
          </Link>
        </div>
      </header>

      <main className={`${styles.main} ${styles.portalMain}`} id="main-content">
        <section
          aria-labelledby="portal-title"
          className={`${styles.portalCard} ${
            preparedHandoff ? styles.portalResultCard : ""
          }`}
        >
          <div className={styles.portalTopline}>
            <p className={styles.claimType}>Insurer portal sandbox</p>
            <span className={styles.sandboxBadge}>Sandbox only</span>
          </div>

          {preparedHandoff ? (
            <>
              <div className={styles.portalPreparedSignal}>
                <span aria-hidden="true">✓</span>
                <strong>Filled by Computer Use</strong>
              </div>

              <h1 id="portal-title">Insurer portal prepared</h1>
              <p className={styles.portalLead}>
                Computer Use filled this synthetic form for your review.
              </p>

              <figure className={styles.portalScreenshot}>
                <Image
                  alt="Completed insurer portal sandbox form filled by Computer Use"
                  height={720}
                  priority
                  src={preparedHandoff.screenshotDataUrl}
                  unoptimized
                  width={1280}
                />
                <figcaption>
                  <span>Final sandbox view</span>
                  <Link
                    className={styles.portalScreenshotLink}
                    href="/portal/sandbox/claims/new"
                  >
                    Open filled Demo Mutual portal
                    <span aria-hidden="true">↗</span>
                  </Link>
                </figcaption>
              </figure>

              <div className={styles.portalBoundary}>
                <p>
                  <strong>Sandbox only.</strong> Nothing was submitted.
                </p>
              </div>
            </>
          ) : (
            <>
              <div aria-hidden="true" className={styles.portalEmptyMark}>
                ↗
              </div>
              <h1 id="portal-title">No prepared portal yet</h1>
              <p className={styles.portalLead}>
                Start from a ready claim to let Computer Use fill the insurer portal
                sandbox.
              </p>
              <div className={styles.portalBoundary}>
                <p>
                  <strong>Sandbox only.</strong> Nothing was submitted.
                </p>
              </div>
            </>
          )}

          <Link
            className={styles.primaryAction}
            href="/"
            onClick={() => setPreparedHandoff(null)}
          >
            <span aria-hidden="true">←</span>
            Back to claim
          </Link>
        </section>
      </main>
    </div>
  );
}
