"""GA Responses API adapter for the screenshot/action computer-use loop."""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol, TypeGuard, cast

from claimdone_api.ai.telemetry import response_member

from .models import (
    MAX_COMPUTER_ACTIONS,
    ClickAction,
    ComputerAction,
    ComputerCall,
    ComputerTurn,
    DoubleClickAction,
    DragAction,
    KeypressAction,
    MouseButton,
    MoveAction,
    Point,
    ScreenshotAction,
    ScrollAction,
    TypeAction,
    WaitAction,
)
from .ports import InvalidComputerResponseError, PendingComputerSafetyCheckError

COMPUTER_MODEL = "gpt-5.6"
COMPUTER_TOOL_TYPE = "computer"
MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_KNOWN_OUTPUT_TYPES = frozenset({"computer_call", "message", "reasoning"})
_MAX_RESPONSE_OUTPUT_ITEMS = 16
_MAX_PENDING_SAFETY_CHECKS = 16
_MAX_DRAG_POINTS = 128
_MAX_KEYPRESS_KEYS = 8
_MAX_MODIFIER_KEYS = 4
_COMPUTER_CALL_FIELDS = {
    "id",
    "call_id",
    "pending_safety_checks",
    "status",
    "type",
    "action",
    "actions",
}


class ResponsesCreateAPI(Protocol):
    """SDK-independent create surface used by production clients and deterministic fakes."""

    def create(self, **kwargs: object) -> object:
        """Create one response using the supplied GA request shape."""


class ResponsesComputerUseAdapter:
    """Emit only the current GA computer request shapes and sanitize responses."""

    def __init__(self, responses: ResponsesCreateAPI) -> None:
        self._responses = responses

    def start(self, task: str, *, timeout_seconds: float) -> ComputerTurn:
        """Send the first text-only turn with the GA computer tool enabled."""

        _require_task(task)
        _require_timeout(timeout_seconds)
        response = self._responses.create(
            model=COMPUTER_MODEL,
            tools=[{"type": COMPUTER_TOOL_TYPE}],
            input=task,
            timeout=float(timeout_seconds),
        )
        return _normalize_turn(response)

    def continue_with_screenshot(
        self,
        *,
        previous_response_id: str,
        call_id: str,
        screenshot_png: bytes,
        timeout_seconds: float,
    ) -> ComputerTurn:
        """Send one original-detail PNG for the exact preceding computer call."""

        _require_provider_identifier(previous_response_id)
        _require_provider_identifier(call_id)
        _require_screenshot(screenshot_png)
        _require_timeout(timeout_seconds)
        encoded = base64.b64encode(screenshot_png).decode("ascii")
        response = self._responses.create(
            model=COMPUTER_MODEL,
            tools=[{"type": COMPUTER_TOOL_TYPE}],
            previous_response_id=previous_response_id,
            input=[
                {
                    "type": "computer_call_output",
                    "call_id": call_id,
                    "output": {
                        "type": "computer_screenshot",
                        "image_url": f"data:image/png;base64,{encoded}",
                        "detail": "original",
                    },
                }
            ],
            timeout=float(timeout_seconds),
        )
        return _normalize_turn(response)


