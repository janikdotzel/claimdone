import { describe, expect, it } from "vitest";

import { GET as getPortal } from "../src/app/api/sandbox/cases/[caseId]/route";
import { PUT as putDraft } from "../src/app/api/sandbox/cases/[caseId]/draft/route";
import { POST as postReview } from "../src/app/api/sandbox/cases/[caseId]/review/route";
import type { PortalView } from "../src/features/sandbox/contracts";
import { COMPLETE_PORTAL_FIELDS } from "../src/features/sandbox/fixtures";
import { PortalConflictError, SandboxPortalStore } from "../src/features/sandbox/store";
import { assertPortalTransition, PortalInputError } from "../src/features/sandbox/validation";

const FIXED_NOW = new Date("2026-07-14T14:00:00Z");

describe("SandboxPortalStore", () => {
  it("persists a reviewed session across reads", () => {
    const store = fixedStore();
    const created = store.getOrCreate("case-reload", "A");
    const saved = store.saveDraft("case-reload", "A", created.version, COMPLETE_PORTAL_FIELDS);
    const reviewed = store.advanceToReview("case-reload", "A", saved.version);

    const reloaded = store.getOrCreate("case-reload", "A");

    expect(reviewed.state).toBe("review");
    expect(reloaded).toEqual(reviewed);
    expect(store.renderedValues("case-reload", "A").fields).toEqual(
      COMPLETE_PORTAL_FIELDS,
    );
  });

  it("produces identical reviewed values for layout A and B", () => {
    const store = fixedStore();
    for (const variant of ["A", "B"] as const) {
      const caseId = `case-layout-${variant.toLowerCase()}`;
      const created = store.reset(caseId, variant, "complete");
      store.advanceToReview(caseId, variant, created.version);
    }

    const layoutA = store.renderedValues("case-layout-a", "A");
    const layoutB = store.renderedValues("case-layout-b", "B");

    expect(layoutA.state).toBe("review");
    expect(layoutB.state).toBe("review");
    expect(layoutA.fields).toEqual(layoutB.fields);
  });

  it("rejects stale writes and invalid state jumps", () => {
    const store = fixedStore();
    const created = store.getOrCreate("case-conflict", "A");
    store.saveDraft("case-conflict", "A", created.version, COMPLETE_PORTAL_FIELDS);

    expect(() =>
      store.saveDraft("case-conflict", "A", created.version, COMPLETE_PORTAL_FIELDS),
    ).toThrow(PortalConflictError);
    expect(() => assertPortalTransition("draft", "receipt")).toThrow(PortalInputError);
    expect(() => assertPortalTransition("review", "human_approved")).toThrow(
      PortalInputError,
    );
  });

  it("blocks review until every required field and three attachments exist", () => {
    const store = fixedStore();
    const created = store.getOrCreate("case-incomplete", "A");

    expect(() => store.advanceToReview("case-incomplete", "A", created.version)).toThrow(
      PortalInputError,
    );
    try {
      store.advanceToReview("case-incomplete", "A", created.version);
    } catch (error) {
      expect(error).toBeInstanceOf(PortalInputError);
      expect((error as PortalInputError).fieldErrors.some((issue) => issue.field === "attachments"))
        .toBe(true);
    }
  });

  it("keeps audit summaries redacted", () => {
    const store = fixedStore();
    const created = store.reset("case-redacted", "A", "complete");
    const saved = store.saveDraft(
      "case-redacted",
      "A",
      created.version,
      COMPLETE_PORTAL_FIELDS,
    );
    store.advanceToReview("case-redacted", "A", saved.version);

    const serializedAudit = JSON.stringify(store.audit("case-redacted"));

    expect(serializedAudit).not.toContain(COMPLETE_PORTAL_FIELDS.claimantName);
    expect(serializedAudit).not.toContain(COMPLETE_PORTAL_FIELDS.policyReference);
    expect(serializedAudit).not.toContain(COMPLETE_PORTAL_FIELDS.vehicleRegistration);
    expect(serializedAudit).not.toContain(COMPLETE_PORTAL_FIELDS.narrative);
    expect(serializedAudit).toContain("attachmentCount");
  });

  it("resets deterministically and never seeds approved or receipt states", () => {
    const store = fixedStore();
    const first = store.reset("case-reset", "B", "complete");
    const second = store.reset("case-reset", "B", "complete");

    expect(second).toEqual(first);
    expect(second.state).toBe("draft");
    expect(second.version).toBe(1);
  });
});

