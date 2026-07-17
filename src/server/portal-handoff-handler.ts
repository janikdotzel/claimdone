import {
  DemoPortalHandoffSuccessSchema,
  PortalHandoffRequestSchema,
  PortalHandoffSuccessSchema,
  type PortalHandoffError,
  type PortalHandoffErrorCode,
} from "@/lib/portal-handoff-schema";
import { getMissingClaimDetailFields } from "@/lib/analysis-schema";

import {
  PortalAutomationNotConfiguredError,
  PortalAutomationSafetyError,
  type PortalAutomator,
} from "./computer-use-portal";

type CreatePortalAutomator = () => PortalAutomator;

function errorResponse(code: PortalHandoffErrorCode, status: number): Response {
  const messages: Record<PortalHandoffErrorCode, string> = {
    automation_failed: "We couldn’t prepare the insurer portal.",
    invalid_input: "Check the claim details and try again.",
    not_configured: "Portal preparation is not configured for this demo.",
    safety_blocked: "Portal preparation stopped for a safety check.",
  };
  const body: PortalHandoffError = {
    error: {
      code,
      message: messages[code],
    },
  };

  return Response.json(body, {
    headers: { "cache-control": "no-store" },
    status,
  });
}

export function createPortalHandoffHandler(
  createAutomator: CreatePortalAutomator,
  options: { includeReplay?: boolean } = {},
): (request: Request) => Promise<Response> {
  return async (request) => {
    const contentType = request.headers
      .get("content-type")
      ?.split(";", 1)[0]
      ?.trim()
      .toLowerCase();

    if (contentType !== "application/json") {
      return errorResponse("invalid_input", 400);
    }

    const payload: unknown = await request.json().catch(() => null);
    const parsed = PortalHandoffRequestSchema.safeParse(payload);

    if (!parsed.success) {
      return errorResponse("invalid_input", 400);
    }

    if (getMissingClaimDetailFields(parsed.data.claim).length > 0) {
      return errorResponse("invalid_input", 400);
    }

    try {
      const automationResult = await createAutomator().prepare(
        parsed.data.claim,
        { captureReplay: options.includeReplay === true },
      );
      const result = options.includeReplay
        ? DemoPortalHandoffSuccessSchema.parse(automationResult)
        : PortalHandoffSuccessSchema.parse({
            screenshotDataUrl: automationResult.screenshotDataUrl,
            status: automationResult.status,
            submitted: automationResult.submitted,
          });
      return Response.json(result, {
        headers: { "cache-control": "no-store" },
      });
    } catch (error) {
      if (error instanceof PortalAutomationNotConfiguredError) {
        return errorResponse("not_configured", 503);
      }

      if (error instanceof PortalAutomationSafetyError) {
        return errorResponse("safety_blocked", 409);
      }

      return errorResponse("automation_failed", 502);
    }
  };
}
