import {
  COUNTERPARTY_KNOWN_VALUES,
  PORTAL_FIELD_NAMES,
  PORTAL_FIXTURES,
  PORTAL_SCALAR_FIELDS,
  PORTAL_VARIANTS,
  type PortalDraftFields,
  type PortalFieldIssue,
  type PortalFieldName,
  type PortalFixture,
  type PortalRunExpectedFields,
  type PortalRunRenderFaultInjection,
  type PortalRunRenderFaultRepair,
  type PortalRunRelease,
  type PortalRunSetup,
  type PortalState,
  type PortalVariant,
} from "./contracts";

const CASE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const TIME_PATTERN = /^\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?$/;
const MODEL_ASSET_ID_PATTERN = /^model-[a-f0-9]{32}\.(?:jpg|png)$/;
const DEMO_ASSET_ID_PATTERN = /^asset-demo-[a-z0-9]+(?:-[a-z0-9]+)*$/;
const MAX_ASSET_ID_LENGTH = 128;
type PortalTextFieldName = Exclude<
  PortalFieldName,
  "attachments" | "counterpartyKnown"
>;
const PORTAL_TEXT_MAX_CODE_POINTS = {
  claimantName: 512,
  incidentDate: 10,
  incidentTime: 21,
  location: 512,
  narrative: 4_000,
  policyReference: 512,
  vehicleRegistration: 512,
} as const satisfies Readonly<Record<PortalTextFieldName, number>>;

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

export function assertPortalRunId(runId: string): void {
  if (!CASE_ID_PATTERN.test(runId)) {
    throw new PortalInputError("The portal run ID is invalid.");
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
    (key): key is PortalTextFieldName =>
      key !== "attachments" && key !== "counterpartyKnown",
  );
  for (const key of stringKeys) {
    const fieldValue = value[key];
    if (typeof fieldValue !== "string") {
      throw new PortalInputError(`${key} must be a string.`);
    }
    if (Array.from(fieldValue).length > PORTAL_TEXT_MAX_CODE_POINTS[key]) {
      throw new PortalInputError(
        `${key} must contain at most ${PORTAL_TEXT_MAX_CODE_POINTS[key]} characters.`,
      );
    }
  }
  if (!isCounterpartyKnown(value.counterpartyKnown)) {
    throw new PortalInputError("counterpartyKnown has an unknown value.");
  }
  if (
    !Array.isArray(value.attachments) ||
    value.attachments.length > 3 ||
    value.attachments.some((assetId) => !isServerAssetId(assetId)) ||
    new Set(value.attachments).size !== value.attachments.length
  ) {
    throw new PortalInputError(
      "attachments must contain at most three unique approved server asset IDs.",
    );
  }

  return {
    attachments: [...value.attachments],
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

export function parsePortalRunExpectedFields(
  value: unknown,
): PortalRunExpectedFields {
  const fields = parsePortalFields(value);
  const issues = validateReviewFields(fields);
  if (
    issues.length ||
    fields.counterpartyKnown === "" ||
    fields.attachments.length !== 3
  ) {
    throw new PortalInputError("Expected portal fields must be complete.", issues);
  }
  return {
    ...fields,
    attachments: [
      fields.attachments[0] as string,
      fields.attachments[1] as string,
      fields.attachments[2] as string,
    ],
    counterpartyKnown: fields.counterpartyKnown,
  };
}

export function parsePortalRunSetup(value: unknown): PortalRunSetup {
  const body = requireClosedObject(value, [
    "caseId",
    "contractVersion",
    "expectedFields",
    "runId",
    "variant",
  ]);
  requireContractVersion(body.contractVersion);
  if (typeof body.caseId !== "string") {
    throw new PortalInputError("caseId must be a string.");
  }
  if (typeof body.runId !== "string") {
    throw new PortalInputError("runId must be a string.");
  }
  assertCaseId(body.caseId);
  assertPortalRunId(body.runId);
  return {
    caseId: body.caseId,
    contractVersion: "4.0.0",
    expectedFields: parsePortalRunExpectedFields(body.expectedFields),
    runId: body.runId,
    variant: parsePortalVariant(body.variant),
  };
}

export function parsePortalRunRelease(value: unknown): PortalRunRelease {
  const body = requireClosedObject(value, [
    "caseId",
    "contractVersion",
    "runId",
    "variant",
  ]);
  requireContractVersion(body.contractVersion);
  if (typeof body.caseId !== "string") {
    throw new PortalInputError("caseId must be a string.");
  }
  if (typeof body.runId !== "string") {
    throw new PortalInputError("runId must be a string.");
  }
  assertCaseId(body.caseId);
  assertPortalRunId(body.runId);
  return {
    caseId: body.caseId,
    contractVersion: "4.0.0",
    runId: body.runId,
    variant: parsePortalVariant(body.variant),
  };
}

export function parsePortalRunRenderFaultInjection(
  value: unknown,
): PortalRunRenderFaultInjection {
  return parsePortalRunRenderFaultCommand(value);
}

export function parsePortalRunRenderFaultRepair(
  value: unknown,
): PortalRunRenderFaultRepair {
  return parsePortalRunRenderFaultCommand(value);
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
  if (
    year === undefined ||
    month === undefined ||
    day === undefined ||
    year < 1
  ) {
    return false;
  }
  const candidate = new Date(0);
  candidate.setUTCHours(0, 0, 0, 0);
  candidate.setUTCFullYear(year, month - 1, day);
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

function isServerAssetId(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length <= MAX_ASSET_ID_LENGTH &&
    (MODEL_ASSET_ID_PATTERN.test(value) || DEMO_ASSET_ID_PATTERN.test(value))
  );
}

function isCounterpartyKnown(
  value: unknown,
): value is PortalDraftFields["counterpartyKnown"] {
  return value === "" || COUNTERPARTY_KNOWN_VALUES.some((candidate) => candidate === value);
}

function requireContractVersion(value: unknown): void {
  if (value !== "4.0.0") {
    throw new PortalInputError("The portal contract version is unsupported.");
  }
}

function parsePortalRunRenderFaultCommand(
  value: unknown,
): PortalRunRenderFaultInjection {
  const body = requireClosedObject(value, [
    "caseId",
    "contractVersion",
    "expectedVersion",
    "field",
    "runId",
    "variant",
  ]);
  requireContractVersion(body.contractVersion);
  if (typeof body.caseId !== "string") {
    throw new PortalInputError("caseId must be a string.");
  }
  if (typeof body.runId !== "string") {
    throw new PortalInputError("runId must be a string.");
  }
  if (
    typeof body.field !== "string" ||
    !(PORTAL_SCALAR_FIELDS as readonly string[]).includes(body.field)
  ) {
    throw new PortalInputError("Render fault field must be one known scalar field.");
  }
  assertCaseId(body.caseId);
  assertPortalRunId(body.runId);
  return {
    caseId: body.caseId,
    contractVersion: "4.0.0",
    expectedVersion: parseExpectedVersion(body.expectedVersion),
    field: body.field as PortalRunRenderFaultInjection["field"],
    runId: body.runId,
    variant: parsePortalVariant(body.variant),
  };
}

function requireClosedObject(
  value: unknown,
  expectedKeys: readonly string[],
): Record<string, unknown> {
  if (!isRecord(value)) {
    throw new PortalInputError("Request body must be an object.");
  }
  const actual = Object.keys(value).sort();
  const expected = [...expectedKeys].sort();
  if (
    actual.length !== expected.length ||
    actual.some((key, index) => key !== expected[index])
  ) {
    throw new PortalInputError("Request body contains unknown or missing fields.");
  }
  return value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
