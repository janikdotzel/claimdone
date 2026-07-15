import { afterEach, describe, expect, it, vi } from "vitest";

import { POST as abortPortalRun } from "../src/app/api/internal/portal-runs/abort/route";
import { POST as injectRenderFault } from "../src/app/api/internal/portal-runs/inject-render-fault/route";
import { POST as releasePortalRun } from "../src/app/api/internal/portal-runs/release/route";
import { POST as repairRenderFault } from "../src/app/api/internal/portal-runs/repair-render-fault/route";
import { POST as setupPortalRun } from "../src/app/api/internal/portal-runs/setup/route";
import {
  DELETE as deletePortal,
  GET as getPortal,
} from "../src/app/api/sandbox/cases/[caseId]/route";
import { PUT as putDraft } from "../src/app/api/sandbox/cases/[caseId]/draft/route";
import { GET as getRenderedValues } from "../src/app/api/sandbox/cases/[caseId]/rendered-values/route";
import { POST as postReview } from "../src/app/api/sandbox/cases/[caseId]/review/route";
import {
  DELETE as resetAllPortals,
  POST as resetPortal,
} from "../src/app/api/dev/reset/route";
import type {
  PortalDraftFields,
  PortalRunExpectedFields,
  PortalRunRenderFaultInjection,
  PortalRunRenderFaultRepair,
  PortalRunRelease,
  PortalRunSetup,
  PortalVariant,
  PortalView,
  PortalScalarField,
} from "../src/features/sandbox/contracts";
import {
  PORTAL_CONTROL_HEADER,
  isPortalControlAuthorized,
} from "../src/features/sandbox/portal-control";
import {
  PortalConflictError,
  PortalRunConflictError,
  SandboxPortalStore,
  sandboxPortalStore,
} from "../src/features/sandbox/store";
import {
  PortalInputError,
  validateReviewFields,
} from "../src/features/sandbox/validation";
const CONTROL_ENV = "CLAIMDONE_PORTAL_CONTROL_TOKEN";
const CONTROL_TOKEN = "portal-control-token-0123456789abcdef0123456789";
const FIXED_NOW = new Date("2026-07-15T09:00:00Z");
const ATTACHMENTS = [
  `model-${"1".repeat(32)}.jpg`,
  `model-${"2".repeat(32)}.png`,
  `model-${"3".repeat(32)}.jpg`,
] as const;
const EXPECTED_FIELDS: PortalRunExpectedFields = Object.freeze({
  attachments: ATTACHMENTS,
  claimantName: "  Demo Claimant  ",
  counterpartyKnown: "yes",
  incidentDate: "2026-07-14",
  incidentTime: "14:30:00",
  location: "  Berlin  ",
  narrative: "  Raw packet-bound narrative.  ",
  policyReference: "DEMO-42",
  vehicleRegistration: "DEMO-CD-1",
});
const SCALAR_FIELD_CASES = [
  { field: "incident_date", portalField: "incidentDate" },
  { field: "incident_time", portalField: "incidentTime" },
  { field: "location", portalField: "location" },
  { field: "claimant_name", portalField: "claimantName" },
  { field: "policy_reference", portalField: "policyReference" },
  { field: "vehicle_registration", portalField: "vehicleRegistration" },
  { field: "counterparty_known", portalField: "counterpartyKnown" },
  { field: "narrative", portalField: "narrative" },
] as const satisfies readonly {
  readonly field: PortalScalarField;
  readonly portalField: Exclude<keyof PortalDraftFields, "attachments">;
}[];

const singletonBindings: PortalRunRelease[] = [];
const singletonFaultRepairs: PortalRunRenderFaultRepair[] = [];

afterEach(() => {
  for (const repair of singletonFaultRepairs.splice(0).reverse()) {
    try {
      sandboxPortalStore.repairRenderFault(repair);
    } catch {
      // The test may not have armed the fault or may already have repaired it.
    }
  }
  for (const binding of singletonBindings.splice(0).reverse()) {
    try {
      sandboxPortalStore.abortRun(binding);
    } catch {
      try {
        sandboxPortalStore.releaseRun(binding);
      } catch {
        // The test may already have released or aborted this binding.
      }
    }
    try {
      sandboxPortalStore.delete(binding.caseId);
    } catch {
      // A failed assertion can leave an active binding; the primary failure is clearer.
    }
  }
  vi.unstubAllEnvs();
  vi.useRealTimers();
});