def _normalize_turn(response: object) -> ComputerTurn:
    try:
        response_id = _required_str(response_member(response, "id"))
        _require_provider_identifier(response_id)
        if response_member(response, "status") != "completed":
            raise ValueError("Response status is not completed")
        output = _bounded_sequence(
            response_member(response, "output"),
            maximum=_MAX_RESPONSE_OUTPUT_ITEMS,
            label="response output",
        )

        calls: list[ComputerCall] = []
        for item in output:
            output_type = response_member(item, "type")
            if output_type not in _KNOWN_OUTPUT_TYPES:
                raise ValueError("Response contains an unknown output type")
            if output_type != "computer_call":
                continue
            computer_call = _strict_object_mapping(item)
            _require_mapping_keys(computer_call, _COMPUTER_CALL_FIELDS)
            if computer_call.get("type") != "computer_call":
                raise ValueError("Computer call type is invalid")
            if computer_call.get("status") != "completed":
                raise ValueError("Computer call status is not completed")
            item_id = computer_call.get("id")
            if item_id is not None:
                _require_provider_identifier(_required_str(item_id))
            if computer_call.get("action") is not None:
                raise ValueError("Singular computer action is not accepted")
            _validate_pending_safety_checks(computer_call)
            call_id = _required_str(computer_call.get("call_id"))
            _require_provider_identifier(call_id)
            raw_actions = _bounded_sequence(
                computer_call.get("actions"),
                maximum=MAX_COMPUTER_ACTIONS,
                label="computer call actions",
            )
            actions = tuple(_normalize_action(action) for action in raw_actions)
            calls.append(
                ComputerCall(
                    response_id=response_id,
                    call_id=call_id,
                    actions=actions,
                )
            )
        if len(calls) > 1:
            raise ValueError("A response may contain at most one computer call")
        return ComputerTurn(
            response_id=response_id,
            computer_call=calls[0] if calls else None,
        )
    except (TypeError, ValueError):
        raise InvalidComputerResponseError from None


def _normalize_action(value: object) -> ComputerAction:
    action = _strict_object_mapping(value)
    action_type = action.get("type")
    if action_type == "click":
        _require_mapping_keys(action, {"type", "x", "y", "button", "keys"})
        if "button" not in action:
            raise ValueError("Click action omitted its button")
        return ClickAction(
            x=_required_int(action.get("x")),
            y=_required_int(action.get("y")),
            button=_button(action),
            keys=_keys(action, required=False, maximum=_MAX_MODIFIER_KEYS),
        )
    if action_type == "double_click":
        _require_mapping_keys(action, {"type", "x", "y", "keys"})
        return DoubleClickAction(
            x=_required_int(action.get("x")),
            y=_required_int(action.get("y")),
            keys=_keys(action, required=False, maximum=_MAX_MODIFIER_KEYS),
        )
    if action_type == "drag":
        _require_mapping_keys(action, {"type", "path", "keys"})
        raw_path = _bounded_sequence(
            action.get("path"),
            maximum=_MAX_DRAG_POINTS,
            minimum=2,
            label="drag path",
        )
        return DragAction(
            path=tuple(_point(point) for point in raw_path),
            keys=_keys(action, required=False, maximum=_MAX_MODIFIER_KEYS),
        )
    if action_type == "move":
        _require_mapping_keys(action, {"type", "x", "y", "keys"})
        return MoveAction(
            x=_required_int(action.get("x")),
            y=_required_int(action.get("y")),
            keys=_keys(action, required=False, maximum=_MAX_MODIFIER_KEYS),
        )
    if action_type == "scroll":
        _require_mapping_keys(
            action,
            {"type", "x", "y", "scroll_x", "scroll_y", "keys"},
        )
        return ScrollAction(
            x=_required_int(action.get("x")),
            y=_required_int(action.get("y")),
            scroll_x=_required_int(action.get("scroll_x")),
            scroll_y=_required_int(action.get("scroll_y")),
            keys=_keys(action, required=False, maximum=_MAX_MODIFIER_KEYS),
        )
    if action_type == "keypress":
        _require_mapping_keys(action, {"type", "keys"})
        return KeypressAction(
            keys=_keys(action, required=True, maximum=_MAX_KEYPRESS_KEYS)
        )
    if action_type == "type":
        _require_mapping_keys(action, {"type", "text"})
        return TypeAction(text=_required_str(action.get("text")))
    if action_type == "wait":
        _require_mapping_keys(action, {"type"})
        return WaitAction()
    if action_type == "screenshot":
        _require_mapping_keys(action, {"type"})
        return ScreenshotAction()
    raise ValueError("Unknown computer action")


def _point(value: object) -> Point:
    if _is_sequence(value):
        if len(value) != 2:
            raise ValueError("A tuple point must contain two coordinates")
        return Point(x=_required_int(value[0]), y=_required_int(value[1]))
    point = _strict_object_mapping(value)
    _require_mapping_keys(point, {"x", "y"})
    return Point(
        x=_required_int(point.get("x")),
        y=_required_int(point.get("y")),
    )


