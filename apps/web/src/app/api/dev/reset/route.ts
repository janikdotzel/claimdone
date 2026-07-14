import { portalErrorResponse, readResetRequest } from "../../../../features/sandbox/http";
import { sandboxPortalStore } from "../../../../features/sandbox/store";

export async function POST(request: Request): Promise<Response> {
  try {
    const body = await readResetRequest(request);
    return Response.json(
      sandboxPortalStore.reset(body.caseId, body.variant, body.fixture),
    );
  } catch (error) {
    return portalErrorResponse(error);
  }
}

export async function DELETE(): Promise<Response> {
  return Response.json({ deletedCount: sandboxPortalStore.resetAll() });
}
