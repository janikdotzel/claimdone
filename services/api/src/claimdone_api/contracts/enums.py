"""Stable enum values shared by every ClaimDone contract consumer."""

from enum import StrEnum
from types import MappingProxyType


class FactStatus(StrEnum):
    """How strongly an evidence fact is supported."""

    OBSERVED = "observed"
    USER_STATED = "user_stated"
    UNKNOWN = "unknown"
    NOT_SUPPORTED = "not_supported"


class CaseState(StrEnum):
    """Canonical orchestrator states."""

    CREATED = "created"
    DISCLOSED = "disclosed"
    ANALYZING = "analyzing"
    AWAITING_TRANSCRIPT_CONFIRMATION = "awaiting_transcript_confirmation"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    READY_TO_FILL = "ready_to_fill"
    FILLING = "filling"
    VERIFYING = "verifying"
    REVIEW = "review"
    BLOCKED = "blocked"
    HUMAN_APPROVED = "human_approved"
    RECEIPT = "receipt"
    EMERGENCY_STOPPED = "emergency_stopped"
    ABANDONED = "abandoned"
    FAILED = "failed"


class PortalState(StrEnum):
    """States exposed by the local sandbox portal."""

    DRAFT = "draft"
    REVIEW = "review"
    HUMAN_APPROVED = "human_approved"
    RECEIPT = "receipt"


class VerificationState(StrEnum):
    """Outcome of the independent verification pass."""

    PENDING = "pending"
    VERIFIED = "verified"
    MISMATCH = "mismatch"
    BLOCKED = "blocked"


class GateId(StrEnum):
    """Ordered deterministic gate registry identifiers."""

    G0_INTAKE = "G0"
    G1_PRIVACY = "G1"
    G2_OUTPUT_CONTRACT = "G2"
    G3_SAFETY_SCOPE = "G3"
    G4_PROVENANCE = "G4"
    G5_COMPLETENESS = "G5"
    G6_TOOL_AUTHORITY = "G6"
    G7_PORTAL_WRITE = "G7"
    G8_VERIFICATION = "G8"
    G9_HUMAN_APPROVAL = "G9"
    G10_RECEIPT_REDACTION = "G10"
    G11_RELEASE = "G11"


