import type {
  PortalAuditEntry,
  PortalDraftFields,
  PortalFixture,
  PortalRunExpectedFields,
  PortalRunRenderFaultInjection,
  PortalRunRenderFaultRepair,
  PortalRunRelease,
  PortalRunSetup,
  PortalScalarField,
  PortalSession,
  PortalVariant,
  PortalView,
  RenderedPortalValues,
} from "./contracts";
import { clonePortalFields, EMPTY_PORTAL_FIELDS, fieldsForFixture } from "./fixtures";
import {
  assertCaseId,
  assertPortalTransition,
  parsePortalFields,
  parsePortalRunRelease,
  parsePortalRunRenderFaultInjection,
  parsePortalRunRenderFaultRepair,
  parsePortalRunSetup,
  PortalInputError,
  validateReviewFields,
} from "./validation";

export class PortalConflictError extends Error {
  readonly code = "PORTAL_VERSION_CONFLICT";
  readonly status = 409;
  readonly fieldErrors = [];

  constructor(message: string) {
    super(message);
    this.name = "PortalConflictError";
  }
}

export class PortalNotFoundError extends Error {
  readonly code = "PORTAL_NOT_FOUND";
  readonly status = 404;
  readonly fieldErrors = [];

  constructor() {
    super("Create or reset the sandbox case before reading it.");
    this.name = "PortalNotFoundError";
  }
}

export class PortalStateConflictError extends Error {
  readonly code = "PORTAL_STATE_CONFLICT";
  readonly status = 409;
  readonly fieldErrors = [];

  constructor(message: string) {
    super(message);
    this.name = "PortalStateConflictError";
  }
}

export class PortalRunConflictError extends Error {
  readonly code = "PORTAL_RUN_CONFLICT";
  readonly status = 409;
  readonly fieldErrors = [];

  constructor() {
    super("The packet-bound portal run rejected this operation.");
    this.name = "PortalRunConflictError";
  }
}

interface ActivePortalRunAuthority {
  readonly runId: string;
  readonly caseId: string;
  readonly variant: PortalVariant;
  readonly expectedFields: PortalRunExpectedFields;
  readonly authorizedSaveVersion?: number;
  readonly reviewedVersion?: number;
  readonly faultField?: PortalScalarField;
  readonly faultVersion?: number;
  readonly faultActive?: boolean;
  readonly repairedVersion?: number;
}

export class SandboxPortalStore {
  readonly #sessions = new Map<string, PortalSession>();
  readonly #activeRuns = new Map<string, ActivePortalRunAuthority>();
  readonly #usedRunIds = new Set<string>();

  constructor(private readonly now: () => Date = () => new Date()) {}

