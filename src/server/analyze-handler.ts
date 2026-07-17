import { z } from "zod";

import {
  AnalyzeResponseSchema,
  MissingFieldSchema,
  StatementModeSchema,
  type AnalyzeError,
  type AnalyzeErrorCode,
  type AnalyzeResponse,
  type MissingField,
} from "@/lib/analysis-schema";
import { DemoAnalyzeResponseSchema } from "@/lib/demo-analysis-schema";

import {
  AnalyzerNotConfiguredError,
  type ClaimAnalyzer,
} from "./claim-analyzer";

const MAX_PHOTO_BYTES = 8 * 1024 * 1024;
const MAX_VOICE_BYTES = 10 * 1024 * 1024;
const PHOTO_TYPES = new Set(["image/jpeg", "image/png"]);
const VOICE_TYPES = new Set([
  "audio/mp4",
  "audio/x-m4a",
  "audio/mpeg",
  "audio/wav",
  "audio/x-wav",
  "audio/webm",
]);
const GENERIC_FILE_TYPES = new Set(["", "application/octet-stream"]);
const VOICE_EXTENSIONS = /\.(m4a|mp3|wav|webm)$/i;
const ALLOWED_FIELDS = new Set([
  "photos",
  "statementMode",
  "statementText",
  "voiceMemo",
  "questionField",
  "questionAnswer",
]);

const TextSchema = z.string().trim().min(1).max(1500);

type FollowUp = {
  field: MissingField;
  answer: string;
};

type ParsedRequest =
  | {
      photos: File[];
      statementMode: "text";
      statementText: string;
      followUp?: FollowUp;
    }
  | {
      photos: File[];
      statementMode: "voice";
      voiceMemo: File;
      followUp?: FollowUp;
    };

class RequestValidationError extends Error {
  constructor(
    readonly code: Extract<
      AnalyzeErrorCode,
      "invalid_input" | "unsupported_media" | "payload_too_large"
    >,
    readonly status: 400 | 413 | 415,
  ) {
    super(code);
    this.name = "RequestValidationError";
  }
}

function isFile(value: FormDataEntryValue): value is File {
  return (
    typeof value !== "string" &&
    typeof value.arrayBuffer === "function" &&
    typeof value.name === "string"
  );
}

function fail(
  code: RequestValidationError["code"],
  status: RequestValidationError["status"],
): never {
  throw new RequestValidationError(code, status);
}

function parseFollowUp(formData: FormData): FollowUp | undefined {
  const fieldEntries = formData.getAll("questionField");
  const answerEntries = formData.getAll("questionAnswer");

  if (fieldEntries.length > 1 || answerEntries.length > 1) {
    fail("invalid_input", 400);
  }

  if (fieldEntries.length === 0 && answerEntries.length === 0) {
    return undefined;
  }

  const field = MissingFieldSchema.safeParse(fieldEntries[0]);
  const answer = TextSchema.safeParse(answerEntries[0]);

  if (!field.success || !answer.success) {
    fail("invalid_input", 400);
  }

  return { answer: answer.data, field: field.data };
}

function validatePhotos(formData: FormData): File[] {
  const entries = formData.getAll("photos");

  if (entries.length < 1 || entries.length > 3 || entries.some((entry) => !isFile(entry))) {
    fail("invalid_input", 400);
  }

  const photos = entries.filter(isFile);

  if (photos.some((photo) => !PHOTO_TYPES.has(photo.type))) {
    fail("unsupported_media", 415);
  }

  if (photos.some((photo) => photo.size > MAX_PHOTO_BYTES)) {
    fail("payload_too_large", 413);
  }

  return photos;
}

function validateVoiceMemo(entry: FormDataEntryValue | null): File {
  if (!entry || !isFile(entry)) {
    fail("invalid_input", 400);
  }

  const supportedType =
    VOICE_TYPES.has(entry.type) ||
    (GENERIC_FILE_TYPES.has(entry.type) && VOICE_EXTENSIONS.test(entry.name));

  if (!supportedType) {
    fail("unsupported_media", 415);
  }

  if (entry.size > MAX_VOICE_BYTES) {
    fail("payload_too_large", 413);
  }

  return entry;
}