class GateReasonCode(StrEnum):
    """Machine-readable deterministic reasons for blocking a gate."""

    G0_IMAGE_COUNT_INVALID = "G0_IMAGE_COUNT_INVALID"
    G0_IMAGE_TYPE_INVALID = "G0_IMAGE_TYPE_INVALID"
    G0_IMAGE_TOO_LARGE = "G0_IMAGE_TOO_LARGE"
    G0_INPUT_MODE_INVALID = "G0_INPUT_MODE_INVALID"
    G0_AUDIO_TOO_LONG = "G0_AUDIO_TOO_LONG"
    G0_CONSENT_MISSING = "G0_CONSENT_MISSING"

    G1_EXIF_UNREVIEWED = "G1_EXIF_UNREVIEWED"
    G1_MODEL_COPY_NOT_APPROVED = "G1_MODEL_COPY_NOT_APPROVED"
    G1_SENSITIVE_LOG_DATA = "G1_SENSITIVE_LOG_DATA"

    G2_SCHEMA_INVALID = "G2_SCHEMA_INVALID"
    G2_REFUSAL = "G2_REFUSAL"
    G2_OUTPUT_TRUNCATED = "G2_OUTPUT_TRUNCATED"
    G2_REFERENCE_MISSING = "G2_REFERENCE_MISSING"
    G2_RETRY_EXHAUSTED = "G2_RETRY_EXHAUSTED"

    G3_INJURY_OR_EMERGENCY = "G3_INJURY_OR_EMERGENCY"
    G3_REAL_PORTAL = "G3_REAL_PORTAL"
    G3_LEGAL_OR_LIABILITY = "G3_LEGAL_OR_LIABILITY"
    G3_PAYMENT_OR_COVERAGE = "G3_PAYMENT_OR_COVERAGE"
    G3_SUBMISSION_ACTION = "G3_SUBMISSION_ACTION"
    G3_MODEL_UNCERTAIN = "G3_MODEL_UNCERTAIN"

    G4_PROVENANCE_MISSING = "G4_PROVENANCE_MISSING"
    G4_SENSITIVE_IMAGE_INFERENCE = "G4_SENSITIVE_IMAGE_INFERENCE"
    G4_FACT_NOT_WRITABLE = "G4_FACT_NOT_WRITABLE"
    G4_CONFIDENCE_BELOW_THRESHOLD = "G4_CONFIDENCE_BELOW_THRESHOLD"
    G4_CONFLICTING_SOURCES = "G4_CONFLICTING_SOURCES"
    G4_NARRATIVE_UNSUPPORTED = "G4_NARRATIVE_UNSUPPORTED"

    G5_REQUIRED_FIELD_MISSING = "G5_REQUIRED_FIELD_MISSING"
    G5_QUESTION_INVALID = "G5_QUESTION_INVALID"
    G5_CLARIFICATION_LIMIT = "G5_CLARIFICATION_LIMIT"

    G6_TOOL_UNKNOWN = "G6_TOOL_UNKNOWN"
    G6_ARGUMENTS_INVALID = "G6_ARGUMENTS_INVALID"
    G6_STATE_INVALID = "G6_STATE_INVALID"
    G6_URL_NOT_ALLOWED = "G6_URL_NOT_ALLOWED"
    G6_LIMIT_EXCEEDED = "G6_LIMIT_EXCEEDED"
    G6_FORBIDDEN_ACTION = "G6_FORBIDDEN_ACTION"

    G7_FIELD_NOT_ALLOWED = "G7_FIELD_NOT_ALLOWED"
    G7_VALUE_NOT_FROM_PACKET = "G7_VALUE_NOT_FROM_PACKET"
    G7_PROVENANCE_MISSING = "G7_PROVENANCE_MISSING"
    G7_FIELD_NOT_EDITABLE = "G7_FIELD_NOT_EDITABLE"
    G7_ATTACHMENT_MISMATCH = "G7_ATTACHMENT_MISMATCH"

    G8_FIELD_MISMATCH = "G8_FIELD_MISMATCH"
    G8_ATTACHMENT_MISMATCH = "G8_ATTACHMENT_MISMATCH"
    G8_REQUIRED_FIELD_MISSING = "G8_REQUIRED_FIELD_MISSING"
    G8_MODEL_MISMATCH = "G8_MODEL_MISMATCH"

    G9_AGENT_FORBIDDEN = "G9_AGENT_FORBIDDEN"
    G9_ROLE_INVALID = "G9_ROLE_INVALID"
    G9_TOKEN_INVALID = "G9_TOKEN_INVALID"

    G10_BEFORE_APPROVAL = "G10_BEFORE_APPROVAL"
    G10_REDACTION_FAILED = "G10_REDACTION_FAILED"

    G11_DETERMINISTIC_TESTS_FAILED = "G11_DETERMINISTIC_TESTS_FAILED"
    G11_SAFETY_EVAL_FAILED = "G11_SAFETY_EVAL_FAILED"
    G11_THRESHOLD_FAILED = "G11_THRESHOLD_FAILED"
    G11_PORTAL_SUCCESS_FAILED = "G11_PORTAL_SUCCESS_FAILED"
    G11_APPROVAL_ATTACK_FAILED = "G11_APPROVAL_ATTACK_FAILED"
    G11_CLEAN_CHECKOUT_FAILED = "G11_CLEAN_CHECKOUT_FAILED"
    G11_DOCUMENTATION_MISSING = "G11_DOCUMENTATION_MISSING"
    G11_LICENSE_MISSING = "G11_LICENSE_MISSING"
    G11_FIXTURES_MISSING = "G11_FIXTURES_MISSING"
    G11_TEST_REPORT_MISSING = "G11_TEST_REPORT_MISSING"
    G11_HUMAN_CHECKPOINT_MISSING = "G11_HUMAN_CHECKPOINT_MISSING"


MODEL_BLOCK_REASON_BY_GATE = MappingProxyType(
    {
        GateId.G3_SAFETY_SCOPE: GateReasonCode.G3_MODEL_UNCERTAIN,
        GateId.G8_VERIFICATION: GateReasonCode.G8_MODEL_MISMATCH,
    }
)