  read(caseId: string, variant: PortalVariant): PortalView {
    return toView(this.#requireReadableSession(caseId, variant));
  }

  setupRun(value: PortalRunSetup): PortalView {
    const setup = parsePortalRunSetup(value);
    if (
      this.#usedRunIds.has(setup.runId) ||
      this.#sessions.has(setup.caseId) ||
      this.#activeRuns.has(setup.caseId)
    ) {
      throw new PortalRunConflictError();
    }
    const base: PortalSession = {
      audit: [],
      caseId: setup.caseId,
      fields: {
        ...EMPTY_PORTAL_FIELDS,
        attachments: [...setup.expectedFields.attachments],
      },
      state: "draft",
      updatedAt: this.now().toISOString(),
      variant: setup.variant,
      version: 1,
    };
    const session = this.#withAudit(base, "run_setup", "portal_control", false);
    const authority: ActivePortalRunAuthority = {
      caseId: setup.caseId,
      expectedFields: cloneExpectedFields(setup.expectedFields),
      runId: setup.runId,
      variant: setup.variant,
    };
    this.#sessions.set(setup.caseId, session);
    this.#activeRuns.set(setup.caseId, authority);
    this.#usedRunIds.add(setup.runId);
    return toView(session);
  }

  releaseRun(value: PortalRunRelease): void {
    const release = parsePortalRunRelease(value);
    const authority = this.#requireActiveRun(release);
    const session = this.#sessions.get(release.caseId);
    if (
      !session ||
      session.variant !== release.variant ||
      session.state !== "review" ||
      authority.reviewedVersion !== session.version ||
      !portalFieldsEqual(session.fields, authority.expectedFields) ||
      authority.faultActive === true ||
      (authority.faultField !== undefined &&
        authority.repairedVersion !== session.version)
    ) {
      throw new PortalRunConflictError();
    }
    this.#activeRuns.delete(release.caseId);
  }

  abortRun(value: PortalRunRelease): void {
    const release = parsePortalRunRelease(value);
    this.#requireActiveRun(release);
    const session = this.#sessions.get(release.caseId);
    if (!session || session.variant !== release.variant || session.state !== "draft") {
      throw new PortalRunConflictError();
    }
    this.#activeRuns.delete(release.caseId);
    this.#sessions.delete(release.caseId);
  }

  injectRenderFault(value: PortalRunRenderFaultInjection): void {
    const command = parsePortalRunRenderFaultInjection(value);
    const authority = this.#requireActiveRun(command);
    const session = this.#sessions.get(command.caseId);
    if (
      !session ||
      session.variant !== command.variant ||
      session.state !== "review" ||
      session.version !== command.expectedVersion ||
      authority.reviewedVersion !== command.expectedVersion ||
      !portalFieldsEqual(session.fields, authority.expectedFields) ||
      authority.faultField !== undefined ||
      authority.faultVersion !== undefined ||
      authority.repairedVersion !== undefined
    ) {
      throw new PortalRunConflictError();
    }
    this.#activeRuns.set(command.caseId, {
      ...authority,
      faultActive: true,
      faultField: command.field,
      faultVersion: command.expectedVersion,
    });
  }

  repairRenderFault(value: PortalRunRenderFaultRepair): PortalView {
    const command = parsePortalRunRenderFaultRepair(value);
    const authority = this.#requireActiveRun(command);
    const session = this.#sessions.get(command.caseId);
    if (
      !session ||
      session.variant !== command.variant ||
      session.state !== "review" ||
      session.version !== command.expectedVersion ||
      authority.reviewedVersion !== command.expectedVersion ||
      !portalFieldsEqual(session.fields, authority.expectedFields) ||
      authority.faultActive !== true ||
      authority.faultField !== command.field ||
      authority.faultVersion !== command.expectedVersion ||
      authority.repairedVersion !== undefined
    ) {
      throw new PortalRunConflictError();
    }
    const updated = this.#withAudit(
      {
        ...session,
        fields: clonePortalFields(session.fields),
        version: session.version + 1,
      },
      "render_fault_repaired",
      "portal_control",
    );
    this.#sessions.set(command.caseId, updated);
    this.#activeRuns.set(command.caseId, {
      ...authority,
      faultActive: false,
      repairedVersion: updated.version,
      reviewedVersion: updated.version,
    });
    return toView(updated);
  }

  saveDraft(
    caseId: string,
    variant: PortalVariant,
    expectedVersion: number,
    fields: PortalDraftFields,
  ): PortalView {
    const current = this.#requireSession(caseId, variant);
    this.#assertVersion(current, expectedVersion);
    if (current.state !== "draft") {
      throw new PortalConflictError("Only a draft portal session can be edited.");
    }
    const validatedFields = parsePortalFields(fields);
    const active = this.#activeRuns.get(caseId);
    if (active) {
      if (
        active.variant !== variant ||
        active.authorizedSaveVersion !== undefined ||
        !portalFieldsEqual(validatedFields, active.expectedFields)
      ) {
        throw new PortalRunConflictError();
      }
    }
    const updated = this.#withAudit(
      {
        ...current,
        fields: clonePortalFields(validatedFields),
        version: current.version + 1,
      },
      "draft_saved",
      "portal_client",
    );
    this.#sessions.set(caseId, updated);
    if (active) {
      this.#activeRuns.set(caseId, {
        ...active,
        authorizedSaveVersion: updated.version,
      });
    }
    return toView(updated);
  }

  advanceToReview(
    caseId: string,
    variant: PortalVariant,
    expectedVersion: number,
  ): PortalView {
    const current = this.#requireSession(caseId, variant);
    this.#assertVersion(current, expectedVersion);
    const active = this.#activeRuns.get(caseId);
    if (
      active &&
      (active.variant !== variant ||
        active.authorizedSaveVersion !== current.version ||
        !portalFieldsEqual(current.fields, active.expectedFields))
    ) {
      throw new PortalRunConflictError();
    }
    assertPortalTransition(current.state, "review");
    const issues = validateReviewFields(current.fields);
    if (issues.length) {
      throw new PortalInputError("The sandbox form is incomplete.", issues);
    }
    const updated = this.#withAudit(
      {
        ...current,
        state: "review",
        version: current.version + 1,
      },
      "review_started",
      "portal_client",
    );
    this.#sessions.set(caseId, updated);
    if (active) {
      this.#activeRuns.set(caseId, {
        ...active,
        reviewedVersion: updated.version,
      });
    }
    return toView(updated);
  }

  renderedValues(caseId: string, variant: PortalVariant): RenderedPortalValues {
    const current = this.#requireReadableSession(caseId, variant);
    if (current.state !== "review") {
      throw new PortalStateConflictError(
        "Rendered portal values are available only from the review state.",
      );
    }
    let fields = clonePortalFields(current.fields);
    const active = this.#activeRuns.get(caseId);
    if (active?.faultActive === true) {
      if (
        active.variant !== variant ||
        active.reviewedVersion !== current.version ||
        active.faultVersion !== current.version ||
        active.faultField === undefined ||
        !portalFieldsEqual(current.fields, active.expectedFields)
      ) {
        throw new PortalRunConflictError();
      }
      fields = withSyntheticRenderFault(fields, active.faultField);
    }
    return {
      caseId,
      contractVersion: "4.0.0",
      fields,
      renderedAt: this.now().toISOString(),
      state: current.state,
      variant: current.variant,
      version: current.version,
    };
  }

  reset(caseId: string, variant: PortalVariant, fixture: PortalFixture): PortalView {
    assertCaseId(caseId);
    this.#assertNoActiveRun(caseId);
    const reset = this.#fixtureSession(caseId, variant, fixture);
    this.#sessions.set(caseId, reset);
    return toView(reset);
  }

  delete(caseId: string): boolean {
    assertCaseId(caseId);
    this.#assertNoActiveRun(caseId);
    return this.#sessions.delete(caseId);
  }

  resetAll(): number {
    if (this.#activeRuns.size) {
      throw new PortalRunConflictError();
    }
    const deletedCount = this.#sessions.size;
    this.#sessions.clear();
    return deletedCount;
  }

  audit(caseId: string): readonly PortalAuditEntry[] {
    const session = this.#sessions.get(caseId);
    if (!session) return [];
    return session.audit.map((entry) => ({ ...entry, summary: { ...entry.summary } }));
  }

  #requireSession(caseId: string, variant: PortalVariant): PortalSession {
    assertCaseId(caseId);
    const session = this.#sessions.get(caseId);
    if (!session) {
      throw new PortalConflictError("Create or reset the sandbox case before mutating it.");
    }
    if (session.variant !== variant) {
      throw new PortalConflictError("This case already uses a different portal variant.");
    }
    return session;
  }

  #requireActiveRun(release: PortalRunRelease): ActivePortalRunAuthority {
    const authority = this.#activeRuns.get(release.caseId);
    if (
      !authority ||
      authority.runId !== release.runId ||
      authority.caseId !== release.caseId ||
      authority.variant !== release.variant
    ) {
      throw new PortalRunConflictError();
    }
    return authority;
  }

  #assertNoActiveRun(caseId: string): void {
    if (this.#activeRuns.has(caseId)) {
      throw new PortalRunConflictError();
    }
  }

  #requireReadableSession(caseId: string, variant: PortalVariant): PortalSession {
    assertCaseId(caseId);
    const session = this.#sessions.get(caseId);
    if (!session) {
      throw new PortalNotFoundError();
    }
    if (session.variant !== variant) {
      throw new PortalConflictError("This case already uses a different portal variant.");
    }
    return session;
  }

  #assertVersion(session: PortalSession, expectedVersion: number): void {
    if (session.version !== expectedVersion) {
      throw new PortalConflictError("The sandbox case changed since it was loaded.");
    }
  }

  #fixtureSession(
    caseId: string,
    variant: PortalVariant,
    fixture: PortalFixture,
  ): PortalSession {
    const base: PortalSession = {
      audit: [],
      caseId,
      fields: fieldsForFixture(fixture),
      state: "draft",
      updatedAt: this.now().toISOString(),
      variant,
      version: 1,
    };
    return this.#withAudit(base, "fixture_reset", "developer", false);
  }

  #withAudit(
    session: PortalSession,
    action: PortalAuditEntry["action"],
    actor: PortalAuditEntry["actor"],
    refreshTimestamp = true,
  ): PortalSession {
    const occurredAt = this.now().toISOString();
    const textValues = Object.entries(session.fields).filter(
      ([key, value]) => key !== "attachments" && typeof value === "string" && value.trim(),
    );
    const entry: PortalAuditEntry = {
      action,
      actor,
      occurredAt,
      sequence: session.audit.length + 1,
      summary: {
        attachmentCount: session.fields.attachments.length,
        filledFieldCount: textValues.length,
        variant: session.variant,
      },
    };
    return {
      ...session,
      audit: [...session.audit, entry],
      updatedAt: refreshTimestamp ? occurredAt : session.updatedAt,
    };
  }
}

