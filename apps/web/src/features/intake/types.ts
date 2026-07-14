import type { GateReasonCode } from "../../../../../contracts/generated/claimdone";
import type { IntakeFlowResponse } from "./api";

export const REQUIRED_IMAGE_COUNT = 3;
export const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
export const MAX_AUDIO_SECONDS = 60;

export type ExifDecision = "strip" | "retain";
export type ImageSignature = "jpeg" | "png";
export type InspectionStatus = "checking" | "complete" | "error";
export type StatementMode = "text" | "audio";

export type IntakeImage = Readonly<{
  decision: ExifDecision | null;
  error: string | null;
  fingerprint: string;
  id: string;
  inspectionStatus: InspectionStatus;
  metadataFound: boolean | null;
  metadataSummary: string;
  mimeType: string;
  name: string;
  previewUrl: string;
  signature: ImageSignature | null;
  size: number;
}>;

export type AudioStatement = Readonly<{
  durationSeconds: number | null;
  error: string | null;
  id: string;
  mimeType: string;
  name: string;
  previewUrl: string;
  status: InspectionStatus;
}>;

export type IntakeConsents = Readonly<{
  dataProcessing: boolean;
  imageRights: boolean;
  sandbox: boolean;
}>;

export type IntakeStage =
  | "disclosure"
  | "intake"
  | "awaiting_clarification"
  | "review";

export type ServerRequestKind = "intake" | "clarification";

export type ServerRequest = Readonly<{
  inputRevision: number;
  kind: ServerRequestKind;
  token: number;
}>;

export type ServerErrorState = Readonly<{
  code: string;
  currentVersion: number | null;
  message: string;
  reasonCodes: ReadonlyArray<GateReasonCode>;
}>;

export type IntakeState = Readonly<{
  audio: AudioStatement | null;
  backendErrors: Readonly<Record<string, string>>;
  clientErrors: Readonly<Record<string, string>>;
  consents: IntakeConsents;
  disclosureAccepted: boolean;
  images: ReadonlyArray<IntakeImage>;
  inputRevision: number;
  pendingCaseId: string | null;
  serverAuthority: IntakeFlowResponse | null;
  serverError: ServerErrorState | null;
  serverRequest: ServerRequest | null;
  stage: IntakeStage;
  statementMode: StatementMode;
  textStatement: string;
}>;

export type BackendValidationError = Readonly<{
  field: string;
  message: string;
}>;

export type IntakeAction =
  | Readonly<{ type: "SET_DISCLOSURE_ACCEPTED"; value: boolean }>
  | Readonly<{ type: "BEGIN_INTAKE" }>
  | Readonly<{
      error: string | null;
      images: ReadonlyArray<IntakeImage>;
      type: "ADD_IMAGES";
    }>
  | Readonly<{ id: string; type: "REMOVE_IMAGE" }>
  | Readonly<{
      error: string | null;
      id: string;
      metadataFound: boolean | null;
      metadataSummary: string;
      signature: ImageSignature | null;
      status: InspectionStatus;
      type: "COMPLETE_IMAGE_INSPECTION";
    }>
  | Readonly<{ decision: ExifDecision; id: string; type: "SET_EXIF_DECISION" }>
  | Readonly<{ mode: StatementMode; type: "SET_STATEMENT_MODE" }>
  | Readonly<{ type: "SET_TEXT_STATEMENT"; value: string }>
  | Readonly<{ audio: AudioStatement; type: "SET_AUDIO" }>
  | Readonly<{
      durationSeconds: number | null;
      error: string | null;
      id: string;
      status: InspectionStatus;
      type: "COMPLETE_AUDIO_INSPECTION";
    }>
  | Readonly<{ type: "REMOVE_AUDIO" }>
  | Readonly<{
      consent: keyof IntakeConsents;
      type: "SET_CONSENT";
      value: boolean;
    }>
  | Readonly<{ errors: ReadonlyArray<BackendValidationError>; type: "SET_BACKEND_ERRORS" }>
  | Readonly<{
      kind: ServerRequestKind;
      token: number;
      type: "BEGIN_SERVER_REQUEST";
    }>
  | Readonly<{
      caseId: string;
      token: number;
      type: "SERVER_CASE_CREATED";
    }>
  | Readonly<{
      caseId: string;
      type: "SERVER_CASE_CLEANED";
    }>
  | Readonly<{
      response: IntakeFlowResponse;
      token: number;
      type: "SERVER_SUCCEEDED";
    }>
  | Readonly<{
      currentVersion: number | null;
      errors: ReadonlyArray<BackendValidationError>;
      message: string;
      reasonCodes: ReadonlyArray<GateReasonCode>;
      code: string;
      token: number;
      type: "SERVER_FAILED";
    }>
  | Readonly<{ type: "RESET" }>;

export type GateCheck = Readonly<{
  passed: boolean;
  reasonCodes: ReadonlyArray<GateReasonCode>;
}>;

export type IntakeGateResult = Readonly<{
  canContinue: boolean;
  fieldErrors: Readonly<Record<string, string>>;
  g0: GateCheck;
  g1: GateCheck;
}>;
