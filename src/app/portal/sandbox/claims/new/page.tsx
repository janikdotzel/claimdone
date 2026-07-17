"use client";

import { usePortalHandoff } from "@/app/portal/portal-handoff-context";
import { getPortalFieldValues } from "@/lib/portal-field-values";

import styles from "../../sandbox.module.css";

export default function NewIncidentClaimPage() {
  const { preparedHandoff } = usePortalHandoff();
  const preparedClaim = preparedHandoff?.claim;
  const preparedValues = preparedClaim
    ? getPortalFieldValues(preparedClaim)
    : null;

  return (
    <main className={styles.main} id="main-content">
      <section aria-labelledby="sandbox-title" className={styles.formCard}>
        <div className={styles.formIntro}>
          <div>
            <p className={styles.breadcrumb}>Home / Claims / New motor claim</p>
            <p className={styles.eyebrow}>Motor claim · New incident</p>
            <h1 id="sandbox-title">Tell us about the incident</h1>
            <p>Review the details provided for this demo claim.</p>
          </div>
          <span className={styles.draftBadge}>Draft</span>
        </div>

        <form aria-label="Synthetic insurer claim form" className={styles.form}>
          <div className={styles.field}>
            <label htmlFor="portal-damage">Damage</label>
            <input
              autoComplete="off"
              data-portal-field="damage"
              defaultValue={preparedValues?.damage ?? ""}
              id="portal-damage"
              name="damage"
              placeholder="Describe the visible damage"
              type="text"
            />
          </div>

          <div className={styles.field}>
            <label htmlFor="portal-date-time">Date and time</label>
            <input
              autoComplete="off"
              data-portal-field="dateTime"
              defaultValue={preparedValues?.dateTime ?? ""}
              id="portal-date-time"
              name="dateTime"
              placeholder="When the accident happened"
              type="text"
            />
          </div>

          <div className={styles.field}>
            <label htmlFor="portal-location">Location</label>
            <input
              autoComplete="off"
              data-portal-field="location"
              defaultValue={preparedValues?.location ?? ""}
              id="portal-location"
              name="location"
              placeholder="Where the accident happened"
              type="text"
            />
          </div>

          <div className={styles.field}>
            <label htmlFor="portal-attachments">Attached photos</label>
            <input
              autoComplete="off"
              data-portal-field="attachedPhotos"
              defaultValue={preparedValues?.attachedPhotos ?? ""}
              id="portal-attachments"
              name="attachments"
              placeholder="Number of photos"
              type="text"
            />
          </div>

          <div className={`${styles.field} ${styles.wideField}`}>
            <label htmlFor="portal-description">What happened</label>
            <textarea
              data-portal-field="whatHappened"
              defaultValue={preparedValues?.whatHappened ?? ""}
              id="portal-description"
              name="description"
              placeholder="Describe how the accident happened"
              rows={4}
            />
          </div>
        </form>

        <footer className={styles.formFooter}>
          <span aria-hidden="true">◇</span>
          <p>
            Sandbox only. This form has no submit action and sends nothing to
            an insurer.
          </p>
        </footer>
      </section>
    </main>
  );
}
