import Link from "next/link";

import styles from "./sandbox.module.css";

export default function InsurerPortalSandboxHomePage() {
  return (
    <main className={styles.main} id="main-content">
      <section aria-labelledby="portal-home-title" className={styles.portalHome}>
        <div className={styles.pageIntro}>
          <p className={styles.eyebrow}>Motor insurance</p>
          <h1 id="portal-home-title">Welcome to Demo Mutual</h1>
          <p>Manage your demo policy and claims in one place.</p>
        </div>

        <div className={styles.homeGrid}>
          <article className={styles.policyCard}>
            <div className={styles.cardTopline}>
              <div>
                <p className={styles.cardLabel}>Your motor policy</p>
                <h2>Comprehensive cover</h2>
              </div>
              <span className={styles.policyBadge}>Demo cover</span>
            </div>
            <dl className={styles.policyDetails}>
              <div>
                <dt>Policy number</dt>
                <dd>DM-DEMO-2048</dd>
              </div>
              <div>
                <dt>Cover</dt>
                <dd>Comprehensive</dd>
              </div>
            </dl>
          </article>

          <article className={styles.actionCard}>
            <span aria-hidden="true" className={styles.actionGlyph}>
              ↗
            </span>
            <p className={styles.cardLabel}>Claims</p>
            <h2>Report an accident</h2>
            <p>Start or review a motor claim.</p>
            <Link
              className={styles.portalAction}
              data-portal-action="open_claims"
              href="/portal/sandbox/claims"
            >
              View claims
              <span aria-hidden="true">→</span>
            </Link>
          </article>
        </div>

        <div className={styles.sandboxNotice}>
          <span aria-hidden="true">◇</span>
          <p>Sandbox only. No information is sent to an insurer.</p>
        </div>
      </section>
    </main>
  );
}
