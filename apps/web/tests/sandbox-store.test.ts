import { afterEach, describe, expect, it, vi } from "vitest";

import {
  DELETE as deletePortal,
  GET as getPortal,
} from "../src/app/api/sandbox/cases/[caseId]/route";
import { GET as getRenderedValues } from "../src/app/api/sandbox/cases/[caseId]/rendered-values/route";
import {
  DELETE as resetAllPortals,
  POST as resetPortal,
} from "../src/app/api/dev/reset/route";
import { PUT as putDraft } from "../src/app/api/sandbox/cases/[caseId]/draft/route";
import { POST as postReview } from "../src/app/api/sandbox/cases/[caseId]/review/route";
import type {
  PortalDraftFields,
  PortalView,
} from "../src/features/sandbox/contracts";
import { COMPLETE_PORTAL_FIELDS } from "../src/features/sandbox/fixtures";
import {
  PortalConflictError,
  PortalNotFoundError,
  PortalStateConflictError,
  SandboxPortalStore,
  sandboxPortalStore,
} from "../src/features/sandbox/store";
import { assertPortalTransition, PortalInputError } from "../src/features/sandbox/validation";

const FIXED_NOW = new Date("2026-07-14T14:00:00Z");
type PortalTextFieldName = Exclude<
  keyof PortalDraftFields,
  "attachments" | "counterpartyKnown"
>;
const PORTAL_TEXT_BOUNDARY_CASES = [
  { exact: "d".repeat(10), field: "incidentDate", tooLong: "d".repeat(11) },
  { exact: "t".repeat(21), field: "incidentTime", tooLong: "t".repeat(22) },
  { exact: "l".repeat(512), field: "location", tooLong: "l".repeat(513) },
  {
    exact: "😀".repeat(512),
    field: "claimantName",
    tooLong: "😀".repeat(513),
  },
  {
    exact: "p".repeat(512),
    field: "policyReference",
    tooLong: "p".repeat(513),
  },
  {
    exact: "v".repeat(512),
    field: "vehicleRegistration",
    tooLong: "v".repeat(513),
  },
  { exact: "n".repeat(4_000), field: "narrative", tooLong: "n".repeat(4_001) },
] as const satisfies readonly {
  readonly exact: string;
  readonly field: PortalTextFieldName;
  readonly tooLong: string;
}[];

afterEach(() => vi.useRealTimers());

