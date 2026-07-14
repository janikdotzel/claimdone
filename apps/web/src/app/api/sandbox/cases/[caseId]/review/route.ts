import {
  portalErrorResponse,
  readReviewRequest,
  variantFromRequest,
} from "../../../../../../features/sandbox/http";
import { sandboxPortalStore } from "../../../../../../features/sandbox/store";

interface RouteContext {
  readonly params: Promise<{ readonly caseId: string }>;
}

export async function POST(request: Request, context: RouteContext): Promise<Response> {
  try {
    const { caseId } = await context.params;
    const variant = variantFromRequest(request);
    const body = await readReviewRequest(request);
    return Response.json(
      sandboxPortalStore.advanceToReview(caseId, variant, body.expectedVersion),
    );
  } catch (error) {
    return portalErrorResponse(error);
  }
}
