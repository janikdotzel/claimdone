import { createOpenAIComputerUsePortalAutomator } from "@/server/computer-use-portal";
import { createPortalHandoffHandler } from "@/server/portal-handoff-handler";

export const runtime = "nodejs";

export const POST = createPortalHandoffHandler(
  createOpenAIComputerUsePortalAutomator,
  { includeReplay: true },
);
