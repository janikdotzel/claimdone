import { portalErrorResponse, variantFromRequest } from "../../../../../features/sandbox/http";
import { sandboxPortalStore } from "../../../../../features/sandbox/store";

interface RouteContext {
  readonly params: Promise<{ readonly caseId: string }>;
}

export async function GET(request: Request, context: RouteContext): Promise<Response> {
  try {
    const { caseId } = await context.params;
    const view = sandboxPortalStore.getOrCreate(caseId, variantFromRequest(request));
    return Response.json(view, {
      headers: { "Cache-Control": "no-store" },
      status: 200,
    });
  } catch (error) {
    return portalErrorResponse(error);
  }
}

export async function DELETE(_request: Request, context: RouteContext): Promise<Response> {
  try {
    const { caseId } = await context.params;
    sandboxPortalStore.delete(caseId);
    return new Response(null, { status: 204 });
  } catch (error) {
    return portalErrorResponse(error);
  }
}
