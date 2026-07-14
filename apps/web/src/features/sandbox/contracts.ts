export const PORTAL_VARIANTS = ["A", "B"] as const;
export const PORTAL_STATES = ["draft", "review", "human_approved", "receipt"] as const;
export const PORTAL_FIXTURES = ["empty", "complete"] as const;

export type PortalVariant = (typeof PORTAL_VARIANTS)[number];
export type PortalState = (typeof PORTAL_STATES)[number];
export type PortalFixture = (typeof PORTAL_FIXTURES)[number];
export type CounterpartyKnown = "" | "yes" | "no" | "unknown";

export interface PortalFields {
  readonly incidentDate: string;
  readonly incidentTime: string;
  readonly location: string;
  readonly claimantName: string;
  readonly policyReference: string;
  readonly vehicleRegistration: string;
  readonly counterpartyKnown: CounterpartyKnown;
  readonly narrative: string;
  readonly attachments: readonly string[];
}

export type PortalFieldName = Exclude<keyof PortalFields, "attachments"> | "attachments";

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
  readonly fields: PortalFields;
  readonly audit: readonly PortalAuditEntry[];
  readonly updatedAt: string;
}

export interface PortalView {
  readonly caseId: string;
  readonly variant: PortalVariant;
  readonly state: PortalState;
  readonly version: number;
  readonly fields: PortalFields;
  readonly auditCount: number;
  readonly updatedAt: string;
}

export interface RenderedPortalValues {
  readonly caseId: string;
  readonly state: PortalState;
  readonly fields: PortalFields;
  readonly renderedAt: string;
}

export interface PortalErrorBody {
  readonly error: Readonly<{
    code: string;
    message: string;
    fieldErrors: readonly PortalFieldIssue[];
  }>;
}
