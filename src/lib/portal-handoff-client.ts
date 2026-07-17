import type { Claim } from "./analysis-schema";
import {
  DemoPortalHandoffSuccessSchema,
  PortalHandoffErrorSchema,
  PortalHandoffSuccessSchema,
  type PortalHandoffErrorCode,
  type PortalHandoffSuccess,
  type DemoPortalHandoffSuccess,
} from "./portal-handoff-schema";

export class PortalHandoffRequestError extends Error {
  readonly code: PortalHandoffErrorCode | "invalid_response";

  constructor(code: PortalHandoffErrorCode | "invalid_response") {
    super("The portal handoff request failed");
    this.name = "PortalHandoffRequestError";
    this.code = code;
  }
}

async function requestPortalHandoffPayload(
  claim: Claim,
  endpoint: "/api/portal-handoff" | "/api/demo/portal-handoff",
): Promise<unknown> {
  const response = await fetch(endpoint, {
    body: JSON.stringify({ claim }),
    cache: "no-store",
    headers: { "content-type": "application/json" },
    method: "POST",
  });
  const payload: unknown = await response.json().catch(() => null);

  if (!response.ok) {
    const error = PortalHandoffErrorSchema.safeParse(payload);
    throw new PortalHandoffRequestError(
      error.success ? error.data.error.code : "invalid_response",
    );
  }

  return payload;
}

export async function requestPortalHandoff(
  claim: Claim,
): Promise<PortalHandoffSuccess> {
  const payload = await requestPortalHandoffPayload(
    claim,
    "/api/portal-handoff",
  );

  const result = PortalHandoffSuccessSchema.safeParse(payload);

  if (!result.success) {
    throw new PortalHandoffRequestError("invalid_response");
  }

  return result.data;
}

export async function requestDemoPortalHandoff(
  claim: Claim,
): Promise<DemoPortalHandoffSuccess> {
  const payload = await requestPortalHandoffPayload(
    claim,
    "/api/demo/portal-handoff",
  );
  const result = DemoPortalHandoffSuccessSchema.safeParse(payload);

  if (!result.success) {
    throw new PortalHandoffRequestError("invalid_response");
  }

  return result.data;
}
