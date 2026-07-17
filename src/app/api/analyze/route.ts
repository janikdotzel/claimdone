import { createAnalyzeHandler } from "@/server/analyze-handler";
import { createOpenAIClaimAnalyzer } from "@/server/openai-claim-analyzer";

export const runtime = "nodejs";

export const POST = createAnalyzeHandler(createOpenAIClaimAnalyzer);