describe("packet-bound portal store authority", () => {
  it("pre-stages only ordered attachments and tombstones duplicate or stale run IDs", () => {
    const store = fixedStore();
    const command = setupCommand("case-control-setup", "run-control-setup", "A");
    const initial = store.setupRun(command);

    expect(initial.fields).toEqual({
      attachments: ATTACHMENTS,
      claimantName: "",
      counterpartyKnown: "",
      incidentDate: "",
      incidentTime: "",
      location: "",
      narrative: "",
      policyReference: "",
      vehicleRegistration: "",
    });
    expect(initial.auditCount).toBe(1);
    expect(JSON.stringify(initial)).not.toContain(command.runId);
    expect(JSON.stringify(initial)).not.toContain(EXPECTED_FIELDS.claimantName);
    expect(JSON.stringify(store.audit(command.caseId))).not.toContain(command.runId);

    expect(() => store.setupRun(command)).toThrow(PortalRunConflictError);
    expect(() =>
      store.setupRun(setupCommand(command.caseId, "run-control-other", "A")),
    ).toThrow(PortalRunConflictError);
    expect(() =>
      store.setupRun(setupCommand("case-control-foreign", command.runId, "B")),
    ).toThrow(PortalRunConflictError);

    store.abortRun(releaseBinding(command));
    expect(() =>
      store.setupRun(setupCommand("case-control-stale", command.runId, "A")),
    ).toThrow(PortalRunConflictError);

    const fresh = setupCommand(command.caseId, "run-control-fresh", "B");
    expect(store.setupRun(fresh).variant).toBe("B");
    store.abortRun(releaseBinding(fresh));
  });

  it("rejects every non-exact or repeated active-run write before mutation", () => {
    const store = fixedStore();
    const command = setupCommand("case-control-write", "run-control-write", "A");
    const initial = store.setupRun(command);
    const before = store.read(command.caseId, command.variant);
    const auditBefore = store.audit(command.caseId);
    const fourthAttachment = `model-${"4".repeat(32)}.jpg`;
    const invalidFields: unknown[] = [
      { ...EXPECTED_FIELDS, claimantName: EXPECTED_FIELDS.claimantName.trim() },
      { ...EXPECTED_FIELDS, attachments: [ATTACHMENTS[1], ATTACHMENTS[0], ATTACHMENTS[2]] },
      { ...EXPECTED_FIELDS, attachments: ATTACHMENTS.slice(0, 2) },
      { ...EXPECTED_FIELDS, attachments: [ATTACHMENTS[0], ATTACHMENTS[1], fourthAttachment] },
      { ...EXPECTED_FIELDS, attachments: [...ATTACHMENTS, fourthAttachment] },
      { ...EXPECTED_FIELDS, narrative: 42 },
      { ...EXPECTED_FIELDS, extra: "forbidden" },
    ];

    for (const fields of invalidFields) {
      expect(() =>
        store.saveDraft(
          command.caseId,
          command.variant,
          initial.version,
          fields as PortalDraftFields,
        ),
      ).toThrow(Error);
      expect(store.read(command.caseId, command.variant)).toEqual(before);
      expect(store.audit(command.caseId)).toEqual(auditBefore);
    }
    expect(() =>
      store.saveDraft(command.caseId, "B", initial.version, EXPECTED_FIELDS),
    ).toThrow(PortalConflictError);
    expect(() => store.advanceToReview(command.caseId, "A", initial.version)).toThrow(
      PortalRunConflictError,
    );
    expect(() => store.reset(command.caseId, "A", "complete")).toThrow(
      PortalRunConflictError,
    );
    expect(() => store.delete(command.caseId)).toThrow(PortalRunConflictError);
    expect(() => store.resetAll()).toThrow(PortalRunConflictError);
    expect(store.read(command.caseId, command.variant)).toEqual(before);

    const saved = store.saveDraft(
      command.caseId,
      command.variant,
      initial.version,
      EXPECTED_FIELDS,
    );
    const auditAfterSave = store.audit(command.caseId);
    expect(saved.fields).toEqual(EXPECTED_FIELDS);
    expect(() =>
      store.saveDraft(command.caseId, command.variant, saved.version, EXPECTED_FIELDS),
    ).toThrow(PortalRunConflictError);
    expect(() =>
      store.saveDraft(command.caseId, command.variant, initial.version, EXPECTED_FIELDS),
    ).toThrow(PortalConflictError);
    expect(store.read(command.caseId, command.variant)).toEqual(saved);
    expect(store.audit(command.caseId)).toEqual(auditAfterSave);

    const reviewed = store.advanceToReview(command.caseId, command.variant, saved.version);
    expect(() => store.abortRun(releaseBinding(command))).toThrow(PortalRunConflictError);
    store.releaseRun(releaseBinding(command));
    expect(store.read(command.caseId, command.variant)).toEqual(reviewed);
  });

  it("requires an exact active run, case, and variant binding for release or abort", () => {
    const store = fixedStore();
    const command = setupCommand("case-control-binding", "run-control-binding", "B");
    const initial = store.setupRun(command);
    const binding = releaseBinding(command);
    const foreignBindings: PortalRunRelease[] = [
      { ...binding, runId: "run-control-foreign" },
      { ...binding, caseId: "case-control-foreign" },
      { ...binding, variant: "A" },
    ];

    for (const foreign of foreignBindings) {
      expect(() => store.releaseRun(foreign)).toThrow(PortalRunConflictError);
      expect(() => store.abortRun(foreign)).toThrow(PortalRunConflictError);
      expect(store.read(command.caseId, command.variant)).toEqual(initial);
    }
    expect(() => store.releaseRun(binding)).toThrow(PortalRunConflictError);
    store.abortRun(binding);
  });

  it("counts Unicode code points at setup limits without normalizing raw strings", () => {
    const store = fixedStore();
    const exact = setupCommand("case-control-unicode", "run-control-unicode", "A");
    const exactExpected: PortalRunExpectedFields = {
      ...EXPECTED_FIELDS,
      claimantName: "😀".repeat(512),
    };
    const initial = store.setupRun({ ...exact, expectedFields: exactExpected });
    const saved = store.saveDraft(
      exact.caseId,
      exact.variant,
      initial.version,
      exactExpected,
    );
    expect(saved.fields.claimantName).toBe(exactExpected.claimantName);
    expect(saved.fields.location).toBe("  Berlin  ");
    store.abortRun(releaseBinding(exact));

    expect(() =>
      store.setupRun({
        ...setupCommand(
          "case-control-unicode-long",
          "run-control-unicode-long",
          "A",
        ),
        expectedFields: { ...EXPECTED_FIELDS, claimantName: "😀".repeat(513) },
      }),
    ).toThrow(PortalInputError);

    const earliest = setupCommand(
      "case-control-earliest-date",
      "run-control-earliest-date",
      "A",
    );
    const earliestFields: PortalRunExpectedFields = {
      ...EXPECTED_FIELDS,
      incidentDate: "0001-01-01",
    };
    const earliestInitial = store.setupRun({
      ...earliest,
      expectedFields: earliestFields,
    });
    expect(
      store.saveDraft(
        earliest.caseId,
        earliest.variant,
        earliestInitial.version,
        earliestFields,
      ).fields.incidentDate,
    ).toBe("0001-01-01");
    store.abortRun(releaseBinding(earliest));

    expect(() =>
      store.setupRun({
        ...setupCommand(
          "case-control-year-zero",
          "run-control-year-zero",
          "A",
        ),
        expectedFields: { ...EXPECTED_FIELDS, incidentDate: "0000-01-01" },
      }),
    ).toThrow(PortalInputError);
  });

  it("keeps packet-bound data semantics identical across variants A and B", () => {
    const store = fixedStore();
    const reviewed = (["A", "B"] as const).map((variant) => {
      const command = setupCommand(
        `case-control-layout-${variant.toLowerCase()}`,
        `run-control-layout-${variant.toLowerCase()}`,
        variant,
      );
      const initial = store.setupRun(command);
      const saved = store.saveDraft(
        command.caseId,
        variant,
        initial.version,
        EXPECTED_FIELDS,
      );
      const view = store.advanceToReview(command.caseId, variant, saved.version);
      store.releaseRun(releaseBinding(command));
      return view;
    });

    expect(reviewed[0]?.fields).toEqual(reviewed[1]?.fields);
    expect(reviewed[0]?.version).toBe(reviewed[1]?.version);
  });

  it.each(SCALAR_FIELD_CASES)(
    "faults and repairs only the rendered $field value",
    ({ field, portalField }) => {
      const store = fixedStore();
      const suffix = field.replaceAll("_", "-");
      const command = setupCommand(
        `case-control-fault-${suffix}`,
        `run-control-fault-${suffix}`,
        "A",
      );
      const initial = store.setupRun(command);
      const saved = store.saveDraft(
        command.caseId,
        command.variant,
        initial.version,
        EXPECTED_FIELDS,
      );
      const reviewed = store.advanceToReview(
        command.caseId,
        command.variant,
        saved.version,
      );
      const baseline = store.renderedValues(command.caseId, command.variant);
      const auditBefore = store.audit(command.caseId);
      const injection = renderFaultInjection(command, reviewed.version, field);

      store.injectRenderFault(injection);
      const firstFaulted = store.renderedValues(command.caseId, command.variant);
      const repeatedFaulted = store.renderedValues(command.caseId, command.variant);
      expect(firstFaulted).toEqual(repeatedFaulted);
      expect(firstFaulted.fields[portalField]).not.toBe(
        EXPECTED_FIELDS[portalField],
      );
      expect({
        ...firstFaulted.fields,
        [portalField]: EXPECTED_FIELDS[portalField],
      }).toEqual(EXPECTED_FIELDS);
      expect(firstFaulted.fields.attachments).toEqual(ATTACHMENTS);
      expect(validateReviewFields(firstFaulted.fields)).toEqual([]);
      expect(store.read(command.caseId, command.variant)).toEqual(reviewed);
      expect(store.audit(command.caseId)).toEqual(auditBefore);

      const repaired = store.repairRenderFault(renderFaultRepair(injection));
      expect(repaired.version).toBe(reviewed.version + 1);
      expect(repaired.fields).toEqual(reviewed.fields);
      expect(repaired.fields.attachments).toEqual(reviewed.fields.attachments);
      expect(store.renderedValues(command.caseId, command.variant)).toEqual({
        ...baseline,
        renderedAt: baseline.renderedAt,
        version: reviewed.version + 1,
      });
      const repairAudit = store.audit(command.caseId);
      expect(repairAudit).toHaveLength(auditBefore.length + 1);
      expect(JSON.stringify(repairAudit)).not.toContain(command.runId);
      expect(JSON.stringify(repairAudit)).not.toContain(field);
      expect(JSON.stringify(repairAudit)).not.toContain(EXPECTED_FIELDS.claimantName);
      store.releaseRun(releaseBinding(command));
    },
  );
});

