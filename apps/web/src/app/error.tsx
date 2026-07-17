"use client";

import Link from "next/link";

import styles from "./app-state.module.css";

export default function ErrorPage({ reset }: Readonly<{ reset: () => void }>) {
  return (
    <main className={styles.page} id="main-content">
      <section className={styles.panel} role="alert">
        <span aria-hidden="true" className={styles.mark}>!</span>
        <p className={styles.eyebrow}>Claim flow paused</p>
        <h1>This page could not finish loading</h1>
        <p className={styles.copy}>
          Try the page again. If the problem continues, return home and start from the last confirmed step.
        </p>
        <div className={styles.actions}>
          <button className={styles.primary} onClick={reset} type="button">Try again</button>
          <Link className={styles.secondary} href="/">Return home</Link>
        </div>
      </section>
    </main>
  );
}
