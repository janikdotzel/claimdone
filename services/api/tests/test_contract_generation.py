import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

from claimdone_api.contracts import CaseState
from claimdone_api.contracts.generate import (
    PUBLIC_MODELS,
    build_schema,
    check_artifacts,
    render_schema,
    render_typescript,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
GENERATED = REPOSITORY_ROOT / "contracts" / "generated"
SCHEMA_PATH = GENERATED / "claimdone.schema.json"
TYPESCRIPT_PATH = GENERATED / "claimdone.ts"


def committed_schema() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def test_generated_artifacts_have_no_drift() -> None:
    assert check_artifacts(REPOSITORY_ROOT) == []


def test_committed_typescript_is_rendered_from_committed_schema() -> None:
    typescript = TYPESCRIPT_PATH.read_text(encoding="utf-8")

    assert render_typescript(committed_schema()) == typescript


def test_schema_generation_is_byte_deterministic() -> None:
    assert render_schema(build_schema()) == render_schema(build_schema())


def test_schema_exports_every_required_public_root() -> None:
    roots = committed_schema()["x-root-models"]

    assert set(roots) == {model.__name__ for model in PUBLIC_MODELS}
    assert set(roots) == {
        "AuditEvent",
        "ClaimData",
        "ClaimPacket",
        "ClaimScope",
        "ClarificationAnswerRequest",
        "ClarificationView",
        "EvalCase",
        "EvalCaseResult",
        "EvalCheckResult",
        "EvalRunSummary",
        "EvidenceFact",
        "EvidenceItem",
        "GateDecision",
        "PlanStep",
        "PortalDraftFields",
        "PortalReviewFields",
        "PortalSessionView",
        "ProvenanceRef",
        "ReleaseDecision",
        "RenderedPortalSnapshot",
        "SandboxReceipt",
        "ToolInvocation",
        "ToolPlan",
        "TranscriptConfirmationRequest",
        "TranscriptConfirmationView",
        "VerificationAttempt",
        "VerificationAttemptSeries",
        "VerificationReport",
        "WorkflowCaseView",
        "WorkflowEventEnvelope",
        "WorkflowSnapshot",
    }


def test_schema_and_typescript_lock_submission_boundary_to_false() -> None:
    schema = committed_schema()
    definitions = schema["$defs"]
    typescript = TYPESCRIPT_PATH.read_text(encoding="utf-8")

    assert definitions["ClaimScope"]["properties"]["agentCanSubmit"]["const"] is False
    assert definitions["ToolPlan"]["properties"]["agentCanSubmit"]["const"] is False
    assert "readonly agentCanSubmit: false;" in typescript
    assert "export const AGENT_CAN_SUBMIT = false as const;" in typescript


def test_exact_three_attachments_render_as_readonly_tuple() -> None:
    schema = committed_schema()
    attachment_schema = schema["$defs"]["ClaimData"]["properties"]["attachments"]
    typescript = TYPESCRIPT_PATH.read_text(encoding="utf-8")

    assert attachment_schema["minItems"] == 3
    assert attachment_schema["maxItems"] == 3
    assert attachment_schema["uniqueItems"] is True
    assert committed_schema()["$defs"]["EvidenceItem"]["properties"]["localRef"][
        "pattern"
    ] == "^[A-Za-z0-9][A-Za-z0-9._:-]*$"
    assert "readonly attachments: readonly [string, string, string];" in typescript


def test_verification_attachment_identity_schema_has_v4_bounds_and_required_fields() -> None:
    report = committed_schema()["$defs"]["VerificationReport"]
    properties = report["properties"]
    typescript = TYPESCRIPT_PATH.read_text(encoding="utf-8")

    assert properties["expectedAttachmentIds"]["minItems"] == 3
    assert properties["expectedAttachmentIds"]["maxItems"] == 3
    assert properties["expectedAttachmentIds"]["uniqueItems"] is True
    actual_ids = properties["actualAttachmentIds"]["anyOf"][0]
    assert actual_ids["maxItems"] == 3
    assert actual_ids["uniqueItems"] is True
    actual_count = properties["actualAttachmentCount"]["anyOf"][0]
    assert actual_count["minimum"] == 0
    assert actual_count["maximum"] == 3
    assert {
        "expectedAttachmentCount",
        "expectedAttachmentIds",
        "actualAttachmentCount",
        "actualAttachmentIds",
    }.issubset(report["required"])
    assert (
        "readonly expectedAttachmentIds: readonly [string, string, string];"
        in typescript
    )
    assert "readonly actualAttachmentIds: ReadonlyArray<string> | null;" in typescript


def test_portal_attachment_schemas_require_unique_ids() -> None:
    definitions = committed_schema()["$defs"]

    assert definitions["PortalDraftFields"]["properties"]["attachments"][
        "uniqueItems"
    ] is True
    assert definitions["PortalReviewFields"]["properties"]["attachments"][
        "uniqueItems"
    ] is True


def test_generated_transition_map_covers_every_case_state() -> None:
    transitions = committed_schema()["x-case-transitions"]

    assert set(transitions) == {state.value for state in CaseState}
    assert transitions["review"] == ["abandoned", "failed", "human_approved"]
    assert transitions["awaiting_transcript_confirmation"] == [
        "abandoned",
        "analyzing",
        "blocked",
        "emergency_stopped",
        "failed",
    ]
    assert transitions["blocked"] == []


def _snapshot_state(variant: dict[str, Any]) -> str:
    case_schema = cast(dict[str, Any], variant["properties"]["case"])
    refinement = cast(dict[str, Any], case_schema["allOf"][1])
    return cast(str, refinement["properties"]["state"]["const"])


def _allows_null(schema: dict[str, Any]) -> bool:
    if schema.get("type") == "null":
        return True
    return any(
        isinstance(option, dict) and option.get("type") == "null"
        for option in cast(list[object], schema.get("anyOf", []))
    )


def test_workflow_snapshot_schema_encodes_the_state_payload_matrix() -> None:
    definitions = committed_schema()["$defs"]
    snapshot = cast(dict[str, Any], definitions["WorkflowSnapshot"])
    variants = cast(list[dict[str, Any]], snapshot["oneOf"])

    assert len(variants) == len(CaseState) + 4
    assert {"ClaimPacket", "PortalSessionView", "VerificationAttemptSeries"} <= set(
        definitions
    )

    created = next(variant for variant in variants if _snapshot_state(variant) == "created")
    for field in ("claimPacket", "portalSession", "verificationAttempts", "receipt"):
        assert created["properties"][field] == {"type": "null"}

    review = next(variant for variant in variants if _snapshot_state(variant) == "review")
    for field in ("claimPacket", "portalSession", "verificationAttempts"):
        assert not _allows_null(review["properties"][field])
    assert review["properties"]["receipt"] == {"type": "null"}

    verifying = next(
        variant for variant in variants if _snapshot_state(variant) == "verifying"
    )
    assert not _allows_null(verifying["properties"]["claimPacket"])
    assert not _allows_null(verifying["properties"]["portalSession"])
    assert _allows_null(verifying["properties"]["verificationAttempts"])

    approved = next(
        variant for variant in variants if _snapshot_state(variant) == "human_approved"
    )
    assert not _allows_null(approved["properties"]["claimPacket"])
    assert approved["properties"]["portalSession"] == {"type": "null"}
    assert approved["properties"]["verificationAttempts"] == {"type": "null"}

    blocked = [variant for variant in variants if _snapshot_state(variant) == "blocked"]
    assert len(blocked) == 2
    packetless = next(
        variant
        for variant in blocked
        if variant["properties"]["claimPacket"] == {"type": "null"}
    )
    assert packetless["properties"]["verificationAttempts"] == {"type": "null"}


def test_tool_call_schema_discriminates_started_and_terminal_duration() -> None:
    tool_call = cast(
        dict[str, Any], committed_schema()["$defs"]["ToolCallWorkflowEvent"]
    )
    variants = cast(list[dict[str, Any]], tool_call["oneOf"])
    assert len(variants) == 2

    started = next(
        variant
        for variant in variants
        if variant["properties"]["status"].get("const") == "started"
    )
    assert "durationMs" not in started["properties"]
    assert "durationMs" not in started["required"]

    terminal = next(variant for variant in variants if variant is not started)
    assert terminal["properties"]["status"]["enum"] == ["succeeded", "blocked"]
    assert terminal["properties"]["durationMs"]["type"] == "integer"
    assert "durationMs" in terminal["required"]


def test_generated_typescript_and_type_assertions_compile() -> None:
    node = shutil.which("node")
    tsc = REPOSITORY_ROOT / "apps" / "web" / "node_modules" / ".bin" / "tsc"
    assert node is not None, "Node.js is required for the cross-language contract gate"
    assert tsc.exists(), "Run pnpm install before the cross-language contract gate"

    result = subprocess.run(
        [str(tsc), "-p", str(REPOSITORY_ROOT / "contracts" / "tsconfig.json")],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
