import {
  portalErrorResponse,
  readPortalRunReleaseRequest,
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
    sandboxPortalStore.abortRun(await readPortalRunReleaseRequest(request));
    return new Response(null, {
      headers: { "Cache-Control": "no-store" },
      status: 204,
    });
  } catch (error) {
    return portalErrorResponse(error);
  }
}
