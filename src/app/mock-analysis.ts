import type {
  AnalyzeResponse,
  MissingField,
  StatementMode,
} from "@/lib/analysis-schema";

export type MockAnalysisInput = {
  photoCount: 1 | 2 | 3;
  statementMode: StatementMode;
  statementText: string;
  voiceFileName?: string;
  questionField?: MissingField;
  questionAnswer?: string;
};

export type MockAnalysisResult = AnalyzeResponse;

const locationPattern =
  /\b(berlin|street|road|avenue|intersection|junction|parking lot|car park|highway|motorway)\b/i;

function getLocation(input: MockAnalysisInput): string | null {
  const answer = input.questionAnswer?.trim();

  if (answer) {
    return answer;
  }

  if (input.statementMode === "voice" || !locationPattern.test(input.statementText)) {
    return null;
  }

  if (/alexanderplatz/i.test(input.statementText)) {
    return "Alexanderplatz, Berlin";
  }

  if (/\bberlin\b/i.test(input.statementText)) {
    return "Berlin";
  }

  return "Location provided in description";
}

export function runMockAnalysis(input: MockAnalysisInput): MockAnalysisResult {
  const location = getLocation(input);

  if (!location) {
    return {
      status: "needs_information",
      question: {
        field: "location",
        prompt: "Where did the accident happen?",
      },
    };
  }

  return {
    status: "ready",
    claim: {
      damage: "Visible front-left bumper damage",
      dateTime: "July 16, 2026 · 8:42 AM",
      location,
      photoCount: input.photoCount,
      status: "ready",
      whatHappened:
        input.statementMode === "text"
          ? input.statementText.trim()
          : "Accident description provided by voice memo.",
    },
  };
}
