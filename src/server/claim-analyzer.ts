import type {
  AnalyzeResponse,
  MissingField,
  StatementMode,
} from "@/lib/analysis-schema";
import type { AgentActivity } from "@/lib/demo-analysis-schema";

export type AnalysisInput = {
  photos: File[];
  statement: string;
  statementMode: StatementMode;
  followUp?: {
    field: MissingField;
    answer: string;
  };
};

export type ClaimAnalysisEnvelope = {
  activity: AgentActivity;
  result: AnalyzeResponse;
};

export type ClaimAnalysis = AnalyzeResponse | ClaimAnalysisEnvelope;

export interface ClaimAnalyzer {
  transcribe(audio: File): Promise<string>;
  analyze(input: AnalysisInput): Promise<ClaimAnalysis>;
}

export class AnalyzerNotConfiguredError extends Error {
  constructor() {
    super("Claim analysis is not configured");
    this.name = "AnalyzerNotConfiguredError";
  }
}
