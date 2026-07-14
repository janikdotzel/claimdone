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
        "WorkflowEventEnvelope",
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
    assert "readonly attachments: readonly [string, string, string];" in typescript


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
