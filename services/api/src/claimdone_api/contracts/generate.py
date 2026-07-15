"""Generate deterministic JSON Schema and dependency-free TypeScript contracts."""

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel
from pydantic.json_schema import JsonSchemaMode, models_json_schema

from .base import CONTRACT_VERSION
from .eval_results import EvalCaseResult, EvalCheckResult, EvalRunSummary
from .evals import EvalCase
from .http_workflow import (
    ClarificationAnswerRequest,
    ClarificationView,
    WorkflowCaseView,
    WorkflowSnapshot,
)
from .models import (
    AuditEvent,
    ClaimData,
    ClaimPacket,
    ClaimScope,
    EvidenceFact,
    EvidenceItem,
    GateDecision,
    PlanStep,
    ProvenanceRef,
    ToolPlan,
    VerificationReport,
)
from .portal import (
    PortalDraftFields,
    PortalReviewFields,
    PortalRunRelease,
    PortalRunRenderFaultInjection,
    PortalRunRenderFaultRepair,
    PortalRunSetup,
    PortalSessionView,
    RenderedPortalSnapshot,
    SandboxReceipt,
)
from .release import ReleaseDecision
from .state_machine import CASE_TRANSITIONS, TERMINAL_CASE_STATES
from .tooling import ToolInvocation
from .transcript import TranscriptConfirmationRequest, TranscriptConfirmationView
from .verification_attempts import VerificationAttempt, VerificationAttemptSeries
from .workflow import WorkflowEventEnvelope

PUBLIC_MODELS: tuple[type[BaseModel], ...] = (
    ClaimPacket,
    ClaimScope,
    EvidenceItem,
    EvidenceFact,
    ProvenanceRef,
    ClaimData,
    ToolPlan,
    PlanStep,
    GateDecision,
    VerificationReport,
    AuditEvent,
    TranscriptConfirmationView,
    TranscriptConfirmationRequest,
    WorkflowCaseView,
    ClarificationView,
    ClarificationAnswerRequest,
    WorkflowSnapshot,
    ToolInvocation,
    PortalDraftFields,
    PortalReviewFields,
    PortalRunRenderFaultInjection,
    PortalRunRenderFaultRepair,
    PortalRunSetup,
    PortalRunRelease,
    PortalSessionView,
    RenderedPortalSnapshot,
    SandboxReceipt,
    WorkflowEventEnvelope,
    VerificationAttempt,
    VerificationAttemptSeries,
    EvalCheckResult,
    EvalCaseResult,
    EvalRunSummary,
    EvalCase,
    ReleaseDecision,
)

SCHEMA_FILENAME = "claimdone.schema.json"
TYPESCRIPT_FILENAME = "claimdone.ts"
_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


def build_schema() -> dict[str, Any]:
    """Build the versioned JSON Schema catalog from canonical Pydantic models."""

    model_inputs: list[tuple[type[BaseModel], JsonSchemaMode]] = [
        (model, "validation") for model in PUBLIC_MODELS
    ]
    roots, generated = models_json_schema(
        model_inputs,
        by_alias=True,
        ref_template="#/$defs/{model}",
        title="ClaimDone canonical contracts",
    )
    definitions = cast(dict[str, Any], generated["$defs"])
    root_refs = {model.__name__: roots[(model, "validation")] for model in PUBLIC_MODELS}
    transitions = {
        state.value: [target.value for target in sorted(targets, key=lambda item: item.value)]
        for state, targets in CASE_TRANSITIONS.items()
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://claimdone.local/contracts/{CONTRACT_VERSION}/claimdone.schema.json",
        "title": "ClaimDone canonical contracts",
        "description": (
            "JSON Schema 2020-12 catalog compatible with the OpenAPI 3.1 schema dialect."
        ),
        "x-contract-version": CONTRACT_VERSION,
        "x-agent-can-submit": False,
        "x-root-models": root_refs,
        "x-case-transitions": transitions,
        "x-terminal-case-states": sorted(state.value for state in TERMINAL_CASE_STATES),
        "$defs": definitions,
    }


