import {
  portalErrorResponse,
  readPortalRunSetupRequest,
} from "../../../../../features/sandbox/http";
import {
  isPortalControlAuthorized,
  portalControlDeniedResponse,
} from "../../../../../features/sandbox/portal-control";
import { sandboxPortalStore } from "../../../../../features/sandbox/store";

export const runtime = "nodejs";

export async function POST(request: Request): Promise<Response> {
  if (!isPortalControlAuthorized(request)) {
    return portalControlDeniedResponse();
  }
  try {
    const setup = await readPortalRunSetupRequest(request);
    return Response.json(sandboxPortalStore.setupRun(setup), {
      headers: { "Cache-Control": "no-store" },
      status: 201,
    });
  } catch (error) {
    return portalErrorResponse(error);
  }
}
