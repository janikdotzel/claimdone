import type { Metadata } from "next";
import Link from "next/link";

import {
  ArrowRightIcon,
  LockIcon,
  ShieldIcon,
  SparkIcon,
} from "../components/ui/icons";
import { LandingClaimDemo } from "./landing-claim-demo";
import {
  FileCheckIcon,
  OrderedListIcon,
  ScanIcon,
} from "./landing-icons";
import styles from "./landing.module.css";

export const metadata: Metadata = {
  description:
    "Three photos and a short statement are enough for ClaimDone to prepare a complete, traceable insurance claim for review.",
  title: "Report an accident without the paperwork",
};

const processSteps = [
  {
    description: "The agent identifies vehicle positions, visible damage, and the relevant road context.",
    Icon: ScanIcon,
    label: "Read photos",
  },
  {
    description: "Photo observations and your statement become one traceable account of the incident.",
    Icon: OrderedListIcon,
    label: "Organize facts",
  },
  {
    description: "Required details, conflicts, and missing information are checked deterministically.",
    Icon: ShieldIcon,
    label: "Check details",
  },
  {
    description: "Time, place, damage, people involved, and what happened become one concise claim draft.",
    Icon: FileCheckIcon,
    label: "Complete claim",
  },
] as const;

export default function HomePage() {
  return (
    <div className={styles.page}>
      <header className={styles.siteHeader}>
        <div className={styles.headerInner}>
          <Link aria-label="ClaimDone home" className={styles.brand} href="/">
            <span aria-hidden="true" className={styles.brandMark}>
              <SparkIcon />
            </span>
            <span>ClaimDone</span>
          </Link>

          <nav aria-label="Primary navigation" className={styles.navigation}>
            <a href="#how-it-works">How it works</a>
          </nav>

          <Link aria-label="Start a claim" className={styles.headerCta} href="/claim/new">
            <span>Start a claim</span>
            <ArrowRightIcon />
          </Link>
        </div>
      </header>

      <main id="main-content">
        <section className={styles.hero}>
          <div className={`${styles.shell} ${styles.heroLayout}`}>
            <div className={styles.heroCopy}>
              <p className={styles.eyebrow}>
                <span />
                Report an accident without the paperwork
              </p>
              <h1>
                Three photos. One short statement.
                <span>Your claim is ready to review.</span>
              </h1>
              <p className={styles.heroLead}>
                ClaimDone finds the important details, organizes what happened, and prepares a complete insurance claim for your review.
              </p>
              <div className={styles.heroActions}>
                <Link className={styles.primaryCta} href="/claim/new">
                  Start my claim
                  <ArrowRightIcon />
                </Link>
                <a className={styles.secondaryCta} href="#how-it-works">
                  See how it works
                </a>
              </div>
              <p className={styles.heroBoundary}>
                <LockIcon />
                You review every claim before anything is shared.
              </p>
            </div>

            <LandingClaimDemo />
          </div>
        </section>

        <section aria-labelledby="process-title" className={styles.processSection} id="how-it-works">
          <div className={styles.shell}>
            <div className={styles.processHeading}>
              <div>
                <p className={styles.eyebrow}>No black box</p>
                <h2 id="process-title">You can see what the Claim Agent does.</h2>
              </div>
              <p>
                Every step has one clear job. If information is missing or conflicts, the process stops and shows you the gap.
              </p>
            </div>

            <ol className={styles.processList}>
              {processSteps.map(({ description, Icon, label }, index) => (
                <li key={label}>
                  <div className={styles.processIndex}>
                    <span>0{index + 1}</span>
                    <Icon />
                  </div>
                  <h3>{label}</h3>
                  <p>{description}</p>
                </li>
              ))}
            </ol>
          </div>
        </section>
      </main>

      <footer className={styles.footer}>
        <div className={styles.footerInner}>
          <Link aria-label="ClaimDone home" className={styles.brand} href="/">
            <span aria-hidden="true" className={styles.brandMark}>
              <SparkIcon />
            </span>
            <span>ClaimDone</span>
          </Link>
          <p>Claim preparation demo · Decisions stay with people.</p>
          <a className={styles.footerLink} href="mailto:hello@claimdone.example">
            Contact
          </a>
        </div>
      </footer>
    </div>
  );
}
