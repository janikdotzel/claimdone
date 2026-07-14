import Link from "next/link";

import styles from "./sandbox-index.module.css";

export default function SandboxIndexPage() {
  return (
    <main className={styles.page} id="main-content">
      <section className={styles.card}>
        <span>ClaimDone developer sandbox</span>
        <h1>Choose a portal layout</h1>
        <p>
          Both layouts store the same synthetic fields and stop at review. Neither route can
          approve or submit a claim.
        </p>
        <div className={styles.links}>
          <Link href="/sandbox/A/cases/demo-layout-a">Open layout A</Link>
          <Link href="/sandbox/B/cases/demo-layout-b">Open layout B</Link>
        </div>
      </section>
    </main>
  );
}
