import {
  portalErrorResponse,
  readPortalRunRenderFaultRepairRequest,
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
    const view = sandboxPortalStore.repairRenderFault(
      await readPortalRunRenderFaultRepairRequest(request),
    );
    return Response.json(view, {
      headers: { "Cache-Control": "no-store" },
      status: 200,
    });
  } catch (error) {
    return portalErrorResponse(error);
  }
}