describe("internal portal-control routes", () => {
  it("fails closed with an empty response for missing, wrong, or misconfigured tokens", async () => {
    const command = setupCommand("case-control-auth", "run-control-auth", "A");
    const cases = [
      { configured: undefined, supplied: CONTROL_TOKEN },
      { configured: CONTROL_TOKEN, supplied: undefined },
      { configured: CONTROL_TOKEN, supplied: `${CONTROL_TOKEN}-wrong` },
      { configured: "short", supplied: "short" },
      { configured: ` ${CONTROL_TOKEN}`, supplied: ` ${CONTROL_TOKEN}` },
    ] as const;

    for (const auth of cases) {
      vi.stubEnv(CONTROL_ENV, auth.configured);
      const response = await setupPortalRun(
        controlRequest("setup", command, auth.supplied),
      );
      const faultResponse = await injectRenderFault(
        controlRequest(
          "inject-render-fault",
          renderFaultInjection(command, 1, "claimant_name"),
          auth.supplied,
        ),
      );
      expect(response.status).toBe(404);
      expect(await response.text()).toBe("");
      expect(faultResponse.status).toBe(404);
      expect(await faultResponse.text()).toBe("");
      expect(sandboxPortalStore.audit(command.caseId)).toEqual([]);
      vi.unstubAllEnvs();
    }

    const request = controlRequest("setup", command, CONTROL_TOKEN);
    expect(isPortalControlAuthorized(request, () => CONTROL_TOKEN)).toBe(true);
    expect(isPortalControlAuthorized(request, () => undefined)).toBe(false);
    for (const token of ["a".repeat(32), "b".repeat(512)]) {
      expect(
        isPortalControlAuthorized(controlRequest("setup", command, token), () => token),
      ).toBe(true);
    }
    for (const token of ["a".repeat(31), "b".repeat(513), `c${"\u007f".repeat(31)}`]) {
      expect(
        isPortalControlAuthorized(controlRequest("setup", command, token), () => token),
      ).toBe(false);
    }
  });

  it("never reflects the control token from an invalid authenticated body", async () => {
    vi.stubEnv(CONTROL_ENV, CONTROL_TOKEN);
    const command = setupCommand("case-control-secret", "run-control-secret", "A");
    const response = await setupPortalRun(
      controlRequest(
        "setup",
        { ...command, controlToken: CONTROL_TOKEN },
        CONTROL_TOKEN,
      ),
    );
    const serialized = await response.text();

    expect(response.status).toBe(422);
    expect(serialized).not.toContain(CONTROL_TOKEN);
    const faultResponse = await injectRenderFault(
      controlRequest(
        "inject-render-fault",
        {
          ...renderFaultInjection(command, 1, "claimant_name"),
          replacementValue: CONTROL_TOKEN,
        },
        CONTROL_TOKEN,
      ),
    );
    expect(faultResponse.status).toBe(422);
    expect(await faultResponse.text()).not.toContain(CONTROL_TOKEN);
    expect(String(new PortalRunConflictError())).not.toContain(CONTROL_TOKEN);
    expect(sandboxPortalStore.audit(command.caseId)).toEqual([]);
  });

  it("blocks public review, fixture reset, delete, and reset-all during an active run", async () => {
    vi.stubEnv(CONTROL_ENV, CONTROL_TOKEN);
    const command = setupCommand("case-control-bypass", "run-control-bypass", "B");
    const binding = releaseBinding(command);
    const setupResponse = await setupPortalRun(
      controlRequest("setup", command, CONTROL_TOKEN),
    );
    expect(setupResponse.status).toBe(201);
    singletonBindings.push(binding);
    const context = routeContext(command.caseId);
    const before = (await setupResponse.json()) as PortalView;

    const prematureReview = await postReview(
      reviewRequest(command.caseId, command.variant, before.version),
      context,
    );
    const fixtureReset = await resetPortal(
      new Request("http://claimdone.local/api/dev/reset", {
        body: JSON.stringify({
          caseId: command.caseId,
          fixture: "complete",
          variant: command.variant,
        }),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      }),
    );
    const deleted = await deletePortal(
      new Request(`http://claimdone.local/api/sandbox/cases/${command.caseId}`, {
        method: "DELETE",
      }),
      context,
    );
    const resetAll = await resetAllPortals();
    const wrongRunRelease = await releasePortalRun(
      controlRequest("release", { ...binding, runId: "run-control-wrong" }, CONTROL_TOKEN),
    );
    const wrongVariantAbort = await abortPortalRun(
      controlRequest("abort", { ...binding, variant: "A" }, CONTROL_TOKEN),
    );

    for (const response of [
      prematureReview,
      fixtureReset,
      deleted,
      resetAll,
      wrongRunRelease,
      wrongVariantAbort,
    ]) {
      expect(response.status).toBe(409);
      expect(await response.json()).toMatchObject({
        error: { code: "PORTAL_RUN_CONFLICT" },
      });
    }
    const unchanged = await getPortal(portalRequest(command.caseId, "B"), context);
    expect(await unchanged.json()).toEqual(before);

    const aborted = await abortPortalRun(
      controlRequest("abort", binding, CONTROL_TOKEN),
    );
    expect(aborted.status).toBe(204);
    const missing = await getPortal(portalRequest(command.caseId, "B"), context);
    expect(missing.status).toBe(404);
  });

  it("binds one render fault and one versioned repair without mutating raw values", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-15T11:00:00Z"));
    vi.stubEnv(CONTROL_ENV, CONTROL_TOKEN);
    const command = setupCommand("case-control-repair", "run-control-repair", "A");
    const binding = releaseBinding(command);
    const context = routeContext(command.caseId);
    const setupResponse = await setupPortalRun(
      controlRequest("setup", command, CONTROL_TOKEN),
    );
    expect(setupResponse.status).toBe(201);
    singletonBindings.push(binding);
    const initial = (await setupResponse.json()) as PortalView;
    const saved = (await (
      await putDraft(
        draftRequest(command.caseId, command.variant, initial.version, EXPECTED_FIELDS),
        context,
      )
    ).json()) as PortalView;
    const reviewed = (await (
      await postReview(
        reviewRequest(command.caseId, command.variant, saved.version),
        context,
      )
    ).json()) as PortalView;
    const rawBefore = sandboxPortalStore.read(command.caseId, command.variant);
    const auditBefore = sandboxPortalStore.audit(command.caseId);
    const injection = renderFaultInjection(
      command,
      reviewed.version,
      "claimant_name",
    );
    const repair = renderFaultRepair(injection);

    const invalidField = await injectRenderFault(
      controlRequest(
        "inject-render-fault",
        { ...injection, field: "attachments" },
        CONTROL_TOKEN,
      ),
    );
    const callerValue = await injectRenderFault(
      controlRequest(
        "inject-render-fault",
        { ...injection, replacementValue: "forbidden" },
        CONTROL_TOKEN,
      ),
    );
    const invalidRepairField = await repairRenderFault(
      controlRequest(
        "repair-render-fault",
        { ...repair, field: "attachments" },
        CONTROL_TOKEN,
      ),
    );
    const callerRepairValue = await repairRenderFault(
      controlRequest(
        "repair-render-fault",
        { ...repair, replacementValue: "forbidden" },
        CONTROL_TOKEN,
      ),
    );
    expect(invalidField.status).toBe(422);
    expect(callerValue.status).toBe(422);
    expect(invalidRepairField.status).toBe(422);
    expect(callerRepairValue.status).toBe(422);
    expect(await callerValue.text()).not.toContain("forbidden");
    expect(await callerRepairValue.text()).not.toContain("forbidden");
    expect(sandboxPortalStore.read(command.caseId, command.variant)).toEqual(
      rawBefore,
    );
    expect(sandboxPortalStore.audit(command.caseId)).toEqual(auditBefore);

    const injected = await injectRenderFault(
      controlRequest("inject-render-fault", injection, CONTROL_TOKEN),
    );
    expect(injected.status).toBe(204);
    singletonFaultRepairs.push(repair);
    const publicDuringFault = await getPortal(
      portalRequest(command.caseId, command.variant),
      context,
    );
    expect(await publicDuringFault.json()).toEqual(rawBefore);
    expect(sandboxPortalStore.audit(command.caseId)).toEqual(auditBefore);

    vi.setSystemTime(new Date("2026-07-15T11:00:01Z"));
    const firstFaultedResponse = await getRenderedValues(
      portalRequest(command.caseId, command.variant, "/rendered-values"),
      context,
    );
    vi.setSystemTime(new Date("2026-07-15T11:00:02Z"));
    const secondFaultedResponse = await getRenderedValues(
      portalRequest(command.caseId, command.variant, "/rendered-values"),
      context,
    );
    const firstFaulted = (await firstFaultedResponse.json()) as Record<
      string,
      unknown
    >;
    const secondFaulted = (await secondFaultedResponse.json()) as Record<
      string,
      unknown
    >;
    expect(firstFaulted).toMatchObject({
      fields: {
        ...EXPECTED_FIELDS,
        claimantName: "Synthetic Claimant A",
      },
      renderedAt: "2026-07-15T11:00:01.000Z",
      version: reviewed.version,
    });
    expect(secondFaulted).toEqual({
      ...firstFaulted,
      renderedAt: "2026-07-15T11:00:02.000Z",
    });

    const blockedResponses = await Promise.all([
      injectRenderFault(
        controlRequest("inject-render-fault", injection, CONTROL_TOKEN),
      ),
      injectRenderFault(
        controlRequest(
          "inject-render-fault",
          { ...injection, field: "location" },
          CONTROL_TOKEN,
        ),
      ),
      injectRenderFault(
        controlRequest(
          "inject-render-fault",
          { ...injection, expectedVersion: reviewed.version - 1 },
          CONTROL_TOKEN,
        ),
      ),
      releasePortalRun(controlRequest("release", binding, CONTROL_TOKEN)),
      resetPortal(
        new Request("http://claimdone.local/api/dev/reset", {
          body: JSON.stringify({
            caseId: command.caseId,
            fixture: "empty",
            variant: command.variant,
          }),
          headers: { "Content-Type": "application/json" },
          method: "POST",
        }),
      ),
      deletePortal(
        new Request(`http://claimdone.local/api/sandbox/cases/${command.caseId}`, {
          method: "DELETE",
        }),
        context,
      ),
      resetAllPortals(),
    ]);
    for (const response of blockedResponses) {
      expect(response.status).toBe(409);
      const body = await response.text();
      expect(body).not.toContain(command.runId);
      expect(body).not.toContain(EXPECTED_FIELDS.claimantName);
      expect(body).not.toContain(CONTROL_TOKEN);
    }

    const unauthenticatedRepair = await repairRenderFault(
      controlRequest("repair-render-fault", repair),
    );
    expect(unauthenticatedRepair.status).toBe(404);
    expect(await unauthenticatedRepair.text()).toBe("");
    const wrongRepairs: PortalRunRenderFaultRepair[] = [
      { ...repair, runId: "run-control-wrong" },
      { ...repair, caseId: "case-control-wrong" },
      { ...repair, variant: "B" },
      { ...repair, field: "location" },
      { ...repair, expectedVersion: reviewed.version - 1 },
    ];
    for (const wrong of wrongRepairs) {
      const response = await repairRenderFault(
        controlRequest("repair-render-fault", wrong, CONTROL_TOKEN),
      );
      expect(response.status).toBe(409);
      const body = await response.text();
      expect(body).not.toContain(wrong.runId);
      expect(body).not.toContain(EXPECTED_FIELDS.claimantName);
    }
    expect(sandboxPortalStore.read(command.caseId, command.variant)).toEqual(
      rawBefore,
    );
    expect(sandboxPortalStore.audit(command.caseId)).toEqual(auditBefore);

    vi.setSystemTime(new Date("2026-07-15T11:00:03Z"));
    const repairedResponse = await repairRenderFault(
      controlRequest("repair-render-fault", repair, CONTROL_TOKEN),
    );
    expect(repairedResponse.status).toBe(200);
    const repaired = (await repairedResponse.json()) as PortalView;
    expect(repaired.version).toBe(reviewed.version + 1);
    expect(repaired.fields).toEqual(reviewed.fields);
    expect(repaired.fields.attachments).toEqual(ATTACHMENTS);
    expect(repaired.auditCount).toBe(reviewed.auditCount + 1);
    const repairAudit = sandboxPortalStore.audit(command.caseId);
    expect(repairAudit).toHaveLength(auditBefore.length + 1);
    const repairAuditJson = JSON.stringify(repairAudit);
    expect(repairAuditJson).not.toContain(command.runId);
    expect(repairAuditJson).not.toContain("claimant_name");
    expect(repairAuditJson).not.toContain(EXPECTED_FIELDS.claimantName);
    expect(repairAuditJson).not.toContain(EXPECTED_FIELDS.policyReference);

    vi.setSystemTime(new Date("2026-07-15T11:00:04Z"));
    const renderedAfterRepair = await getRenderedValues(
      portalRequest(command.caseId, command.variant, "/rendered-values"),
      context,
    );
    expect(await renderedAfterRepair.json()).toMatchObject({
      fields: EXPECTED_FIELDS,
      renderedAt: "2026-07-15T11:00:04.000Z",
      version: repaired.version,
    });

    const repairReplay = await repairRenderFault(
      controlRequest("repair-render-fault", repair, CONTROL_TOKEN),
    );
    const secondField = await injectRenderFault(
      controlRequest(
        "inject-render-fault",
        {
          ...injection,
          expectedVersion: repaired.version,
          field: "location",
        },
        CONTROL_TOKEN,
      ),
    );
    expect(repairReplay.status).toBe(409);
    expect(secondField.status).toBe(409);

    const released = await releasePortalRun(
      controlRequest("release", binding, CONTROL_TOKEN),
    );
    expect(released.status).toBe(204);
    const afterRelease = await getPortal(
      portalRequest(command.caseId, command.variant),
      context,
    );
    expect(await afterRelease.json()).toEqual(repaired);
  });

  it("runs exact setup, save, review, fresh reads, and non-mutating release", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-15T10:00:00Z"));
    vi.stubEnv(CONTROL_ENV, CONTROL_TOKEN);
    const command = setupCommand("case-control-success", "run-control-success", "A");
    const binding = releaseBinding(command);
    const context = routeContext(command.caseId);
    const setupResponse = await setupPortalRun(
      controlRequest("setup", command, CONTROL_TOKEN),
    );
    expect(setupResponse.status).toBe(201);
    singletonBindings.push(binding);
    const initial = (await setupResponse.json()) as PortalView;
    const initialJson = JSON.stringify(initial);
    expect(initialJson).not.toContain(command.runId);
    expect(initialJson).not.toContain(CONTROL_TOKEN);
    expect(initialJson).not.toContain(EXPECTED_FIELDS.claimantName);

    const saveResponse = await putDraft(
      draftRequest(command.caseId, command.variant, initial.version, EXPECTED_FIELDS),
      context,
    );
    expect(saveResponse.status).toBe(200);
    const saved = (await saveResponse.json()) as PortalView;
    const staleReplay = await putDraft(
      draftRequest(command.caseId, command.variant, initial.version, EXPECTED_FIELDS),
      context,
    );
    expect(staleReplay.status).toBe(409);

    const reviewResponse = await postReview(
      reviewRequest(command.caseId, command.variant, saved.version),
      context,
    );
    expect(reviewResponse.status).toBe(200);
    const reviewed = (await reviewResponse.json()) as PortalView;
    const auditBeforeReads = sandboxPortalStore.audit(command.caseId);

    vi.setSystemTime(new Date("2026-07-15T10:00:01Z"));
    const firstRendered = await getRenderedValues(
      portalRequest(command.caseId, command.variant, "/rendered-values"),
      context,
    );
    vi.setSystemTime(new Date("2026-07-15T10:00:02Z"));
    const secondRendered = await getRenderedValues(
      portalRequest(command.caseId, command.variant, "/rendered-values"),
      context,
    );
    const first = (await firstRendered.json()) as Record<string, unknown>;
    const second = (await secondRendered.json()) as Record<string, unknown>;
    expect(first).toMatchObject({
      fields: EXPECTED_FIELDS,
      renderedAt: "2026-07-15T10:00:01.000Z",
      state: "review",
    });
    expect(second).toEqual({ ...first, renderedAt: "2026-07-15T10:00:02.000Z" });
    expect(Object.keys(first).sort()).toEqual([
      "caseId",
      "contractVersion",
      "fields",
      "renderedAt",
      "state",
      "variant",
      "version",
    ]);
    expect(JSON.stringify(first)).not.toMatch(/runId|approval|receipt|audit/i);
    expect(sandboxPortalStore.audit(command.caseId)).toEqual(auditBeforeReads);

    const releaseResponse = await releasePortalRun(
      controlRequest("release", binding, CONTROL_TOKEN),
    );
    expect(releaseResponse.status).toBe(204);
    const postRelease = await getPortal(
      portalRequest(command.caseId, command.variant),
      context,
    );
    expect(await postRelease.json()).toEqual(reviewed);
    expect(sandboxPortalStore.audit(command.caseId)).toEqual(auditBeforeReads);

    const staleRelease = await releasePortalRun(
      controlRequest("release", binding, CONTROL_TOKEN),
    );
    expect(staleRelease.status).toBe(409);
  });
});

