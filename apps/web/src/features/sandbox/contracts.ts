import type {
  CounterpartyKnown,
  EvalInput,
  PortalDraftFields as CanonicalPortalDraftFields,
  PortalRunExpectedFields,
  PortalRunRenderFaultInjection,
  PortalRunRenderFaultRepair,
  PortalRunRelease,
  PortalRunSetup,
  PortalSessionView,
  PortalState,
  RequiredClaimField,
  RenderedPortalSnapshot,
} from "../../../../../contracts/generated/claimdone";

export type {
  CounterpartyKnown,
  PortalRunExpectedFields,
  PortalRunRenderFaultInjection,
  PortalRunRenderFaultRepair,
  PortalRunRelease,
  PortalRunSetup,
  PortalState,
};

export type PortalVariant = EvalInput["portalVariant"];
export type PortalScalarField = PortalRunRenderFaultInjection["field"];

export const PORTAL_VARIANTS = ["A", "B"] as const satisfies readonly PortalVariant[];
export const PORTAL_STATES = [
  "draft",
  "review",
  "human_approved",
  "receipt",
] as const satisfies readonly PortalState[];
export const COUNTERPARTY_KNOWN_VALUES = [
  "yes",
  "no",
  "unknown",
] as const satisfies readonly CounterpartyKnown[];
export const PORTAL_FIXTURES = ["empty", "complete"] as const;
export const PORTAL_SCALAR_FIELDS = [
  "incident_date",
  "incident_time",
  "location",
  "claimant_name",
  "policy_reference",
  "vehicle_registration",
  "counterparty_known",
  "narrative",
] as const satisfies readonly PortalScalarField[];

export type PortalFixture = (typeof PORTAL_FIXTURES)[number];

/**
 * Editable UI state. Unlike a valid ClaimData payload, a draft may contain
 * empty fields and fewer than three approved server or synthetic demo asset IDs.
 */
export type PortalDraftFields = CanonicalPortalDraftFields;

export type PortalFieldName = keyof PortalDraftFields;

export const REQUIRED_CLAIM_FIELD_TO_PORTAL_FIELD = {
  attachments: "attachments",
  claimant_name: "claimantName",
  counterparty_known: "counterpartyKnown",
  incident_date: "incidentDate",
  incident_time: "incidentTime",
  location: "location",
  narrative: "narrative",
  policy_reference: "policyReference",
  vehicle_registration: "vehicleRegistration",
} as const satisfies Readonly<Record<RequiredClaimField, PortalFieldName>>;

export const PORTAL_FIELD_NAMES = Object.values(
  REQUIRED_CLAIM_FIELD_TO_PORTAL_FIELD,
) satisfies readonly PortalFieldName[];

export interface PortalFieldIssue {
  readonly field: PortalFieldName;
  readonly code: string;
  readonly message: string;
}

export interface PortalAuditEntry {
  readonly sequence: number;
  readonly action:
    | "fixture_reset"
    | "run_setup"
    | "draft_saved"
    | "review_started"
    | "render_fault_repaired";
  readonly actor: "developer" | "portal_control" | "portal_client";
  readonly occurredAt: string;
  readonly summary: Readonly<{
    attachmentCount: number;
    filledFieldCount: number;
    variant: PortalVariant;
  }>;
}

export interface PortalSession {
  readonly caseId: string;
  readonly variant: PortalVariant;
  readonly state: PortalSessionView["state"];
  readonly version: number;
  readonly fields: PortalDraftFields;
  readonly audit: readonly PortalAuditEntry[];
  readonly updatedAt: string;
}

export type PortalView = Omit<PortalSessionView, "auditCount"> &
  Readonly<{ readonly auditCount: number }>;

export type RenderedPortalValues = RenderedPortalSnapshot;

export interface PortalErrorBody {
  readonly error: Readonly<{
    code: string;
    message: string;
    fieldErrors: readonly PortalFieldIssue[];
  }>;
}
