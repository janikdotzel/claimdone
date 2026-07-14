import type {
  ClaimData,
  CounterpartyKnown,
  EvalInput,
  PortalState,
  RequiredClaimField,
} from "../../../../../contracts/generated/claimdone";

export type { CounterpartyKnown, PortalState };

export type PortalVariant = EvalInput["portalVariant"];

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

export type PortalFixture = (typeof PORTAL_FIXTURES)[number];

type PortalTextClaimFields = Pick<
  ClaimData,
  | "claimantName"
  | "incidentDate"
  | "incidentTime"
  | "location"
  | "narrative"
  | "policyReference"
  | "vehicleRegistration"
>;

/**
 * Editable UI state. Unlike a valid ClaimData payload, a draft may contain
 * empty fields and fewer than three demo attachment names.
 */
export type PortalDraftFields = Readonly<
  {
    readonly [Field in keyof PortalTextClaimFields]: NonNullable<
      PortalTextClaimFields[Field]
    >;
  } & {
    /** Display-only sandbox names; never evidence identity or media authority. */
    readonly attachments: ReadonlyArray<ClaimData["attachments"][number]>;
    readonly counterpartyKnown: "" | ClaimData["counterpartyKnown"];
  }
>;

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
  readonly action: "fixture_reset" | "draft_saved" | "review_started";
  readonly actor: "developer" | "human";
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
  readonly state: PortalState;
  readonly version: number;
  readonly fields: PortalDraftFields;
  readonly audit: readonly PortalAuditEntry[];
  readonly updatedAt: string;
}

export interface PortalView {
  readonly caseId: string;
  readonly variant: PortalVariant;
  readonly state: PortalState;
  readonly version: number;
  readonly fields: PortalDraftFields;
  readonly auditCount: number;
  readonly updatedAt: string;
}

export interface RenderedPortalValues {
  readonly caseId: string;
  readonly state: PortalState;
  readonly fields: PortalDraftFields;
  readonly renderedAt: string;
}

export interface PortalErrorBody {
  readonly error: Readonly<{
    code: string;
    message: string;
    fieldErrors: readonly PortalFieldIssue[];
  }>;
}