class EvidenceKind(StrEnum):
    IMAGE = "image"
    USER_STATEMENT = "user_statement"
    TRANSCRIPT = "transcript"
    CLARIFICATION = "clarification"


class EvidenceField(StrEnum):
    VISIBLE_DAMAGE = "visible_damage"
    COLLISION_TYPE = "collision_type"
    VEHICLE_COUNT = "vehicle_count"
    IMPACT_AREA = "impact_area"
    INCIDENT_DATE = "incident_date"
    INCIDENT_TIME = "incident_time"
    LOCATION = "location"
    CLAIMANT_NAME = "claimant_name"
    POLICY_REFERENCE = "policy_reference"
    VEHICLE_REGISTRATION = "vehicle_registration"
    COUNTERPARTY_KNOWN = "counterparty_known"
    NARRATIVE = "narrative"
    INJURY_STATUS = "injury_status"
    IMMEDIATE_DANGER = "immediate_danger"


class RequiredClaimField(StrEnum):
    INCIDENT_DATE = "incident_date"
    INCIDENT_TIME = "incident_time"
    LOCATION = "location"
    CLAIMANT_NAME = "claimant_name"
    POLICY_REFERENCE = "policy_reference"
    VEHICLE_REGISTRATION = "vehicle_registration"
    COUNTERPARTY_KNOWN = "counterparty_known"
    NARRATIVE = "narrative"
    ATTACHMENTS = "attachments"


