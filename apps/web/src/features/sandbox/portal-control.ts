import { timingSafeEqual } from "node:crypto";

export const PORTAL_CONTROL_HEADER = "X-ClaimDone-Portal-Control";

export type PortalControlTokenReader = () => string | undefined;

const CONTROL_TOKEN_PATTERN = /^[!-~]{32,512}$/;

export function readPortalControlToken(): string | undefined {
  return process.env.CLAIMDONE_PORTAL_CONTROL_TOKEN;
}

export function isPortalControlAuthorized(
  request: Request,
  readToken: PortalControlTokenReader = readPortalControlToken,
): boolean {
  const configured = readToken();
  const supplied = request.headers.get(PORTAL_CONTROL_HEADER);
  if (
    configured === undefined ||
    supplied === null ||
    !CONTROL_TOKEN_PATTERN.test(configured)
  ) {
    return false;
  }
  const configuredBytes = Buffer.from(configured, "utf8");
  const suppliedBytes = Buffer.from(supplied, "utf8");
  return (
    configuredBytes.length === suppliedBytes.length &&
    timingSafeEqual(configuredBytes, suppliedBytes)
  );
}

export function portalControlDeniedResponse(): Response {
  return new Response(null, {
    headers: { "Cache-Control": "no-store" },
    status: 404,
  });
}
