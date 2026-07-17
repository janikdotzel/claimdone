import type { Metadata } from "next";
import Link from "next/link";

import styles from "./sandbox-index.module.css";

export const metadata: Metadata = {
  robots: { index: false },
  title: "Developer sandbox",
};

export default function SandboxIndexPage() {
  return (
    <main className={styles.page} id="main-content">
      <div className={styles.shell}>
        <header className={styles.header}>
          <div className={styles.brand}>
            <span className={styles.brandMark} aria-hidden="true">
              CD
            </span>
            <span>ClaimDone</span>
          </div>
          <span className={styles.sandboxTag}>Developer sandbox</span>
        </header>

        <section className={styles.card}>
          <div className={styles.introduction}>
            <span className={styles.eyebrow}>Safe claim-flow preview</span>
            <h1>Choose how to review the same claim.</h1>
            <p>
              Both layouts use the same synthetic information and stop at human review. No
              route can approve or submit a claim.
            </p>
            <div className={styles.links}>
              <Link href="/sandbox/A/cases/demo-layout-a">Explore layout A</Link>
              <Link href="/sandbox/B/cases/demo-layout-b">Explore layout B</Link>
            </div>
          </div>

          <ol className={styles.journey} aria-label="Sandbox claim journey">
            <li>
              <span>01</span>
              <div>
                <strong>Add evidence</strong>
                <p>Use three staged images from the incident.</p>
              </div>
            </li>
            <li>
              <span>02</span>
              <div>
                <strong>Confirm details</strong>
                <p>Check the prepared incident and policy information.</p>
              </div>
            </li>
            <li>
              <span>03</span>
              <div>
                <strong>Review the claim</strong>
                <p>Reach a clear, read-only summary for human review.</p>
              </div>
            </li>
          </ol>
        </section>

        <p className={styles.footnote}>
          Layout changes the order of the experience, never its safety boundary.
        </p>
      </div>
    </main>
  );
}
