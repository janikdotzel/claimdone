import { createOpenAIComputerUsePortalAutomator } from "@/server/computer-use-portal";
import { createPortalHandoffHandler } from "@/server/portal-handoff-handler";

export const runtime = "nodejs";
export const maxDuration = 60;

export const POST = createPortalHandoffHandler(
  createOpenAIComputerUsePortalAutomator,
);