function fixedStore(): SandboxPortalStore {
  return new SandboxPortalStore(() => new Date(FIXED_NOW));
}

function setupCommand(
  caseId: string,
  runId: string,
  variant: PortalVariant,
): PortalRunSetup {
  return {
    caseId,
    contractVersion: "4.0.0",
    expectedFields: EXPECTED_FIELDS,
    runId,
    variant,
  };
}

function releaseBinding(command: PortalRunSetup): PortalRunRelease {
  return {
    caseId: command.caseId,
    contractVersion: "4.0.0",
    runId: command.runId,
    variant: command.variant,
  };
}

function renderFaultInjection(
  command: PortalRunSetup,
  expectedVersion: number,
  field: PortalScalarField,
): PortalRunRenderFaultInjection {
  return {
    caseId: command.caseId,
    contractVersion: "4.0.0",
    expectedVersion,
    field,
    runId: command.runId,
    variant: command.variant,
  };
}

function renderFaultRepair(
  injection: PortalRunRenderFaultInjection,
): PortalRunRenderFaultRepair {
  return { ...injection };
}

function controlRequest(
  action:
    | "setup"
    | "release"
    | "abort"
    | "inject-render-fault"
    | "repair-render-fault",
  body: unknown,
  token?: string,
): Request {
  const headers = new Headers({ "Content-Type": "application/json" });
  if (token !== undefined) headers.set(PORTAL_CONTROL_HEADER, token);
  return new Request(`http://claimdone.local/api/internal/portal-runs/${action}`, {
    body: JSON.stringify(body),
    headers,
    method: "POST",
  });
}

function routeContext(caseId: string): {
  readonly params: Promise<{ readonly caseId: string }>;
} {
  return { params: Promise.resolve({ caseId }) };
}

function portalRequest(
  caseId: string,
  variant: PortalVariant,
  suffix = "",
): Request {
  return new Request(
    `http://claimdone.local/api/sandbox/cases/${caseId}${suffix}?variant=${variant}`,
  );
}

function draftRequest(
  caseId: string,
  variant: PortalVariant,
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

function reviewRequest(
  caseId: string,
  variant: PortalVariant,
  expectedVersion: number,
): Request {
  return new Request(
    `http://claimdone.local/api/sandbox/cases/${caseId}/review?variant=${variant}`,
    {
      body: JSON.stringify({ expectedVersion }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
  );
}
