import {
  AnalyzeErrorSchema,
  AnalyzeResponseSchema,
  type AnalyzeErrorCode,
  type AnalyzeResponse,
  type MissingField,
  type StatementMode,
} from "./analysis-schema";
import {
  DemoAnalyzeResponseSchema,
  type DemoAnalyzeResponse,
} from "./demo-analysis-schema";

export type ClientEvidencePhoto = {
  file?: File;
  src: string;
};

export type ClientAnalysisInput = {
  photos: readonly ClientEvidencePhoto[];
  statementMode: StatementMode;
  statementText: string;
  voiceFile: File | null;
  questionField?: MissingField;
  questionAnswer?: string;
};

export class AnalysisRequestError extends Error {
  readonly code: AnalyzeErrorCode | "invalid_response";

  constructor(code: AnalyzeErrorCode | "invalid_response") {
    super("The analysis request failed");
    this.name = "AnalysisRequestError";
    this.code = code;
  }
}

async function getPhotoFile(
  photo: ClientEvidencePhoto,
  index: number,
): Promise<File> {
  if (photo.file) {
    return photo.file;
  }

  const response = await fetch(photo.src);

  if (!response.ok) {
    throw new AnalysisRequestError("analysis_failed");
  }

  const blob = await response.blob();
  const type = blob.type === "image/png" ? "image/png" : "image/jpeg";
  const extension = type === "image/png" ? "png" : "jpg";

  return new File([blob], `sample-photo-${index + 1}.${extension}`, { type });
}

async function requestAnalysisPayload(
  input: ClientAnalysisInput,
  endpoint: "/api/analyze" | "/api/demo/analyze",
): Promise<unknown> {
  const formData = new FormData();
  const photos = await Promise.all(input.photos.map(getPhotoFile));

  photos.forEach((photo) => formData.append("photos", photo));
  formData.set("statementMode", input.statementMode);

  if (input.statementMode === "text") {
    formData.set("statementText", input.statementText);
  } else if (input.voiceFile) {
    formData.set("voiceMemo", input.voiceFile);
  }

  if (input.questionField && input.questionAnswer) {
    formData.set("questionField", input.questionField);
    formData.set("questionAnswer", input.questionAnswer);
  }

  const response = await fetch(endpoint, {
    body: formData,
    cache: "no-store",
    method: "POST",
  });
  const payload: unknown = await response.json().catch(() => null);

  if (!response.ok) {
    const error = AnalyzeErrorSchema.safeParse(payload);
    throw new AnalysisRequestError(error.success ? error.data.error.code : "invalid_response");
  }

  return payload;
}

export async function requestAnalysis(
  input: ClientAnalysisInput,
): Promise<AnalyzeResponse> {
  const payload = await requestAnalysisPayload(input, "/api/analyze");

  const result = AnalyzeResponseSchema.safeParse(payload);

  if (!result.success) {
    throw new AnalysisRequestError("invalid_response");
  }

  return result.data;
}

export async function requestDemoAnalysis(
  input: ClientAnalysisInput,
): Promise<DemoAnalyzeResponse> {
  const payload = await requestAnalysisPayload(input, "/api/demo/analyze");
  const result = DemoAnalyzeResponseSchema.safeParse(payload);

  if (!result.success) {
    throw new AnalysisRequestError("invalid_response");
  }

  return result.data;
}