describe("SandboxPortalStore", () => {
  it("persists a reviewed session across reads", () => {
    const store = fixedStore();
    const created = store.reset("case-reload", "A", "empty");
    const saved = store.saveDraft("case-reload", "A", created.version, COMPLETE_PORTAL_FIELDS);
    const reviewed = store.advanceToReview("case-reload", "A", saved.version);

    const reloaded = store.read("case-reload", "A");

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
    const created = store.reset("case-conflict", "A", "empty");
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
    const created = store.reset("case-incomplete", "A", "empty");

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

  it("deletes one case or resets all cases deterministically", () => {
    const store = fixedStore();
    store.reset("case-delete-one", "A", "empty");
    store.reset("case-delete-two", "B", "empty");

    expect(store.delete("case-delete-one")).toBe(true);
    expect(store.delete("case-delete-one")).toBe(false);
    expect(store.audit("case-delete-one")).toEqual([]);
    expect(store.resetAll()).toBe(1);
    expect(store.resetAll()).toBe(0);
    expect(store.audit("case-delete-two")).toEqual([]);
  });

  it("does not create, audit, or bind a variant while reading an unknown case", () => {
    const store = fixedStore();

    expect(() => store.read("case-missing", "A")).toThrow(PortalNotFoundError);
    expect(() => store.renderedValues("case-missing", "B")).toThrow(
      PortalNotFoundError,
    );
    expect(store.audit("case-missing")).toEqual([]);

    const created = store.reset("case-missing", "B", "empty");
    expect(created.variant).toBe("B");
    expect(created.auditCount).toBe(1);
  });

  it("exposes rendered values only from review and preserves raw draft strings", () => {
    const store = fixedStore();
    const rawFields = {
      ...COMPLETE_PORTAL_FIELDS,
      claimantName: "  Demo Claimant  ",
      location: "  Berlin  ",
      narrative: "  Raw staged narrative.  ",
    };
    const created = store.reset("case-rendered", "A", "empty");
    const saved = store.saveDraft("case-rendered", "A", created.version, rawFields);

    expect(() => store.renderedValues("case-rendered", "A")).toThrow(
      PortalStateConflictError,
    );

    const reviewed = store.advanceToReview("case-rendered", "A", saved.version);
    const rendered = store.renderedValues("case-rendered", "A");

    expect(rendered).toEqual({
      caseId: "case-rendered",
      contractVersion: "4.0.0",
      fields: rawFields,
      renderedAt: "2026-07-14T14:00:00.000Z",
      state: "review",
      variant: "A",
      version: reviewed.version,
    });
  });
});

describe("sandbox portal route handlers", () => {
  it("deletes one portal case and supports a deterministic reset-all route", async () => {
    const caseId = "route-delete";
    const context = { params: Promise.resolve({ caseId }) };
    await resetPortal(
      resetRequest({ caseId, fixture: "empty", variant: "A" }),
    );

    const deleted = await deletePortal(
      new Request(`http://claimdone.local/api/sandbox/cases/${caseId}`, {
        method: "DELETE",
      }),
      context,
    );
    const resetAll = await resetAllPortals();

    expect(deleted.status).toBe(204);
    expect(await resetAll.json()).toMatchObject({ deletedCount: expect.any(Number) });
  });

  it("round-trips draft to review through the server handlers", async () => {
    const caseId = "route-roundtrip";
    const context = { params: Promise.resolve({ caseId }) };
    await resetPortal(resetRequest({ caseId, fixture: "empty", variant: "A" }));
    const initialResponse = await getPortal(portalRequest(caseId, "A"), context);
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
    const reloadResponse = await getPortal(portalRequest(caseId, "A"), context);
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
    expect(Object.keys(reloaded).sort()).toEqual([
      "auditCount",
      "caseId",
      "contractVersion",
      "fields",
      "state",
      "updatedAt",
      "variant",
      "version",
    ]);
    expect(JSON.stringify(reloaded)).not.toMatch(/capabilit|approval|receipt/i);
  });

  it("returns field-level 422 errors and rejects unknown body fields", async () => {
    const caseId = "route-invalid";
    const context = { params: Promise.resolve({ caseId }) };
    await resetPortal(resetRequest({ caseId, fixture: "empty", variant: "B" }));
    const initial = (await (
      await getPortal(portalRequest(caseId, "B"), context)
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

  it.each(PORTAL_TEXT_BOUNDARY_CASES)(
    "accepts the exact $field V4 code-point limit and rejects max plus one",
    async ({ exact, field, tooLong }) => {
      const caseId = `route-boundary-${field.toLowerCase()}`;
      const context = { params: Promise.resolve({ caseId }) };
      const initial = (await (
        await resetPortal(resetRequest({ caseId, fixture: "empty", variant: "A" }))
      ).json()) as PortalView;

      const acceptedResponse = await putDraft(
        draftRequest(caseId, "A", initial.version, {
          ...COMPLETE_PORTAL_FIELDS,
          [field]: exact,
        }),
        context,
      );
      const accepted = (await acceptedResponse.json()) as PortalView;
      const rejectedResponse = await putDraft(
        draftRequest(caseId, "A", accepted.version, {
          ...COMPLETE_PORTAL_FIELDS,
          [field]: tooLong,
        }),
        context,
      );

      expect(acceptedResponse.status).toBe(200);
      expect(accepted.fields[field]).toBe(exact);
      expect(rejectedResponse.status).toBe(422);
      expect(await rejectedResponse.json()).toMatchObject({
        error: { code: "PORTAL_INPUT_INVALID" },
      });
      expect(sandboxPortalStore.read(caseId, "A")).toEqual(accepted);
    },
  );

  it("returns PORTAL_NOT_FOUND without creating a session or audit entry", async () => {
    const caseId = "route-missing-read";
    const context = { params: Promise.resolve({ caseId }) };
    sandboxPortalStore.delete(caseId);

    const first = await getPortal(portalRequest(caseId, "A"), context);
    const second = await getPortal(portalRequest(caseId, "B"), context);

    expect(first.status).toBe(404);
    expect(await first.json()).toMatchObject({ error: { code: "PORTAL_NOT_FOUND" } });
    expect(second.status).toBe(404);
    expect(await second.json()).toMatchObject({ error: { code: "PORTAL_NOT_FOUND" } });
    expect(sandboxPortalStore.audit(caseId)).toEqual([]);
    expect(() => sandboxPortalStore.read(caseId, "A")).toThrow(PortalNotFoundError);
    expect(() => sandboxPortalStore.read(caseId, "B")).toThrow(PortalNotFoundError);
  });

  it("keeps repeated session reads byte-equivalent and side-effect free", async () => {
    const caseId = "route-repeat-read";
    const context = { params: Promise.resolve({ caseId }) };
    await resetPortal(resetRequest({ caseId, fixture: "complete", variant: "A" }));
    const beforeAudit = sandboxPortalStore.audit(caseId);

    const first = await getPortal(portalRequest(caseId, "A"), context);
    const second = await getPortal(portalRequest(caseId, "A"), context);

    expect(await first.text()).toBe(await second.text());
    expect(sandboxPortalStore.audit(caseId)).toEqual(beforeAudit);
  });

  it("rejects draft rendered-values reads and returns an exact V4 review snapshot", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-14T14:00:00Z"));
    const caseId = "route-rendered-values";
    const context = { params: Promise.resolve({ caseId }) };
    const rawFields = {
      ...COMPLETE_PORTAL_FIELDS,
      claimantName: "  Demo Claimant  ",
      location: "  Berlin  ",
      narrative: "  Raw staged narrative.  ",
    };
    const initial = (await (
      await resetPortal(resetRequest({ caseId, fixture: "empty", variant: "B" }))
    ).json()) as PortalView;
    const saveResponse = await putDraft(
      new Request(`http://claimdone.local/api/sandbox/cases/${caseId}/draft?variant=B`, {
        body: JSON.stringify({ expectedVersion: initial.version, fields: rawFields }),
        headers: { "Content-Type": "application/json" },
        method: "PUT",
      }),
      context,
    );
    const saved = (await saveResponse.json()) as PortalView;

    const draftRead = await getRenderedValues(
      portalRequest(caseId, "B", "/rendered-values"),
      context,
    );
    expect(draftRead.status).toBe(409);
    expect(await draftRead.json()).toMatchObject({
      error: { code: "PORTAL_STATE_CONFLICT" },
    });

    const reviewResponse = await postReview(
      new Request(`http://claimdone.local/api/sandbox/cases/${caseId}/review?variant=B`, {
        body: JSON.stringify({ expectedVersion: saved.version }),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      }),
      context,
    );
    const reviewed = (await reviewResponse.json()) as PortalView;
    const sessionBeforeReads = sandboxPortalStore.read(caseId, "B");
    const auditBeforeReads = sandboxPortalStore.audit(caseId);
    vi.setSystemTime(new Date("2026-07-14T14:00:01Z"));
    const renderedResponse = await getRenderedValues(
      portalRequest(caseId, "B", "/rendered-values"),
      context,
    );
    const rendered = (await renderedResponse.json()) as Record<string, unknown>;
    vi.setSystemTime(new Date("2026-07-14T14:00:02Z"));
    const repeatedResponse = await getRenderedValues(
      portalRequest(caseId, "B", "/rendered-values"),
      context,
    );
    const repeated = (await repeatedResponse.json()) as Record<string, unknown>;

    expect(renderedResponse.status).toBe(200);
    expect(Object.keys(rendered).sort()).toEqual([
      "caseId",
      "contractVersion",
      "fields",
      "renderedAt",
      "state",
      "variant",
      "version",
    ]);
    expect(rendered).toMatchObject({
      caseId,
      contractVersion: "4.0.0",
      fields: rawFields,
      state: "review",
      variant: "B",
      version: reviewed.version,
    });
    expect(JSON.stringify(rendered)).not.toMatch(/audit|capabilit|approval|receipt/i);
    expect(repeated).toMatchObject({
      caseId,
      contractVersion: "4.0.0",
      fields: rawFields,
      renderedAt: expect.any(String),
      state: "review",
      variant: "B",
      version: reviewed.version,
    });
    expect(repeated).toEqual({ ...rendered, renderedAt: expect.any(String) });
    expect(rendered.renderedAt).toBe("2026-07-14T14:00:01.000Z");
    expect(repeated.renderedAt).toBe("2026-07-14T14:00:02.000Z");
    expect(sandboxPortalStore.read(caseId, "B")).toEqual(sessionBeforeReads);
    expect(sandboxPortalStore.audit(caseId)).toEqual(auditBeforeReads);
  });
});

function fixedStore(): SandboxPortalStore {
  return new SandboxPortalStore(() => new Date(FIXED_NOW));
}

function portalRequest(
  caseId: string,
  variant: "A" | "B",
  suffix = "",
): Request {
  return new Request(
    `http://claimdone.local/api/sandbox/cases/${caseId}${suffix}?variant=${variant}`,
  );
}

function resetRequest(body: {
  readonly caseId: string;
  readonly fixture: "empty" | "complete";
  readonly variant: "A" | "B";
}): Request {
  return new Request("http://claimdone.local/api/dev/reset", {
    body: JSON.stringify(body),
    headers: { "Content-Type": "application/json" },
    method: "POST",
  });
}

function draftRequest(
  caseId: string,
  variant: "A" | "B",
  expectedVersion: number,
  fields: PortalDraftFields,
): Request {
  return new Request(
    `http://claimdone.local/api/sandbox/cases/${caseId}/draft?variant=${variant}`,
    {
      body: JSON.stringify({ expectedVersion, fields }),
      headers: { "Content-Type": "application/json" },
      method: "PUT",
    },
  );
}
