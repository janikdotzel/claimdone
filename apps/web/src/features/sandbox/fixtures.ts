import type { PortalDraftFields, PortalFixture } from "./contracts";

export const EMPTY_PORTAL_FIELDS: PortalDraftFields = Object.freeze({
  attachments: Object.freeze([]),
  claimantName: "",
  counterpartyKnown: "",
  incidentDate: "",
  incidentTime: "",
  location: "",
  narrative: "",
  policyReference: "",
  vehicleRegistration: "",
});

export const COMPLETE_PORTAL_FIELDS: PortalDraftFields = Object.freeze({
  attachments: Object.freeze([
    "asset-demo-rear-overview",
    "asset-demo-rear-detail",
    "asset-demo-context",
  ]),
  claimantName: "Demo Claimant",
  counterpartyKnown: "yes",
  incidentDate: "2026-07-14",
  incidentTime: "14:30:00",
  location: "Berlin",
  narrative:
    "Another vehicle contacted the rear of the demo vehicle. Visible rear-bumper damage is shown in the supplied images.",
  policyReference: "DEMO-42",
  vehicleRegistration: "DEMO-CD-1",
});

export function fieldsForFixture(fixture: PortalFixture): PortalDraftFields {
  return clonePortalFields(fixture === "complete" ? COMPLETE_PORTAL_FIELDS : EMPTY_PORTAL_FIELDS);
}

export function clonePortalFields(fields: PortalDraftFields): PortalDraftFields {
  return {
    ...fields,
    attachments: [...fields.attachments],
  };
}