function parseRequest(formData: FormData): ParsedRequest {
  for (const key of formData.keys()) {
    if (!ALLOWED_FIELDS.has(key)) {
      fail("invalid_input", 400);
    }
  }

  const modeEntries = formData.getAll("statementMode");

  if (modeEntries.length !== 1) {
    fail("invalid_input", 400);
  }

  const mode = StatementModeSchema.safeParse(modeEntries[0]);

  if (!mode.success) {
    fail("invalid_input", 400);
  }

  const photos = validatePhotos(formData);
  const followUp = parseFollowUp(formData);

  if (mode.data === "text") {
    const textEntries = formData.getAll("statementText");

    if (textEntries.length !== 1 || formData.getAll("voiceMemo").length !== 0) {
      fail("invalid_input", 400);
    }

    const statementText = TextSchema.safeParse(textEntries[0]);

    if (!statementText.success) {
      fail("invalid_input", 400);
    }

    return {
      ...(followUp ? { followUp } : {}),
      photos,
      statementMode: "text",
      statementText: statementText.data,
    };
  }

  if (
    formData.getAll("statementText").length !== 0 ||
    formData.getAll("voiceMemo").length !== 1
  ) {
    fail("invalid_input", 400);
  }

  return {
    ...(followUp ? { followUp } : {}),
    photos,
    statementMode: "voice",
    voiceMemo: validateVoiceMemo(formData.get("voiceMemo")),
  };
}

function errorResponse(code: AnalyzeErrorCode, status: number): Response {
  const messages: Record<AnalyzeErrorCode, string> = {
    analysis_failed: "We couldn’t analyze these photos.",
    invalid_input: "Check the photos and description and try again.",
    not_configured: "Analysis is not configured for this demo.",
    payload_too_large: "One or more files are too large.",
    unsupported_media: "Use the supported photo or audio formats.",
  };
  const body: AnalyzeError = {
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

function normalizeReadyResponse(
  response: AnalyzeResponse,
  photoCount: 1 | 2 | 3,
): AnalyzeResponse {
  if (response.status === "needs_information") {
    return response;
  }

  return {
    claim: {
      ...response.claim,
      photoCount,
      status: "ready",
    },
    status: "ready",
  };
}

function toPhotoCount(count: number): 1 | 2 | 3 {
  if (count === 1 || count === 2 || count === 3) {
    return count;
  }

  throw new Error("Invalid photo count");
}

export function createAnalyzeHandler(
  createAnalyzer: () => ClaimAnalyzer,
  options: { includeActivity?: boolean } = {},
): (request: Request) => Promise<Response> {
  return async (request: Request) => {
    let parsedRequest: ParsedRequest;

    try {
      parsedRequest = parseRequest(await request.formData());
    } catch (error) {
      if (error instanceof RequestValidationError) {
        return errorResponse(error.code, error.status);
      }

      return errorResponse("invalid_input", 400);
    }

    let analyzer: ClaimAnalyzer;

    try {
      analyzer = createAnalyzer();
    } catch (error) {
      if (error instanceof AnalyzerNotConfiguredError) {
        return errorResponse("not_configured", 503);
      }

      return errorResponse("analysis_failed", 502);
    }

    try {
      const statement =
        parsedRequest.statementMode === "text"
          ? parsedRequest.statementText
          : await analyzer.transcribe(parsedRequest.voiceMemo);

      if (!statement.trim()) {
        throw new Error("The transcription was empty");
      }

      const analysis = await analyzer.analyze({
        ...(parsedRequest.followUp ? { followUp: parsedRequest.followUp } : {}),
        photos: parsedRequest.photos,
        statement: statement.trim(),
        statementMode: parsedRequest.statementMode,
      });
      const result = AnalyzeResponseSchema.parse(
        "result" in analysis ? analysis.result : analysis,
      );

      if (parsedRequest.followUp && result.status === "needs_information") {
        throw new Error("A second question is not allowed");
      }

      const normalizedResult = normalizeReadyResponse(
        result,
        toPhotoCount(parsedRequest.photos.length),
      );
      const body = options.includeActivity
        ? DemoAnalyzeResponseSchema.parse({
            activity: "activity" in analysis ? analysis.activity : undefined,
            result: normalizedResult,
          })
        : normalizedResult;

      return Response.json(body, {
        headers: { "cache-control": "no-store" },
      });
    } catch {
      return errorResponse("analysis_failed", 502);
    }
  };
}
