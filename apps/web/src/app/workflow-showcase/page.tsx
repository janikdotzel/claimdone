import {
  SHOWCASE_EVENTS,
  WORKFLOW_SHOWCASE_SNAPSHOTS,
} from "../../features/workflow/fixtures";
import { WorkflowExperience } from "../../features/workflow/components";
import {
  INITIAL_WORKFLOW_EVENT_STORE,
  reduceWorkflowEventStore,
} from "../../features/workflow/store";

export default function WorkflowShowcasePage() {
  const store = SHOWCASE_EVENTS.reduce(
    (current, envelope) =>
      reduceWorkflowEventStore(current, { envelope, type: "EVENT_RECEIVED" }),
    INITIAL_WORKFLOW_EVENT_STORE,
  );
  return (
    <main id="main-content">
      {WORKFLOW_SHOWCASE_SNAPSHOTS.map((snapshot) => (
        <WorkflowExperience
          events={store.events}
          key={snapshot.case.state}
          mode="ready"
          snapshot={snapshot}
        />
      ))}
    </main>
  );
}
