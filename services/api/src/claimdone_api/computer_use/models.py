"""Closed CU-001 models for the bounded local computer-use runner."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar, Literal

from claimdone_api.contracts import ProviderFailure

MAX_COMPUTER_ACTIONS = 40
MAX_RUN_SECONDS = 90.0
DEFAULT_PORTAL_ORIGIN = "http://127.0.0.1:3000"
DEFAULT_VIEWPORT_WIDTH = 1440
DEFAULT_VIEWPORT_HEIGHT = 900

MouseButton = Literal["left", "middle", "right"]


class ComputerUseBlockReason(StrEnum):
    """Content-free terminal reasons safe for workflow events and logs."""

    ACTION_LIMIT_EXCEEDED = "action_limit_exceeded"
    APPROVAL_ACTION_BLOCKED = "approval_action_blocked"
    BROWSER_FAILURE = "browser_failure"
    BROWSER_TIMEOUT = "browser_timeout"
    CASE_NOT_RUNNABLE = "case_not_runnable"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    DOWNLOAD_BLOCKED = "download_blocked"
    INITIAL_SCREENSHOT_REQUIRED = "initial_screenshot_required"
    INVALID_PROVIDER_RESPONSE = "invalid_provider_response"
    NAVIGATION_NOT_ALLOWED = "navigation_not_allowed"
    NETWORK_CAPABILITY_BLOCKED = "network_capability_blocked"
    FILE_ACCESS_BLOCKED = "file_access_blocked"
    PERMISSION_REQUEST_BLOCKED = "permission_request_blocked"
    POPUP_BLOCKED = "popup_blocked"
    PORTAL_REVIEW_SIGNAL_INVALID = "portal_review_signal_invalid"
    PROVIDER_COMPLETED_BEFORE_PORTAL_REVIEW = "provider_completed_before_portal_review"
    PROVIDER_FAILURE = "provider_failure"
    PROVIDER_SAFETY_CHECK_BLOCKED = "provider_safety_check_blocked"
    UNSUPPORTED_ACTION = "unsupported_action"


class ComputerUseRunStatus(StrEnum):
    """CU-owned outcome; never a claim about the persisted backend case state."""

    BLOCKED = "blocked"
    PORTAL_REVIEW_REACHED = "portal_review_reached"


@dataclass(frozen=True, slots=True, repr=False)
class Point:
    """One integer screen coordinate in the model's native viewport."""

    x: int
    y: int

    def __post_init__(self) -> None:
        _require_coordinate(self.x, "x")
        _require_coordinate(self.y, "y")


@dataclass(frozen=True, slots=True, repr=False)
class ClickAction:
    kind: ClassVar[Literal["click"]] = "click"
    x: int
    y: int
    button: MouseButton = "left"
    keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_pointer(self.x, self.y, self.button, self.keys)


@dataclass(frozen=True, slots=True, repr=False)
class DoubleClickAction:
    kind: ClassVar[Literal["double_click"]] = "double_click"
    x: int
    y: int
    button: MouseButton = "left"
    keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_pointer(self.x, self.y, self.button, self.keys)


@dataclass(frozen=True, slots=True, repr=False)
class DragAction:
    kind: ClassVar[Literal["drag"]] = "drag"
    path: tuple[Point, ...]
    keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.path) < 2 or len(self.path) > 128:
            raise ValueError("A drag action requires 2 through 128 points")
        _validate_modifier_keys(self.keys)


@dataclass(frozen=True, slots=True, repr=False)
class MoveAction:
    kind: ClassVar[Literal["move"]] = "move"
    x: int
    y: int
    keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_coordinate(self.x, "x")
        _require_coordinate(self.y, "y")
        _validate_modifier_keys(self.keys)


@dataclass(frozen=True, slots=True, repr=False)
class ScrollAction:
    kind: ClassVar[Literal["scroll"]] = "scroll"
    x: int
    y: int
    scroll_x: int
    scroll_y: int
    keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_coordinate(self.x, "x")
        _require_coordinate(self.y, "y")
        _require_delta(self.scroll_x, "scrollX")
        _require_delta(self.scroll_y, "scrollY")
        _validate_modifier_keys(self.keys)


@dataclass(frozen=True, slots=True, repr=False)
class KeypressAction:
    kind: ClassVar[Literal["keypress"]] = "keypress"
    keys: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.keys:
            raise ValueError("A keypress action requires at least one key")
        _validate_keys(self.keys)


