import Link from "next/link";

import styles from "./app-state.module.css";

export default function NotFound() {
  return (
    <main className={styles.page} id="main-content">
      <section className={styles.panel}>
        <span aria-hidden="true" className={styles.mark}>404</span>
        <p className={styles.eyebrow}>Page not found</p>
        <h1>This route is not part of your claim</h1>
        <p className={styles.copy}>
          The link may be outdated. Return home or begin a new claim with three incident photos.
        </p>
        <div className={styles.actions}>
          <Link className={styles.primary} href="/claim/new">Start a claim</Link>
          <Link className={styles.secondary} href="/">Return home</Link>
        </div>
      </section>
    </main>
  );
}