def render_schema(schema: Mapping[str, Any]) -> str:
    """Serialize the schema with stable formatting and a final newline."""

    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _literal(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _deduplicated_union(parts: Sequence[str]) -> str:
    unique = list(dict.fromkeys(parts))
    return " | ".join(unique) if unique else "never"


def _property_name(value: str) -> str:
    return value if _IDENTIFIER.fullmatch(value) else _literal(value)


def _typescript_type(schema: Mapping[str, Any]) -> str:
    reference = schema.get("$ref")
    if isinstance(reference, str):
        return reference.rsplit("/", maxsplit=1)[-1]

    if "const" in schema:
        return _literal(schema["const"])

    enum_values = schema.get("enum")
    if isinstance(enum_values, list):
        return _deduplicated_union([_literal(value) for value in enum_values])

    for union_key in ("anyOf", "oneOf"):
        options = schema.get(union_key)
        if isinstance(options, list):
            return _deduplicated_union(
                [_typescript_type(cast(Mapping[str, Any], option)) for option in options]
            )

    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        return " & ".join(_typescript_type(cast(Mapping[str, Any], option)) for option in all_of)

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        return _deduplicated_union([_typescript_type({"type": item}) for item in schema_type])
    if schema_type == "string":
        return "string"
    if schema_type in {"integer", "number"}:
        return "number"
    if schema_type == "boolean":
        return "boolean"
    if schema_type == "null":
        return "null"
    if schema_type == "array":
        item_schema = schema.get("items", {})
        item_type = _typescript_type(cast(Mapping[str, Any], item_schema))
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and minimum == maximum:
            return "readonly [" + ", ".join([item_type] * minimum) + "]"
        return f"ReadonlyArray<{item_type}>"
    if schema_type == "object" or "properties" in schema:
        properties = cast(Mapping[str, Mapping[str, Any]], schema.get("properties", {}))
        if properties:
            required = set(cast(Sequence[str], schema.get("required", [])))
            members = []
            for name, property_schema in properties.items():
                optional = "" if name in required else "?"
                members.append(
                    f"readonly {_property_name(name)}{optional}: "
                    f"{_typescript_type(property_schema)};"
                )
            return "{ " + " ".join(members) + " }"
        additional = schema.get("additionalProperties")
        if additional is False:
            return "Readonly<Record<string, never>>"
        if isinstance(additional, dict):
            return f"Readonly<Record<string, {_typescript_type(additional)}>>"
        return "Readonly<Record<string, unknown>>"
    return "unknown"


def _render_definition(name: str, definition: Mapping[str, Any]) -> str:
    if definition.get("type") == "object" or "properties" in definition:
        properties = cast(Mapping[str, Mapping[str, Any]], definition.get("properties", {}))
        if not properties and definition.get("additionalProperties") is False:
            return f"export type {name} = Readonly<Record<string, never>>;"
        required = set(cast(Sequence[str], definition.get("required", [])))
        lines = [f"export interface {name} {{"]
        for property_name, property_schema in properties.items():
            optional = "" if property_name in required else "?"
            lines.append(
                f"  readonly {_property_name(property_name)}{optional}: "
                f"{_typescript_type(property_schema)};"
            )
        lines.append("}")
        return "\n".join(lines)
    return f"export type {name} = {_typescript_type(definition)};"


def render_typescript(schema: Mapping[str, Any]) -> str:
    """Render standalone readonly TypeScript declarations only from JSON Schema."""

    definitions = cast(Mapping[str, Mapping[str, Any]], schema["$defs"])
    transitions = cast(Mapping[str, Sequence[str]], schema["x-case-transitions"])
    terminal_states = cast(Sequence[str], schema["x-terminal-case-states"])
    root_models = cast(Mapping[str, Mapping[str, str]], schema["x-root-models"])

    sections = [
        "// Generated by claimdone_api.contracts.generate. DO NOT EDIT.",
        "/* eslint-disable */",
        "",
        f"export const CONTRACT_VERSION = {_literal(schema['x-contract-version'])} as const;",
        f"export const AGENT_CAN_SUBMIT = {_literal(schema['x-agent-can-submit'])} as const;",
        "",
    ]
    sections.extend(
        _render_definition(name, definition) for name, definition in sorted(definitions.items())
    )
    sections.extend(
        [
            "",
            "export const CONTRACT_ROOT_MODELS = "
            f"{json.dumps(sorted(root_models), ensure_ascii=False, indent=2)} as const;",
            "",
            "export const CASE_TRANSITIONS = "
            f"{json.dumps(transitions, ensure_ascii=False, indent=2, sort_keys=True)} "
            "as const satisfies Readonly<Record<CaseState, readonly CaseState[]>>;",
            "",
            "export const TERMINAL_CASE_STATES = "
            f"{json.dumps(terminal_states, ensure_ascii=False, indent=2)} "
            "as const satisfies readonly CaseState[];",
            "",
        ]
    )
    return "\n\n".join(sections).replace("\n\n\n", "\n\n").rstrip() + "\n"


def repository_root() -> Path:
    """Resolve the repository root from this stable package location."""

    return Path(__file__).resolve().parents[5]


def generated_artifacts(root: Path | None = None) -> dict[Path, str]:
    """Return every generated path and its expected byte content."""

    destination = (root or repository_root()) / "contracts" / "generated"
    schema = build_schema()
    schema_content = render_schema(schema)
    canonical_schema = cast(dict[str, Any], json.loads(schema_content))
    return {
        destination / SCHEMA_FILENAME: schema_content,
        destination / TYPESCRIPT_FILENAME: render_typescript(canonical_schema),
    }


def write_artifacts(root: Path | None = None) -> None:
    """Write generated artifacts deterministically."""

    for path, content in generated_artifacts(root).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def check_artifacts(root: Path | None = None) -> list[Path]:
    """Return generated paths whose committed content has drifted."""

    drifted = []
    for path, expected in generated_artifacts(root).items():
        if not path.is_file() or path.read_text(encoding="utf-8") != expected:
            drifted.append(path)
    return drifted


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail without writing when committed generated files have drifted",
    )
    arguments = parser.parse_args(argv)
    if arguments.check:
        drifted = check_artifacts()
        if drifted:
            for path in drifted:
                print(f"generated contract drift: {path}", file=sys.stderr)
            return 1
        print("generated contracts are current")
        return 0
    write_artifacts()
    print("generated ClaimDone JSON Schema and TypeScript contracts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
