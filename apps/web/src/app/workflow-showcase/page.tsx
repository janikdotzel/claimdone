import type { Metadata } from "next";

import {
  SHOWCASE_EVENTS,
  WORKFLOW_SHOWCASE_SNAPSHOTS,
} from "../../features/workflow/fixtures";
import { WorkflowExperience } from "../../features/workflow/components";
import {
  INITIAL_WORKFLOW_EVENT_STORE,
  reduceWorkflowEventStore,
} from "../../features/workflow/store";
import styles from "./workflow-showcase.module.css";

export const metadata: Metadata = {
  robots: { index: false },
  title: "Workflow state showcase",
};

export default function WorkflowShowcasePage() {
  const store = SHOWCASE_EVENTS.reduce(
    (current, envelope) =>
      reduceWorkflowEventStore(current, { envelope, type: "EVENT_RECEIVED" }),
    INITIAL_WORKFLOW_EVENT_STORE,
  );
  return (
    <main className={styles.page} id="main-content">
      <header className={styles.header}>
        <p>Internal state reference</p>
        <h1>Claim workflow states</h1>
        <span>
          Each example preserves the same evidence, deterministic checks, and human-review boundary used in the product flow.
        </span>
      </header>
      <div className={styles.notice} role="note">
        Sandbox examples only · no claim is submitted
      </div>
      <div className={styles.states}>
        {WORKFLOW_SHOWCASE_SNAPSHOTS.map((snapshot) => (
          <section className={styles.state} key={snapshot.case.state}>
            <p className={styles.stateLabel}>{snapshot.case.state.replaceAll("_", " ")}</p>
            <WorkflowExperience
              events={store.events}
              mode="ready"
              showSandboxBanner={false}
              snapshot={snapshot}
            />
          </section>
        ))}
      </div>
    </main>
  );
}