@dataclass(frozen=True, slots=True, repr=False)
class TypeAction:
    kind: ClassVar[Literal["type"]] = "type"
    text: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.text) is not str
            or not 1 <= len(self.text) <= 4_000
            or any(
                not character.isprintable() and character not in {"\n", "\t"}
                for character in self.text
            )
        ):
            raise ValueError("A type action requires 1 through 4000 characters")


@dataclass(frozen=True, slots=True, repr=False)
class WaitAction:
    kind: ClassVar[Literal["wait"]] = "wait"


@dataclass(frozen=True, slots=True, repr=False)
class ScreenshotAction:
    kind: ClassVar[Literal["screenshot"]] = "screenshot"


type ComputerAction = (
    ClickAction
    | DoubleClickAction
    | DragAction
    | MoveAction
    | ScrollAction
    | KeypressAction
    | TypeAction
    | WaitAction
    | ScreenshotAction
)


@dataclass(frozen=True, slots=True)
class ComputerCall:
    """One GA computer call whose actions retain provider order exactly."""

    response_id: str
    call_id: str
    actions: tuple[ComputerAction, ...] = field(repr=False)

    def __post_init__(self) -> None:
        _require_identifier(self.response_id, "responseId")
        _require_identifier(self.call_id, "callId")
        if not self.actions:
            raise ValueError("A computer call requires at least one action")


@dataclass(frozen=True, slots=True)
class ComputerTurn:
    """Sanitized provider turn; non-computer output is deliberately discarded."""

    response_id: str
    computer_call: ComputerCall | None

    def __post_init__(self) -> None:
        _require_identifier(self.response_id, "responseId")
        if self.computer_call is not None and self.computer_call.response_id != self.response_id:
            raise ValueError("Computer call and turn response IDs must match")


@dataclass(frozen=True, slots=True)
class ComputerUseRunRequest:
    """Ephemeral case task; prompt contents are excluded from repr and results."""

    case_id: str
    task: str = field(repr=False)

    def __post_init__(self) -> None:
        _require_identifier(self.case_id, "caseId", maximum=128)
        if (
            type(self.task) is not str
            or not 1 <= len(self.task) <= 12_000
            or not self.task.strip()
        ):
            raise ValueError("task must be 1 through 12000 characters and contain non-whitespace")


@dataclass(frozen=True, slots=True)
class ComputerUseConfig:
    """Immutable bounds; no caller may configure beyond the CU-001 limits."""

    portal_origin: str = DEFAULT_PORTAL_ORIGIN
    deadline_seconds: float = MAX_RUN_SECONDS
    browser_launch_timeout_seconds: float = 15.0
    navigation_timeout_seconds: float = 10.0
    action_timeout_seconds: float = 5.0
    screenshot_timeout_seconds: float = 5.0
    provider_timeout_seconds: float = 30.0
    wait_action_seconds: float = 2.0
    viewport_width: int = DEFAULT_VIEWPORT_WIDTH
    viewport_height: int = DEFAULT_VIEWPORT_HEIGHT

    def __post_init__(self) -> None:
        if type(self.portal_origin) is not str or not self.portal_origin:
            raise ValueError("portalOrigin must be a non-empty string")
        _require_seconds(self.deadline_seconds, "deadlineSeconds", maximum=MAX_RUN_SECONDS)
        for label, value in (
            ("browserLaunchTimeoutSeconds", self.browser_launch_timeout_seconds),
            ("navigationTimeoutSeconds", self.navigation_timeout_seconds),
            ("actionTimeoutSeconds", self.action_timeout_seconds),
            ("screenshotTimeoutSeconds", self.screenshot_timeout_seconds),
            ("providerTimeoutSeconds", self.provider_timeout_seconds),
            ("waitActionSeconds", self.wait_action_seconds),
        ):
            _require_seconds(value, label, maximum=MAX_RUN_SECONDS)
        if (
            type(self.viewport_width) is not int
            or not 640 <= self.viewport_width <= 1_920
            or type(self.viewport_height) is not int
            or not 480 <= self.viewport_height <= 1_200
        ):
            raise ValueError("viewport must remain within the bounded desktop range")


