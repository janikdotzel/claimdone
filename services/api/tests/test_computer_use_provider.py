"""Deterministic GA Responses adapter tests; no provider calls leave the process."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import pytest
from openai.types.responses import ResponseComputerToolCall

from claimdone_api.computer_use import (
    ClickAction,
    InvalidComputerResponseError,
    MoveAction,
    PendingComputerSafetyCheckError,
    ResponsesComputerUseAdapter,
    ScreenshotAction,
    TypeAction,
)

PNG = b"\x89PNG\r\n\x1a\nmock"


class FakeResponses:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self.responses.pop(0)


@dataclass
class SDKAction:
    payload: dict[str, object]

    def __getattr__(self, name: str) -> object:
        return self.payload.get(name)

    def model_dump(self, **kwargs: object) -> object:
        assert kwargs == {"mode": "python", "exclude_none": True}
        return self.payload


def computer_response(
    *,
    response_id: str = "resp_001",
    call_id: str = "call_001",
    actions: list[object] | None = None,
    response_status: str = "completed",
    call_status: str = "completed",
) -> dict[str, object]:
    return {
        "id": response_id,
        "status": response_status,
        "output": [
            {
                "type": "computer_call",
                "call_id": call_id,
                "pending_safety_checks": [],
                "status": call_status,
                "actions": actions if actions is not None else [{"type": "screenshot"}],
            }
        ],
    }


def computer_output_item(call_id: str) -> object:
    output = computer_response(call_id=call_id)["output"]
    assert isinstance(output, list)
    return output[0]


def test_emits_exact_ga_start_and_original_screenshot_followup_shapes() -> None:
    raw = FakeResponses(
        computer_response(),
        {
            "id": "resp_002",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "status": "completed",
                    "content": [],
                }
            ],
        },
    )
    adapter = ResponsesComputerUseAdapter(raw)

    first = adapter.start("Fill the sandbox draft.", timeout_seconds=12)
    assert first.computer_call is not None
    final = adapter.continue_with_screenshot(
        previous_response_id=first.response_id,
        call_id=first.computer_call.call_id,
        screenshot_png=PNG,
        timeout_seconds=11,
    )

    assert isinstance(first.computer_call.actions[0], ScreenshotAction)
    assert final.computer_call is None
    assert raw.calls[0] == {
        "model": "gpt-5.6",
        "tools": [{"type": "computer"}],
        "input": "Fill the sandbox draft.",
        "timeout": 12.0,
    }
    assert set(raw.calls[1]) == {
        "model",
        "tools",
        "previous_response_id",
        "input",
        "timeout",
    }
    assert raw.calls[1]["model"] == "gpt-5.6"
    assert raw.calls[1]["tools"] == [{"type": "computer"}]
    assert raw.calls[1]["previous_response_id"] == "resp_001"
    output = cast(list[dict[str, object]], raw.calls[1]["input"])
    assert output[0]["type"] == "computer_call_output"
    assert output[0]["call_id"] == "call_001"
    screenshot = cast(dict[str, str], output[0]["output"])
    assert screenshot["type"] == "computer_screenshot"
    assert screenshot["detail"] == "original"
    assert screenshot["image_url"].startswith("data:image/png;base64,")
    assert "computer_use_preview" not in repr(raw.calls)
    assert "truncation" not in raw.calls[0]


def test_normalizes_all_actions_in_provider_order_and_sdk_models_strictly() -> None:
    raw = FakeResponses(
        computer_response(
            actions=[
                SDKAction({"type": "screenshot"}),
                SDKAction(
                    {"type": "click", "x": 10, "y": 11, "button": "left", "keys": []}
                ),
                SDKAction({"type": "move", "x": 12, "y": 13, "keys": ["SHIFT"]}),
                SDKAction({"type": "type", "text": "private mock value"}),
            ]
        )
    )

    turn = ResponsesComputerUseAdapter(raw).start("task", timeout_seconds=10)

    assert turn.computer_call is not None
    assert tuple(action.kind for action in turn.computer_call.actions) == (
        "screenshot",
        "click",
        "move",
        "type",
    )
    assert isinstance(turn.computer_call.actions[1], ClickAction)
    assert isinstance(turn.computer_call.actions[2], MoveAction)
    assert "private mock value" not in repr(turn)
    assert "private mock value" not in repr(turn.computer_call.actions[3])


def test_normalizes_typed_sdk_computer_call_with_exact_known_fields() -> None:
    sdk_call = ResponseComputerToolCall.model_validate(
        {
            "id": "cu_001",
            "type": "computer_call",
            "call_id": "call_001",
            "pending_safety_checks": [],
            "status": "completed",
            "actions": [
                {"type": "screenshot"},
                {"type": "click", "x": 10, "y": 11, "button": "wheel"},
                {"type": "double_click", "x": 12, "y": 13},
                {
                    "type": "scroll",
                    "x": 14,
                    "y": 15,
                    "scroll_x": 0,
                    "scroll_y": 100,
                },
            ],
        }
    )
    raw = FakeResponses(
        {
            "id": "resp_001",
            "status": "completed",
            "output": [sdk_call],
        }
    )

    turn = ResponsesComputerUseAdapter(raw).start("task", timeout_seconds=10)

    assert turn.computer_call is not None
    assert isinstance(turn.computer_call.actions[0], ScreenshotAction)
    assert isinstance(turn.computer_call.actions[1], ClickAction)
    assert turn.computer_call.actions[1].button == "middle"
    assert tuple(action.kind for action in turn.computer_call.actions) == (
        "screenshot",
        "click",
        "double_click",
        "scroll",
    )


@pytest.mark.parametrize("button", ["back", "forward"])
def test_typed_sdk_navigation_sensitive_click_buttons_fail_closed(button: str) -> None:
    sdk_call = ResponseComputerToolCall.model_validate(
        {
            "id": "cu_001",
            "type": "computer_call",
            "call_id": "call_001",
            "pending_safety_checks": [],
            "status": "completed",
            "actions": [
                {"type": "click", "x": 10, "y": 11, "button": button},
            ],
        }
    )

    with pytest.raises(InvalidComputerResponseError):
        ResponsesComputerUseAdapter(
            FakeResponses(
                {
                    "id": "resp_001",
                    "status": "completed",
                    "output": [sdk_call],
                }
            )
        ).start("task", timeout_seconds=10)


@pytest.mark.parametrize(
    "response",
    [
        computer_response(actions=[{"type": "wait"}] * 41),
        {
            "id": "resp_001",
            "status": "completed",
            "output": [{"type": "message"}] * 17,
        },
        computer_response(
            actions=[
                {
                    "type": "drag",
                    "path": [{"x": 1, "y": 1}] * 129,
                }
            ]
        ),
        computer_response(
            actions=[{"type": "keypress", "keys": ["TAB"] * 9}]
        ),
        computer_response(
            actions=[
                {
                    "type": "click",
                    "x": 1,
                    "y": 1,
                    "button": "left",
                    "keys": ["SHIFT"] * 5,
                }
            ]
        ),
    ],
)
def test_rejects_oversize_provider_sequences_before_normalization(
    response: object,
) -> None:
    with pytest.raises(InvalidComputerResponseError):
        ResponsesComputerUseAdapter(FakeResponses(response)).start(
            "task", timeout_seconds=10
        )


def test_nonempty_pending_safety_checks_fail_closed_without_retaining_content() -> None:
    sdk_call = ResponseComputerToolCall.model_validate(
        {
            "id": "cu_001",
            "type": "computer_call",
            "call_id": "call_001",
            "pending_safety_checks": [
                {
                    "id": "safety_001",
                    "code": "confirmation_required",
                    "message": "private provider safety detail",
                }
            ],
            "status": "completed",
            "actions": [{"type": "screenshot"}],
        }
    )
    response = {
        "id": "resp_001",
        "status": "completed",
        "output": [sdk_call],
    }

    with pytest.raises(PendingComputerSafetyCheckError) as captured:
        ResponsesComputerUseAdapter(FakeResponses(response)).start(
            "task", timeout_seconds=10
        )

    assert "private provider safety detail" not in repr(captured.value)


@pytest.mark.parametrize(
    "response",
    [
        computer_response(response_status="failed"),
        computer_response(call_status="in_progress"),
        computer_response(call_id=""),
        computer_response(call_id="bad call"),
        computer_response(actions=[]),
        computer_response(actions=[{"type": "unknown"}]),
        computer_response(actions=[{"type": "click", "x": True, "y": 1}]),
        computer_response(
            actions=[{"type": "click", "x": 1, "y": 1, "button": "middle"}]
        ),
        computer_response(
            actions=[{"type": "click", "x": 1, "y": 1, "button": "back"}]
        ),
        computer_response(
            actions=[
                {"type": "double_click", "x": 1, "y": 1, "button": "left"}
            ]
        ),
        computer_response(
            actions=[
                {
                    "type": "scroll",
                    "x": 1,
                    "y": 1,
                    "scrollX": 0,
                    "scrollY": 100,
                }
            ]
        ),
        computer_response(actions=[{"type": "click", "x": 1, "y": 1, "extra": 1}]),
        computer_response(actions=[SDKAction({"type": "wait", "extra": "unknown"})]),
        {
            "id": "resp_001",
            "status": "completed",
            "output": [
                {
                    **cast(dict[str, object], computer_output_item("call_001")),
                    "unknown": True,
                }
            ],
        },
        {
            "id": "resp_001",
            "status": "completed",
            "output": [
                {
                    **cast(dict[str, object], computer_output_item("call_001")),
                    "action": {"type": "screenshot"},
                }
            ],
        },
        {
            "id": "resp_001",
            "status": "completed",
            "output": [
                {
                    "type": "computer_call",
                    "call_id": "call_001",
                    "status": "completed",
                    "actions": [{"type": "screenshot"}],
                }
            ],
        },
        {
            "id": "resp_001",
            "status": "completed",
            "output": [
                {
                    **cast(dict[str, object], computer_output_item("call_001")),
                    "pending_safety_checks": [
                        {"id": "safety_001", "unknown": "field"}
                    ],
                }
            ],
        },
        computer_response(actions=[{"type": "click", "x": 1, "y": 1, "keys": ["ENTER"]}]),
        {
            "id": "resp_001",
            "status": "completed",
            "output": [{"type": "unknown_output"}],
        },
        {
            "id": "resp_001",
            "status": "completed",
            "output": [
                computer_output_item("call_001"),
                computer_output_item("call_002"),
            ],
        },
    ],
)
def test_ambiguous_or_unknown_provider_forms_fail_closed(response: object) -> None:
    with pytest.raises(InvalidComputerResponseError):
        ResponsesComputerUseAdapter(FakeResponses(response)).start("task", timeout_seconds=10)


@pytest.mark.parametrize(
    "screenshot",
    [
        b"not-png",
        b"\x89PNG\r\n\x1a\n",
        b"\x89PNG\r\n\x1a\n" + b"x" * (8 * 1024 * 1024),
    ],
)
def test_rejects_invalid_screenshot_before_provider_call(screenshot: bytes) -> None:
    raw = FakeResponses(computer_response())
    adapter = ResponsesComputerUseAdapter(raw)

    with pytest.raises(ValueError):
        adapter.continue_with_screenshot(
            previous_response_id="resp_001",
            call_id="call_001",
            screenshot_png=screenshot,
            timeout_seconds=10,
        )

    assert raw.calls == []


@pytest.mark.parametrize("task", [" " * 16_000, "x" + " " * 16_000])
def test_rejects_whitespace_only_or_raw_oversize_task_before_provider_call(
    task: str,
) -> None:
    raw = FakeResponses(computer_response())

    with pytest.raises(ValueError):
        ResponsesComputerUseAdapter(raw).start(task, timeout_seconds=10)

    assert raw.calls == []
    assert task not in repr(raw.calls)


def test_action_repr_never_contains_text_or_coordinates() -> None:
    assert "secret-value" not in repr(TypeAction("secret-value"))
    assert "912" not in repr(ClickAction(x=912, y=713))
