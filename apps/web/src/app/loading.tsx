import styles from "./app-state.module.css";

export default function Loading() {
  return (
    <main className={styles.page} id="main-content">
      <section aria-busy="true" aria-live="polite" className={styles.panel}>
        <span aria-hidden="true" className={styles.mark}>✓</span>
        <p className={styles.eyebrow}>ClaimDone</p>
        <h1>Preparing the next step</h1>
        <p className={styles.copy}>
          Your claim flow is loading. Your evidence and review boundary remain unchanged.
        </p>
        <div aria-hidden="true" className={styles.progress}><span /></div>
      </section>
    </main>
  );
}