def _button(value: Mapping[object, object]) -> MouseButton:
    raw = value.get("button")
    if raw == "wheel":
        return "middle"
    if raw not in {"left", "right"}:
        raise ValueError("Unknown mouse button")
    return cast(MouseButton, raw)


def _keys(
    value: Mapping[object, object],
    *,
    required: bool,
    maximum: int,
) -> tuple[str, ...]:
    raw = value.get("keys")
    if raw is None and not required:
        return ()
    keys_sequence = _bounded_sequence(
        raw,
        maximum=maximum,
        minimum=1 if required else 0,
        label="action keys",
    )
    keys = tuple(_required_str(key) for key in keys_sequence)
    if required and not keys:
        raise ValueError("Keypress actions require keys")
    return keys


def _require_mapping_keys(value: object, allowed: set[str]) -> None:
    if not isinstance(value, Mapping):
        return
    if len(value) > len(allowed) or any(
        type(key) is not str or key not in allowed for key in value
    ):
        raise ValueError("Action mapping contains an unknown member")


def _strict_object_mapping(value: object) -> Mapping[object, object]:
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if not callable(dump):
        raise ValueError("Action must be a mapping or typed SDK model")
    dumped = cast(Callable[..., object], dump)(mode="python", exclude_none=True)
    if not isinstance(dumped, Mapping):
        raise ValueError("SDK action did not dump to a mapping")
    return dumped


def _is_sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray)


def _bounded_sequence(
    value: object,
    *,
    maximum: int,
    label: str,
    minimum: int = 1,
) -> Sequence[object]:
    if not _is_sequence(value) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{label} is outside its closed size bounds")
    return value


def _validate_pending_safety_checks(computer_call: Mapping[object, object]) -> None:
    if "pending_safety_checks" not in computer_call:
        raise ValueError("Computer call omitted pending safety checks")
    checks = _bounded_sequence(
        computer_call.get("pending_safety_checks"),
        maximum=_MAX_PENDING_SAFETY_CHECKS,
        minimum=0,
        label="pending safety checks",
    )
    for raw_check in checks:
        check = _strict_object_mapping(raw_check)
        _require_mapping_keys(check, {"id", "code", "message"})
        _require_provider_identifier(_required_str(check.get("id")))
        for optional_name in ("code", "message"):
            optional_value = check.get(optional_name)
            if optional_value is not None and (
                type(optional_value) is not str or len(optional_value) > 1_000
            ):
                raise ValueError("Pending safety check member is invalid")
    if checks:
        raise PendingComputerSafetyCheckError


def _required_int(value: object) -> int:
    if type(value) is not int:
        raise ValueError("Expected a strict integer")
    return value


def _required_str(value: object) -> str:
    if type(value) is not str:
        raise ValueError("Expected a strict string")
    return value


def _require_provider_identifier(value: str) -> None:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 256
        or not value.isascii()
        or any(not (character.isalnum() or character in "._:-") for character in value)
    ):
        raise ValueError("Provider identifier is invalid")


def _require_task(task: str) -> None:
    if (
        type(task) is not str
        or not 1 <= len(task) <= 16_000
        or not task.strip()
    ):
        raise ValueError("Computer task is invalid")


def _require_timeout(timeout_seconds: float) -> None:
    if type(timeout_seconds) not in {int, float} or not 0.001 <= float(timeout_seconds) <= 90:
        raise ValueError("Computer provider timeout is invalid")


def _require_screenshot(screenshot_png: bytes) -> None:
    if (
        type(screenshot_png) is not bytes
        or not len(_PNG_SIGNATURE) < len(screenshot_png) <= MAX_SCREENSHOT_BYTES
        or not screenshot_png.startswith(_PNG_SIGNATURE)
    ):
        raise ValueError("Computer screenshot must be a bounded PNG")
