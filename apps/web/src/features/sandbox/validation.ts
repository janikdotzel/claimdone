import {
  COUNTERPARTY_KNOWN_VALUES,
  PORTAL_FIELD_NAMES,
  PORTAL_FIXTURES,
  PORTAL_VARIANTS,
  type PortalDraftFields,
  type PortalFieldIssue,
  type PortalFixture,
  type PortalState,
  type PortalVariant,
} from "./contracts";

const CASE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const TIME_PATTERN = /^\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?$/;
export class PortalInputError extends Error {
  readonly code = "PORTAL_INPUT_INVALID";
  readonly status = 422;

  constructor(
    message: string,
    readonly fieldErrors: readonly PortalFieldIssue[] = [],
  ) {
    super(message);
    this.name = "PortalInputError";
  }
}

export function assertCaseId(caseId: string): void {
  if (!CASE_ID_PATTERN.test(caseId)) {
    throw new PortalInputError("The sandbox case ID is invalid.");
  }
}

export function parsePortalVariant(value: unknown): PortalVariant {
  if (typeof value === "string" && (PORTAL_VARIANTS as readonly string[]).includes(value)) {
    return value as PortalVariant;
  }
  throw new PortalInputError("Portal variant must be A or B.");
}

export function parsePortalFixture(value: unknown): PortalFixture {
  if (typeof value === "string" && (PORTAL_FIXTURES as readonly string[]).includes(value)) {
    return value as PortalFixture;
  }
  throw new PortalInputError("Unknown sandbox fixture.");
}

export function parseExpectedVersion(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 1) {
    throw new PortalInputError("expectedVersion must be a positive integer.");
  }
  return value;
}

export function parsePortalFields(value: unknown): PortalDraftFields {
  if (!isRecord(value)) {
    throw new PortalInputError("fields must be an object.");
  }
  const actualKeys = Object.keys(value).sort();
  const expectedKeys = [...PORTAL_FIELD_NAMES].sort();
  if (actualKeys.length !== expectedKeys.length || actualKeys.some((key, index) => key !== expectedKeys[index])) {
    throw new PortalInputError("fields must contain exactly the known sandbox fields.");
  }

  const stringKeys = PORTAL_FIELD_NAMES.filter(
    (key) => key !== "attachments" && key !== "counterpartyKnown",
  );
  for (const key of stringKeys) {
    if (typeof value[key] !== "string") {
      throw new PortalInputError(`${key} must be a string.`);
    }
  }
  if (!isCounterpartyKnown(value.counterpartyKnown)) {
    throw new PortalInputError("counterpartyKnown has an unknown value.");
  }
  if (
    !Array.isArray(value.attachments) ||
    value.attachments.length > 3 ||
    value.attachments.some((name) => typeof name !== "string")
  ) {
    throw new PortalInputError("attachments must contain at most three file names.");
  }

  return {
    attachments: value.attachments.map(sanitizeAttachmentName),
    claimantName: value.claimantName as string,
    counterpartyKnown: value.counterpartyKnown,
    incidentDate: value.incidentDate as string,
    incidentTime: value.incidentTime as string,
    location: value.location as string,
    narrative: value.narrative as string,
    policyReference: value.policyReference as string,
    vehicleRegistration: value.vehicleRegistration as string,
  };
}

export function validateReviewFields(fields: PortalDraftFields): readonly PortalFieldIssue[] {
  const issues: PortalFieldIssue[] = [];
  requireText(issues, "incidentDate", fields.incidentDate, "Incident date is required.");
  requireText(issues, "incidentTime", fields.incidentTime, "Incident time is required.");
  requireText(issues, "location", fields.location, "Location is required.");
  requireText(issues, "claimantName", fields.claimantName, "Claimant name is required.");
  requireText(issues, "policyReference", fields.policyReference, "Policy reference is required.");
  requireText(
    issues,
    "vehicleRegistration",
    fields.vehicleRegistration,
    "Vehicle registration is required.",
  );
  requireText(issues, "narrative", fields.narrative, "Incident narrative is required.");

  if (fields.incidentDate && !isValidDate(fields.incidentDate)) {
    issues.push({
      code: "PORTAL_DATE_INVALID",
      field: "incidentDate",
      message: "Use a valid date in YYYY-MM-DD format.",
    });
  }
  if (fields.incidentTime && !isValidTime(fields.incidentTime)) {
    issues.push({
      code: "PORTAL_TIME_INVALID",
      field: "incidentTime",
      message: "Use a valid time including seconds.",
    });
  }
  if (!fields.counterpartyKnown) {
    issues.push({
      code: "PORTAL_REQUIRED",
      field: "counterpartyKnown",
      message: "Select whether the counterparty is known.",
    });
  }
  if (fields.attachments.length !== 3) {
    issues.push({
      code: "PORTAL_ATTACHMENT_COUNT",
      field: "attachments",
      message: "Exactly three approved images are required.",
    });
  }
  return issues;
}

export function assertPortalTransition(current: PortalState, target: PortalState): void {
  if (current !== "draft" || target !== "review") {
    throw new PortalInputError(`Invalid sandbox portal transition: ${current} -> ${target}`);
  }
}

function requireText(
  issues: PortalFieldIssue[],
  field: Exclude<keyof PortalDraftFields, "attachments" | "counterpartyKnown">,
  value: string,
  message: string,
): void {
  if (!value.trim()) {
    issues.push({ code: "PORTAL_REQUIRED", field, message });
  }
}

function isValidDate(value: string): boolean {
  if (!DATE_PATTERN.test(value)) return false;
  const [year, month, day] = value.split("-").map(Number);
  const candidate = new Date(Date.UTC(year ?? 0, (month ?? 1) - 1, day ?? 0));
  return (
    candidate.getUTCFullYear() === year &&
    candidate.getUTCMonth() + 1 === month &&
    candidate.getUTCDate() === day
  );
}

function isValidTime(value: string): boolean {
  if (!TIME_PATTERN.test(value)) return false;
  const [hours, minutes, secondsWithFraction] = value.split(":");
  const seconds = Number(secondsWithFraction);
  return Number(hours) <= 23 && Number(minutes) <= 59 && seconds < 60;
}

function sanitizeAttachmentName(value: string): string {
  const basename = value.split(/[\\/]/).at(-1) ?? "image";
  return basename.replace(/[\u0000-\u001F\u007F]/g, "").slice(0, 128) || "image";
}

function isCounterpartyKnown(
  value: unknown,
): value is PortalDraftFields["counterpartyKnown"] {
  return value === "" || COUNTERPARTY_KNOWN_VALUES.some((candidate) => candidate === value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
