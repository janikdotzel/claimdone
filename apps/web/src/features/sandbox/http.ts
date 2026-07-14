import type {
  PortalDraftFields,
  PortalErrorBody,
  PortalFixture,
  PortalVariant,
} from "./contracts";
import { PortalConflictError } from "./store";
import {
  assertCaseId,
  parseExpectedVersion,
  parsePortalFields,
  parsePortalFixture,
  parsePortalVariant,
  PortalInputError,
} from "./validation";

export interface SaveDraftRequest {
  readonly expectedVersion: number;
  readonly fields: PortalDraftFields;
}

export interface ReviewRequest {
  readonly expectedVersion: number;
}

export interface ResetRequest {
  readonly caseId: string;
  readonly fixture: PortalFixture;
  readonly variant: PortalVariant;
}

export async function readSaveDraftRequest(request: Request): Promise<SaveDraftRequest> {
  const body = await readObject(request, ["expectedVersion", "fields"]);
  return {
    expectedVersion: parseExpectedVersion(body.expectedVersion),
    fields: parsePortalFields(body.fields),
  };
}

export async function readReviewRequest(request: Request): Promise<ReviewRequest> {
  const body = await readObject(request, ["expectedVersion"]);
  return { expectedVersion: parseExpectedVersion(body.expectedVersion) };
}

export async function readResetRequest(request: Request): Promise<ResetRequest> {
  const body = await readObject(request, ["caseId", "fixture", "variant"]);
  if (typeof body.caseId !== "string") {
    throw new PortalInputError("caseId must be a string.");
  }
  assertCaseId(body.caseId);
  return {
    caseId: body.caseId,
    fixture: parsePortalFixture(body.fixture),
    variant: parsePortalVariant(body.variant),
  };
}

export function variantFromRequest(request: Request): PortalVariant {
  const variant = new URL(request.url).searchParams.get("variant");
  return parsePortalVariant(variant);
}

export function portalErrorResponse(error: unknown): Response {
  if (error instanceof PortalInputError || error instanceof PortalConflictError) {
    const body: PortalErrorBody = {
      error: {
        code: error.code,
        fieldErrors: error.fieldErrors,
        message: error.message,
      },
    };
    return Response.json(body, { status: error.status });
  }
  const body: PortalErrorBody = {
    error: {
      code: "PORTAL_INTERNAL_ERROR",
      fieldErrors: [],
      message: "The sandbox portal could not complete the request.",
    },
  };
  return Response.json(body, { status: 500 });
}

async function readObject(
  request: Request,
  expectedKeys: readonly string[],
): Promise<Record<string, unknown>> {
  let value: unknown;
  try {
    value = await request.json();
  } catch {
    throw new PortalInputError("Request body must be valid JSON.");
  }
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new PortalInputError("Request body must be an object.");
  }
  const body = value as Record<string, unknown>;
  const actual = Object.keys(body).sort();
  const expected = [...expectedKeys].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    throw new PortalInputError("Request body contains unknown or missing fields.");
  }
  return body;
}
