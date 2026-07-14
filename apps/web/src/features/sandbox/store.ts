import type {
  PortalAuditEntry,
  PortalDraftFields,
  PortalFixture,
  PortalSession,
  PortalVariant,
  PortalView,
  RenderedPortalValues,
} from "./contracts";
import { clonePortalFields, fieldsForFixture } from "./fixtures";
import {
  assertCaseId,
  assertPortalTransition,
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

export class SandboxPortalStore {
  readonly #sessions = new Map<string, PortalSession>();

  constructor(private readonly now: () => Date = () => new Date()) {}

  getOrCreate(caseId: string, variant: PortalVariant): PortalView {
    assertCaseId(caseId);
    const existing = this.#sessions.get(caseId);
    if (existing) {
      if (existing.variant !== variant) {
        throw new PortalConflictError("This case already uses a different portal variant.");
      }
      return toView(existing);
    }
    const created = this.#fixtureSession(caseId, variant, "empty");
    this.#sessions.set(caseId, created);
    return toView(created);
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
    const updated = this.#withAudit(
      {
        ...current,
        fields: clonePortalFields(fields),
        version: current.version + 1,
      },
      "draft_saved",
      "portal_client",
    );
    this.#sessions.set(caseId, updated);
    return toView(updated);
  }

  advanceToReview(
    caseId: string,
    variant: PortalVariant,
    expectedVersion: number,
  ): PortalView {
    const current = this.#requireSession(caseId, variant);
    this.#assertVersion(current, expectedVersion);
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
    return toView(updated);
  }

  renderedValues(caseId: string, variant: PortalVariant): RenderedPortalValues {
    const current = this.#requireSession(caseId, variant);
    return {
      caseId,
      fields: clonePortalFields(current.fields),
      renderedAt: this.now().toISOString(),
      state: current.state,
    };
  }

  reset(caseId: string, variant: PortalVariant, fixture: PortalFixture): PortalView {
    assertCaseId(caseId);
    const reset = this.#fixtureSession(caseId, variant, fixture);
    this.#sessions.set(caseId, reset);
    return toView(reset);
  }

  delete(caseId: string): boolean {
    assertCaseId(caseId);
    return this.#sessions.delete(caseId);
  }

  resetAll(): number {
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

function toView(session: PortalSession): PortalView {
  return {
    auditCount: session.audit.length,
    caseId: session.caseId,
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