class CounterpartyKnown(StrEnum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class AllowedTool(StrEnum):
    INSPECT_EVIDENCE = "inspect_evidence"
    CHECK_REQUIRED_FIELDS = "check_required_fields"
    ASK_CLARIFICATION = "ask_clarification"
    INSPECT_FORM = "inspect_form"
    FILL_UNTIL_REVIEW = "fill_until_review"
    VERIFY_RENDERED_FIELDS = "verify_rendered_fields"
    READ_RECEIPT = "read_receipt"


class PortalVariant(StrEnum):
    """Closed visual variants supported by the local sandbox portal."""

    A = "A"
    B = "B"


class ProviderFailureCategory(StrEnum):
    """Sanitized provider failure categories safe for persisted workflow events."""

    QUOTA_EXHAUSTED = "quota_exhausted"
    BILLING_LIMIT = "billing_limit"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    MODEL_NOT_FOUND = "model_not_found"
    INVALID_RESPONSE = "invalid_response"
    CONTENT_FILTERED = "content_filtered"
    AUTHENTICATION_FAILED = "authentication_failed"
    PERMISSION_DENIED = "permission_denied"
    INVALID_REQUEST = "invalid_request"
    CANCELLED = "cancelled"


class WorkflowEventKind(StrEnum):
    """Closed discriminator for persistable workflow events."""

    STATE = "state"
    GATE = "gate"
    CLARIFICATION = "clarification"
    PLAN_STEP = "plan_step"
    TOOL_CALL = "tool_call"
    PORTAL_FILL = "portal_fill"
    VERIFICATION = "verification"
    RETRY = "retry"
    OPERATIONAL_FAILURE = "operational_failure"
    PROVIDER_CALL = "provider_call"


class ClarificationStatus(StrEnum):
    """Content-free clarification lifecycle states."""

    REQUESTED = "requested"
    CONFIRMED = "confirmed"
    EXHAUSTED = "exhausted"


class ToolCallStatus(StrEnum):
    """Persistable result of a bounded tool invocation."""

    STARTED = "started"
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"


class WorkflowOperation(StrEnum):
    """Closed operations that may be retried or fail operationally."""

    TRANSCRIPTION = "transcription"
    EXTRACTION = "extraction"
    COMPUTER_USE = "computer_use"
    VERIFICATION = "verification"


class ProviderModelId(StrEnum):
    """Exact V1 model identities allowed in value-free provider telemetry."""

    SOL = "gpt-5.6-sol"
    TERRA = "gpt-5.6-terra"
    LUNA = "gpt-5.6-luna"
    TRANSCRIBE = "gpt-4o-transcribe"
    DETERMINISTIC_MOCK = "claimdone-deterministic-mock"


class EvalGraderType(StrEnum):
    """Authority class for one evaluation check."""

    DETERMINISTIC = "deterministic"
    MODEL = "model"
    HUMAN = "human"


class EvalMetricId(StrEnum):
    """Closed release metrics required in every v2 eval run summary."""

    SCHEMA_VALIDITY = "schema_validity"
    PROVENANCE_COVERAGE = "provenance_coverage"
    FORBIDDEN_FACTS = "forbidden_facts"
    REQUIRED_FIELD_COMPLETION = "required_field_completion"
    SAFETY_BLOCKING = "safety_blocking"
    TOOL_POLICY = "tool_policy"
    PORTAL_VALUE_MATCH = "portal_value_match"
    MISMATCH_DETECTION = "mismatch_detection"
    APPROVAL_AUTHORITY = "approval_authority"
    RECEIPT_REDACTION = "receipt_redaction"


class EvalMetricStatus(StrEnum):
    """Aggregate result, including denominator-free not-applicable metrics."""

    PASSED = "passed"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"


class EvalFailureCode(StrEnum):
    """Why a grader failed, separate from observed product gate reasons."""

    EXPECTATION_MISMATCH = "expectation_mismatch"
    MISSING_OBSERVATION = "missing_observation"
    PROVIDER_FAILURE = "provider_failure"
    GRADER_FAILED = "grader_failed"


class VerificationFieldStatus(StrEnum):
    MATCH = "match"
    MISMATCH = "mismatch"
    MISSING = "missing"


class AuditEventType(StrEnum):
    CASE_STATE_CHANGED = "case_state_changed"
    GATE_DECISION = "gate_decision"
    PLAN_STEP = "plan_step"
    TOOL_CALL = "tool_call"
    CLARIFICATION = "clarification"
    PORTAL_FILL = "portal_fill"
    VERIFICATION = "verification"
    RETRY = "retry"
    OPERATIONAL_FAILURE = "operational_failure"
    PROVIDER_CALL = "provider_call"
    HUMAN_APPROVAL = "human_approval"
    RECEIPT = "receipt"
    RESET = "reset"


AUDIT_EVENT_TYPE_BY_WORKFLOW_KIND = MappingProxyType(
    {
        WorkflowEventKind.STATE: AuditEventType.CASE_STATE_CHANGED,
        WorkflowEventKind.GATE: AuditEventType.GATE_DECISION,
        WorkflowEventKind.CLARIFICATION: AuditEventType.CLARIFICATION,
        WorkflowEventKind.PLAN_STEP: AuditEventType.PLAN_STEP,
        WorkflowEventKind.TOOL_CALL: AuditEventType.TOOL_CALL,
        WorkflowEventKind.PORTAL_FILL: AuditEventType.PORTAL_FILL,
        WorkflowEventKind.VERIFICATION: AuditEventType.VERIFICATION,
        WorkflowEventKind.RETRY: AuditEventType.RETRY,
        WorkflowEventKind.OPERATIONAL_FAILURE: AuditEventType.OPERATIONAL_FAILURE,
        WorkflowEventKind.PROVIDER_CALL: AuditEventType.PROVIDER_CALL,
    }
)


class ActorType(StrEnum):
    SYSTEM = "system"
    AGENT = "agent"
    HUMAN = "human"


class EvaluationMode(StrEnum):
    DETERMINISTIC = "deterministic"
    LIVE = "live"
    HYBRID = "hybrid"


class EvalPriority(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class ReleaseCheckId(StrEnum):
    DETERMINISTIC_TESTS = "deterministic_tests"
    SAFETY_EVALS = "safety_evals"
    EVAL_THRESHOLDS = "eval_thresholds"
    PORTAL_SUCCESS_RATE = "portal_success_rate"
    APPROVAL_ATTACKS = "approval_attacks"
    CLEAN_CHECKOUT = "clean_checkout"
    README = "readme"
    LICENSE = "license"
    FIXTURES = "fixtures"
    TEST_REPORT = "test_report"


class HumanCheckpointId(StrEnum):
    DEMO_VIDEO = "demo_video"
    FEEDBACK_SESSION = "feedback_session"
    REPOSITORY_ACCESS = "repository_access"


class CheckpointStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
