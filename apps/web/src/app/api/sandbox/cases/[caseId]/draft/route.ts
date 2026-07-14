import {
  portalErrorResponse,
  readSaveDraftRequest,
  variantFromRequest,
} from "../../../../../../features/sandbox/http";
import { sandboxPortalStore } from "../../../../../../features/sandbox/store";

interface RouteContext {
  readonly params: Promise<{ readonly caseId: string }>;
}

export async function PUT(request: Request, context: RouteContext): Promise<Response> {
  try {
    const { caseId } = await context.params;
    const variant = variantFromRequest(request);
    const body = await readSaveDraftRequest(request);
    return Response.json(
      sandboxPortalStore.saveDraft(caseId, variant, body.expectedVersion, body.fields),
    );
  } catch (error) {
    return portalErrorResponse(error);
  }
}
