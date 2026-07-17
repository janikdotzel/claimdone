import Link from "next/link";

import styles from "../sandbox.module.css";

export default function InsurerPortalClaimsPage() {
  return (
    <main className={styles.main} id="main-content">
      <section aria-labelledby="claims-title" className={styles.claimsPage}>
        <p className={styles.breadcrumb}>
          <span>Home</span>
          <span aria-hidden="true">/</span>
          <strong>Claims</strong>
        </p>

        <div className={styles.pageIntro}>
          <p className={styles.eyebrow}>Motor insurance</p>
          <h1 id="claims-title">Claims</h1>
          <p>Report a new incident or review an existing claim.</p>
        </div>

        <article className={styles.newClaimCard}>
          <div className={styles.newClaimCopy}>
            <span aria-hidden="true" className={styles.actionGlyph}>
              +
            </span>
            <div>
              <p className={styles.cardLabel}>New incident</p>
              <h2>Tell us what happened</h2>
              <p>Start a motor claim with the details from your accident.</p>
            </div>
          </div>
          <Link
            className={styles.portalAction}
            data-portal-action="start_incident_claim"
            href="/portal/sandbox/claims/new"
          >
            Start a motor claim
            <span aria-hidden="true">→</span>
          </Link>
        </article>

        <p className={styles.emptyClaims}>No existing claims in this sandbox.</p>

        <div className={styles.sandboxNotice}>
          <span aria-hidden="true">◇</span>
          <p>Sandbox only. No information is sent to an insurer.</p>
        </div>
      </section>
    </main>
  );
}