describe("sandbox portal route handlers", () => {
  it("round-trips draft to review through the server handlers", async () => {
    const caseId = "route-roundtrip";
    const context = { params: Promise.resolve({ caseId }) };
    const initialResponse = await getPortal(
      new Request(`http://claimdone.local/api/sandbox/cases/${caseId}?variant=A`),
      context,
    );
    const initial = (await initialResponse.json()) as PortalView;

    const saveResponse = await putDraft(
      new Request(`http://claimdone.local/api/sandbox/cases/${caseId}/draft?variant=A`, {
        body: JSON.stringify({
          expectedVersion: initial.version,
          fields: COMPLETE_PORTAL_FIELDS,
        }),
        headers: { "Content-Type": "application/json" },
        method: "PUT",
      }),
      context,
    );
    const saved = (await saveResponse.json()) as PortalView;
    const reviewResponse = await postReview(
      new Request(`http://claimdone.local/api/sandbox/cases/${caseId}/review?variant=A`, {
        body: JSON.stringify({ expectedVersion: saved.version }),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      }),
      context,
    );
    const reviewed = (await reviewResponse.json()) as PortalView;
    const reloadResponse = await getPortal(
      new Request(`http://claimdone.local/api/sandbox/cases/${caseId}?variant=A`),
      context,
    );
    const reloaded = (await reloadResponse.json()) as PortalView;
    const repeatedReviewResponse = await postReview(
      new Request(`http://claimdone.local/api/sandbox/cases/${caseId}/review?variant=A`, {
        body: JSON.stringify({ expectedVersion: reviewed.version }),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      }),
      context,
    );

    expect(initialResponse.status).toBe(200);
    expect(saveResponse.status).toBe(200);
    expect(reviewResponse.status).toBe(200);
    expect(repeatedReviewResponse.status).toBe(422);
    expect(reviewed.state).toBe("review");
    expect(reloaded).toEqual(reviewed);
  });

  it("returns field-level 422 errors and rejects unknown body fields", async () => {
    const caseId = "route-invalid";
    const context = { params: Promise.resolve({ caseId }) };
    const initial = (await (
      await getPortal(
        new Request(`http://claimdone.local/api/sandbox/cases/${caseId}?variant=B`),
        context,
      )
    ).json()) as PortalView;

    const invalidSave = await putDraft(
      new Request(`http://claimdone.local/api/sandbox/cases/${caseId}/draft?variant=B`, {
        body: JSON.stringify({
          expectedVersion: initial.version,
          fields: COMPLETE_PORTAL_FIELDS,
          unexpected: true,
        }),
        headers: { "Content-Type": "application/json" },
        method: "PUT",
      }),
      context,
    );
    const review = await postReview(
      new Request(`http://claimdone.local/api/sandbox/cases/${caseId}/review?variant=B`, {
        body: JSON.stringify({ expectedVersion: initial.version }),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      }),
      context,
    );
    const reviewBody = (await review.json()) as {
      error: { fieldErrors: readonly { field: string }[] };
    };

    expect(invalidSave.status).toBe(422);
    expect(review.status).toBe(422);
    expect(reviewBody.error.fieldErrors.some((issue) => issue.field === "attachments")).toBe(
      true,
    );
  });
});

function fixedStore(): SandboxPortalStore {
  return new SandboxPortalStore(() => new Date(FIXED_NOW));
}
