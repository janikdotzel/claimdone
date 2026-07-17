import type { ReactNode } from "react";

import styles from "./sandbox.module.css";

export default function InsurerPortalSandboxLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.headerInner}>
          <div className={styles.portalBrand}>
            <span aria-hidden="true" className={styles.portalMark}>
              DM
            </span>
            <span>Demo Mutual</span>
          </div>
          <span className={styles.sandboxBadge}>Synthetic sandbox</span>
        </div>
      </header>

      {children}
    </div>
  );
}