function cloneExpectedFields(
  fields: PortalRunExpectedFields,
): PortalRunExpectedFields {
  return {
    ...fields,
    attachments: [...fields.attachments] as readonly [string, string, string],
  };
}

function portalFieldsEqual(
  actual: PortalDraftFields,
  expected: PortalRunExpectedFields,
): boolean {
  return (
    actual.incidentDate === expected.incidentDate &&
    actual.incidentTime === expected.incidentTime &&
    actual.location === expected.location &&
    actual.claimantName === expected.claimantName &&
    actual.policyReference === expected.policyReference &&
    actual.vehicleRegistration === expected.vehicleRegistration &&
    actual.counterpartyKnown === expected.counterpartyKnown &&
    actual.narrative === expected.narrative &&
    actual.attachments.length === expected.attachments.length &&
    actual.attachments.every(
      (attachmentId, index) => attachmentId === expected.attachments[index],
    )
  );
}

function withSyntheticRenderFault(
  fields: PortalDraftFields,
  field: PortalScalarField,
): PortalDraftFields {
  switch (field) {
    case "incident_date":
      return {
        ...fields,
        incidentDate: alternate(fields.incidentDate, "2000-01-01", "2000-01-02"),
      };
    case "incident_time":
      return {
        ...fields,
        incidentTime: alternate(fields.incidentTime, "00:00:00", "00:00:01"),
      };
    case "location":
      return {
        ...fields,
        location: alternate(
          fields.location,
          "Synthetic render location A",
          "Synthetic render location B",
        ),
      };
    case "claimant_name":
      return {
        ...fields,
        claimantName: alternate(
          fields.claimantName,
          "Synthetic Claimant A",
          "Synthetic Claimant B",
        ),
      };
    case "policy_reference":
      return {
        ...fields,
        policyReference: alternate(
          fields.policyReference,
          "SYNTHETIC-POLICY-A",
          "SYNTHETIC-POLICY-B",
        ),
      };
    case "vehicle_registration":
      return {
        ...fields,
        vehicleRegistration: alternate(
          fields.vehicleRegistration,
          "SYNTHETIC-REG-A",
          "SYNTHETIC-REG-B",
        ),
      };
    case "counterparty_known":
      return {
        ...fields,
        counterpartyKnown: fields.counterpartyKnown === "unknown" ? "no" : "unknown",
      };
    case "narrative":
      return {
        ...fields,
        narrative: alternate(
          fields.narrative,
          "Synthetic render mismatch A.",
          "Synthetic render mismatch B.",
        ),
      };
    default:
      return assertNever(field);
  }
}

function alternate(current: string, first: string, second: string): string {
  return current === first ? second : first;
}

function assertNever(value: never): never {
  void value;
  throw new PortalRunConflictError();
}

function toView(session: PortalSession): PortalView {
  return {
    auditCount: session.audit.length,
    caseId: session.caseId,
    contractVersion: "4.0.0",
    fields: clonePortalFields(session.fields),
    state: session.state,
    updatedAt: session.updatedAt,
    variant: session.variant,
    version: session.version,
  };
}

const globalStore = globalThis as typeof globalThis & {
  __claimDoneSandboxPortalStore?: SandboxPortalStore;
};

export const sandboxPortalStore =
  globalStore.__claimDoneSandboxPortalStore ?? new SandboxPortalStore();
globalStore.__claimDoneSandboxPortalStore = sandboxPortalStore;
