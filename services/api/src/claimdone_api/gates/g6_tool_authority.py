"""G6 deterministic authority for one bounded local portal action."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import cast

from pydantic import ValidationError

from claimdone_api.contracts import (
    CONTRACT_VERSION,
    AllowedTool,
    CaseState,
    ClaimPacket,
    GateDecision,
    GateId,
    GateReasonCode,
    PortalVariant,
    ToolInvocation,
)

from .registry import make_gate_decision

MAX_G6_ACTIONS = 40
MAX_G6_SECONDS = 90.0
CANONICAL_PORTAL_ORIGIN = "http://127.0.0.1:3000"

_INVOCATION_KEYS = frozenset(
    {"contractVersion", "invocationId", "sequence", "tool", "arguments"}
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ALLOWED_ACTIONS = frozenset(
    {
        "click",
        "double_click",
        "drag",
        "move",
        "scroll",
        "keypress",
        "type",
        "wait",
        "screenshot",
    }
)
_FORBIDDEN_ACTION = re.compile(
    r"(?:approv(?:e|ed|al)?|authoriz(?:e|ation)|receipt|submit(?:ted|ting)?|"
    r"submission|reset|delete)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True, repr=False)
class ToolAuthorityContext:
    """Trusted state with a one-based action number reserved before execution."""

    packet: ClaimPacket = field(repr=False)
    case_state: CaseState
    portal_variant: PortalVariant
    current_url: str = field(repr=False)
    action: str = field(repr=False)
    proposed_action_number: int
    elapsed_seconds: float


@dataclass(frozen=True, slots=True, repr=False)
class ToolAuthorityResult:
    """Immutable G6 outcome; only a passing result exposes a parsed invocation."""

    decision: GateDecision
    invocation: ToolInvocation | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.decision.gate_id is not GateId.G6_TOOL_AUTHORITY:
            raise ValueError("ToolAuthorityResult requires a G6 decision")
        if self.decision.passed is not (self.invocation is not None):
            raise ValueError("Only a passing G6 result may expose an invocation")


def canonical_portal_case_url(case_id: str, variant: PortalVariant) -> str:
    """Return the only V1 browser URL authorized for a case and variant."""

    if type(case_id) is not str or _IDENTIFIER.fullmatch(case_id) is None:
        raise ValueError("caseId is not a canonical identifier")
    if not isinstance(variant, PortalVariant):
        raise ValueError("portalVariant is invalid")
    return f"{CANONICAL_PORTAL_ORIGIN}/sandbox/{variant.value}/cases/{case_id}"


def evaluate_g6(
    invocation_payload: object,
    *,
    context: ToolAuthorityContext,
    decided_at: datetime | None = None,
) -> ToolAuthorityResult:
    """Fail closed unless the exact planned fill invocation and action remain bounded."""

    reasons: set[GateReasonCode] = set()
    parsed_invocation: ToolInvocation | None = None
    payload = invocation_payload if type(invocation_payload) is dict else None

    if payload is None or set(payload) != _INVOCATION_KEYS:
        reasons.add(GateReasonCode.G6_ARGUMENTS_INVALID)

    tool_value = payload.get("tool") if payload is not None else None
    known_tool = _known_tool(tool_value)
    if known_tool is None:
        reasons.update(
            {
                GateReasonCode.G6_TOOL_UNKNOWN,
                GateReasonCode.G6_FORBIDDEN_ACTION,
            }
        )
    elif known_tool is not AllowedTool.FILL_UNTIL_REVIEW:
        reasons.add(GateReasonCode.G6_FORBIDDEN_ACTION)

    if payload is not None:
        if not _has_exact_invocation_scalars(payload):
            reasons.add(GateReasonCode.G6_ARGUMENTS_INVALID)
        elif known_tool is not None:
            try:
                parsed_invocation = ToolInvocation.model_validate(payload)
            except ValidationError:
                reasons.add(GateReasonCode.G6_ARGUMENTS_INVALID)

    fill_steps = tuple(
        step
        for step in context.packet.plan.steps
        if step.tool is AllowedTool.FILL_UNTIL_REVIEW
    )
    if known_tool is AllowedTool.FILL_UNTIL_REVIEW and (
        len(fill_steps) != 1
        or parsed_invocation is None
        or parsed_invocation.tool is not AllowedTool.FILL_UNTIL_REVIEW
        or parsed_invocation.sequence != fill_steps[0].sequence
    ):
        reasons.add(GateReasonCode.G6_ARGUMENTS_INVALID)

    if (
        context.case_state is not CaseState.FILLING
        or context.packet.state is not CaseState.FILLING
        or context.packet.portal_state.value != "draft"
    ):
        reasons.add(GateReasonCode.G6_STATE_INVALID)

    try:
        expected_url = canonical_portal_case_url(
            context.packet.case_id,
            context.portal_variant,
        )
    except ValueError:
        expected_url = None
    if type(context.current_url) is not str or context.current_url != expected_url:
        reasons.add(GateReasonCode.G6_URL_NOT_ALLOWED)

    if not _limits_are_valid(context.proposed_action_number, context.elapsed_seconds):
        reasons.add(GateReasonCode.G6_LIMIT_EXCEEDED)

    if (
        type(context.action) is not str
        or context.action not in _ALLOWED_ACTIONS
        or _FORBIDDEN_ACTION.search(context.action) is not None
    ):
        reasons.add(GateReasonCode.G6_FORBIDDEN_ACTION)

    decision = make_gate_decision(
        GateId.G6_TOOL_AUTHORITY,
        deterministic_reasons=tuple(reasons),
        decided_at=decided_at,
    )
    return ToolAuthorityResult(
        decision=decision,
        invocation=parsed_invocation if decision.passed else None,
    )


def _known_tool(value: object) -> AllowedTool | None:
    if type(value) is not str:
        return None
    try:
        return AllowedTool(value)
    except ValueError:
        return None


def _has_exact_invocation_scalars(payload: dict[object, object]) -> bool:
    invocation_id = payload.get("invocationId")
    sequence = payload.get("sequence")
    arguments = payload.get("arguments")
    return (
        set(payload) == _INVOCATION_KEYS
        and payload.get("contractVersion") == CONTRACT_VERSION
        and type(payload.get("contractVersion")) is str
        and type(invocation_id) is str
        and _IDENTIFIER.fullmatch(invocation_id) is not None
        and type(sequence) is int
        and 1 <= sequence <= MAX_G6_ACTIONS
        and type(payload.get("tool")) is str
        and type(arguments) is dict
        and not cast(dict[object, object], arguments)
    )


def _limits_are_valid(proposed_action_number: object, elapsed_seconds: object) -> bool:
    if (
        type(proposed_action_number) is not int
        or not 1 <= proposed_action_number <= MAX_G6_ACTIONS
    ):
        return False
    if type(elapsed_seconds) not in {int, float}:
        return False
    seconds = float(cast(int | float, elapsed_seconds))
    return math.isfinite(seconds) and 0.0 <= seconds <= MAX_G6_SECONDS