@dataclass(frozen=True, slots=True)
class ComputerUseRunResult:
    """Terminal CU outcome containing no backend transition or private content."""

    case_id: str
    status: ComputerUseRunStatus
    actions_executed: int
    response_turns: int
    final_response_id: str | None
    block_reason: ComputerUseBlockReason | None
    provider_failure: ProviderFailure | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.case_id, "caseId", maximum=128)
        if not isinstance(self.status, ComputerUseRunStatus):
            raise ValueError("Computer use requires a closed runner-owned status")
        if type(self.actions_executed) is not int or not 0 <= self.actions_executed <= 40:
            raise ValueError("actionsExecuted must be an integer from 0 through 40")
        if type(self.response_turns) is not int or self.response_turns < 0:
            raise ValueError("responseTurns must be a non-negative integer")
        if self.final_response_id is not None:
            _require_identifier(self.final_response_id, "finalResponseId")
        if self.status is ComputerUseRunStatus.PORTAL_REVIEW_REACHED:
            if self.block_reason is not None or self.provider_failure is not None:
                raise ValueError("A portal-review result cannot contain a block")
        elif self.block_reason is None:
            raise ValueError("A blocked result requires a reason")
        if (self.provider_failure is not None) is (
            self.block_reason is ComputerUseBlockReason.PROVIDER_FAILURE
        ):
            return
        raise ValueError("providerFailure is allowed exactly for provider_failure blocks")


def _validate_pointer(
    x: int,
    y: int,
    button: MouseButton,
    keys: tuple[str, ...],
) -> None:
    _require_coordinate(x, "x")
    _require_coordinate(y, "y")
    if button not in {"left", "middle", "right"}:
        raise ValueError("button must be left, middle, or right")
    _validate_modifier_keys(keys)


def _validate_modifier_keys(keys: tuple[str, ...]) -> None:
    if (
        type(keys) is not tuple
        or len(keys) > 4
        or len(set(keys)) != len(keys)
        or any(key not in {"ALT", "CTRL", "META", "SHIFT"} for key in keys)
    ):
        raise ValueError("Mouse action keys may contain only unique keyboard modifiers")


def validate_action_batch(
    actions: tuple[ComputerAction, ...],
    *,
    viewport_width: int,
    viewport_height: int,
) -> None:
    """Validate a complete provider batch before any action in it can execute."""

    if type(actions) is not tuple or not actions or len(actions) > MAX_COMPUTER_ACTIONS:
        raise ValueError("Computer action batch is empty or exceeds the hard limit")
    if type(viewport_width) is not int or type(viewport_height) is not int:
        raise ValueError("Viewport bounds must be strict integers")

    def require_point(point: Point) -> None:
        if not 0 <= point.x < viewport_width or not 0 <= point.y < viewport_height:
            raise ValueError("Computer action coordinate falls outside the viewport")

    for action in actions:
        if isinstance(action, ClickAction | DoubleClickAction | MoveAction | ScrollAction):
            require_point(Point(action.x, action.y))
        elif isinstance(action, DragAction):
            for point in action.path:
                require_point(point)
        elif not isinstance(action, KeypressAction | TypeAction | WaitAction | ScreenshotAction):
            raise ValueError("Computer action has an unknown runtime type")


def _validate_keys(keys: tuple[str, ...]) -> None:
    if type(keys) is not tuple or len(keys) > 8:
        raise ValueError("keys must be a tuple containing at most eight values")
    special_keys = {
        "ALT",
        "ARROWDOWN",
        "ARROWLEFT",
        "ARROWRIGHT",
        "ARROWUP",
        "BACKSPACE",
        "CTRL",
        "DEL",
        "DELETE",
        "END",
        "ENTER",
        "ESC",
        "ESCAPE",
        "HOME",
        "INSERT",
        "META",
        "PAGEDOWN",
        "PAGEUP",
        "RETURN",
        "SHIFT",
        "SPACE",
        "TAB",
    }
    for key in keys:
        single_printable = (
            type(key) is str and len(key) == 1 and key.isascii() and key.isprintable()
        )
        if not single_printable and key not in special_keys:
            raise ValueError("Every key must be a known special key or printable ASCII character")


def _require_coordinate(value: int, label: str) -> None:
    if type(value) is not int or not 0 <= value <= 10_000:
        raise ValueError(f"{label} must be an integer from 0 through 10000")


def _require_delta(value: int, label: str) -> None:
    if type(value) is not int or not -100_000 <= value <= 100_000:
        raise ValueError(f"{label} must be an integer from -100000 through 100000")


def _require_identifier(value: str, label: str, *, maximum: int = 256) -> None:
    if (
        type(value) is not str
        or not 1 <= len(value) <= maximum
        or not value.isascii()
        or any(character.isspace() for character in value)
    ):
        raise ValueError(f"{label} must be a bounded ASCII identifier")


def _require_seconds(value: float, label: str, *, maximum: float) -> None:
    if type(value) not in {int, float} or not 0.001 <= float(value) <= maximum:
        raise ValueError(f"{label} must be between 0.001 and {maximum:g}")
