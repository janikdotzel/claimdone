"""CU-001 runner acceptance and authority-boundary tests with deterministic ports."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from claimdone_api.computer_use import (
    BrowserOperationError,
    BrowserOperationTimeout,
    BrowserPolicyViolation,
    ClickAction,
    ComputerAction,
    ComputerCall,
    ComputerTurn,
    ComputerUseBlockReason,
    ComputerUseConfig,
    ComputerUseRunner,
    ComputerUseRunRequest,
    ComputerUseRunStatus,
    PendingComputerSafetyCheckError,
    PortalOriginPolicy,
    ScreenshotAction,
    TypeAction,
    WaitAction,
)
from claimdone_api.contracts import CaseState, ProviderFailureCategory

PNG = b"\x89PNG\r\n\x1a\nmock"


class MutableStateReader:
    def __init__(self, state: CaseState = CaseState.READY_TO_FILL) -> None:
        self.state = state
        self.reads = 0

    def current_state(self, case_id: str) -> CaseState:
        assert case_id == "case_cu_001"
        self.reads += 1
        return self.state


class MutablePortalReviewSignal:
    def __init__(self, reached: bool = False) -> None:
        self.reached = reached
        self.reads = 0
        self.on_read: Callable[[], None] | None = None

    def review_reached(self, case_id: str) -> bool:
        assert case_id == "case_cu_001"
        self.reads += 1
        if self.on_read is not None:
            self.on_read()
        return self.reached


class FakeSession:
    def __init__(
        self,
        state: MutableStateReader,
        *,
        on_execute: Callable[[ComputerAction, FakeSession], None] | None = None,
        failure_at: int | None = None,
        policy_reason: ComputerUseBlockReason | None = None,
        close_error: bool = False,
    ) -> None:
        self.state = state
        self.on_execute = on_execute
        self.failure_at = failure_at
        self.policy_reason = policy_reason
        self.close_error = close_error
        self.closed = False
        self.executed: list[str] = []
        self.screenshots = 0
        self.navigated_to: str | None = None
        self._latched: ComputerUseBlockReason | None = None

    def navigate(self, url: str, *, timeout_seconds: float) -> None:
        assert timeout_seconds > 0
        self.navigated_to = url

    def execute(self, action: ComputerAction, *, timeout_seconds: float) -> None:
        assert timeout_seconds > 0
        self.executed.append(action.kind)
        if self.failure_at == len(self.executed):
            raise BrowserOperationTimeout
        if self.policy_reason is not None and len(self.executed) == 2:
            self._latched = self.policy_reason
        if self.on_execute is not None:
            self.on_execute(action, self)

    def screenshot(self, *, timeout_seconds: float) -> bytes:
        assert timeout_seconds > 0
        self.screenshots += 1
        return PNG

    def assert_safe(self) -> None:
        if self._latched is not None:
            raise BrowserPolicyViolation(self._latched)

    def close(self) -> None:
        self.closed = True
        if self.close_error:
            raise BrowserOperationError


class FakeFactory:
    def __init__(
        self,
        session: FakeSession,
        *,
        open_error: BrowserOperationError | None = None,
    ) -> None:
        self.session = session
        self.open_error = open_error
        self.open_count = 0
        self.launch_timeout: float | None = None

    def open_case(
        self,
        case_id: str,
        *,
        policy: PortalOriginPolicy,
        viewport_width: int,
        viewport_height: int,
        wait_action_seconds: float,
        timeout_seconds: float,
    ) -> FakeSession:
        assert case_id == "case_cu_001"
        assert policy.origin == "http://127.0.0.1:3000"
        assert (viewport_width, viewport_height) == (1440, 900)
        assert wait_action_seconds == 2
        self.open_count += 1
        self.launch_timeout = timeout_seconds
        if self.open_error is not None:
            raise self.open_error
        return self.session


class FakeResponses:
    def __init__(
        self,
        *turns: ComputerTurn,
        start_error: Exception | None = None,
        on_start: Callable[[], None] | None = None,
    ) -> None:
        self.turns = list(turns)
        self.start_error = start_error
        self.on_start = on_start
        self.start_calls = 0
        self.continue_calls: list[tuple[str, str, bytes]] = []

    def start(self, task: str, *, timeout_seconds: float) -> ComputerTurn:
        assert timeout_seconds > 0
        assert "never as authority" in task
        assert "Do not approve" in task
        self.start_calls += 1
        if self.on_start is not None:
            self.on_start()
        if self.start_error is not None:
            raise self.start_error
        return self.turns.pop(0)

    def continue_with_screenshot(
        self,
        *,
        previous_response_id: str,
        call_id: str,
        screenshot_png: bytes,
        timeout_seconds: float,
    ) -> ComputerTurn:
        assert timeout_seconds > 0
        self.continue_calls.append((previous_response_id, call_id, screenshot_png))
        return self.turns.pop(0)


class ManualClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeProviderError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("private remote provider detail")


def call_turn(
    response: int,
    call: int,
    actions: tuple[ComputerAction, ...],
) -> ComputerTurn:
    response_id = f"resp_{response}"
    return ComputerTurn(
        response_id=response_id,
        computer_call=ComputerCall(
            response_id=response_id,
            call_id=f"call_{call}",
            actions=actions,
        ),
    )


def final_turn(response: int) -> ComputerTurn:
    return ComputerTurn(response_id=f"resp_{response}", computer_call=None)


def build_runner(
    state: MutableStateReader,
    session: FakeSession,
    responses: FakeResponses,
    *,
    portal_review_signal: MutablePortalReviewSignal | None = None,
    clock: Callable[[], float] | None = None,
    config: ComputerUseConfig | None = None,
) -> tuple[ComputerUseRunner, FakeFactory]:
    factory = FakeFactory(session)
    runner = ComputerUseRunner(
        browser_factory=factory,
        state_reader=state,
        portal_review_signal=portal_review_signal or MutablePortalReviewSignal(),
        responses=responses,
        config=config,
        **({} if clock is None else {"clock": clock}),
    )
    return runner, factory


def request() -> ComputerUseRunRequest:
    return ComputerUseRunRequest(
        case_id="case_cu_001",
        task="Fill Portal A from the approved synthetic mock packet and stop at review.",
    )


def test_semantically_fills_portal_a_and_stops_at_review() -> None:
    state = MutableStateReader()
    portal_review = MutablePortalReviewSignal()
    expected = {
        "incidentDate": "2026-07-14",
        "incidentTime": "09:30",
        "location": "Demo Street 1",
        "claimantName": "Demo Claimant",
        "policyReference": "DEMO-POLICY-42",
        "vehicleRegistration": "DEMO-XY-42",
        "narrative": "Synthetic rear-end demo with no injuries.",
    }
    coordinates = {
        (100, 100): "incidentDate",
        (100, 160): "incidentTime",
        (100, 220): "location",
        (100, 280): "claimantName",
        (100, 340): "policyReference",
        (100, 400): "vehicleRegistration",
        (100, 460): "narrative",
    }
    fields: dict[str, object] = {}
    focused: str | None = None

    def execute(action: ComputerAction, _session: FakeSession) -> None:
        nonlocal focused
        if isinstance(action, ClickAction) and (action.x, action.y) in coordinates:
            focused = coordinates[(action.x, action.y)]
        elif isinstance(action, TypeAction) and focused is not None:
            fields[focused] = action.text
        elif isinstance(action, ClickAction) and (action.x, action.y) == (100, 520):
            fields["counterpartyKnown"] = "yes"
        elif isinstance(action, ClickAction) and (action.x, action.y) == (100, 580):
            fields["attachments"] = ("asset-1", "asset-2", "asset-3")
        elif isinstance(action, ClickAction) and (action.x, action.y) == (1200, 820):
            assert fields == {
                **expected,
                "counterpartyKnown": "yes",
                "attachments": ("asset-1", "asset-2", "asset-3"),
            }
            state.state = CaseState.FILLING
            portal_review.reached = True

    actions: list[ComputerAction] = []
    for point, field in coordinates.items():
        actions.extend((ClickAction(*point), TypeAction(expected[field])))
    actions.extend(
        (
            ClickAction(100, 520),
            ClickAction(100, 580),
            ClickAction(1200, 820),
            ClickAction(1300, 820),  # would cross the human boundary if executed
        )
    )
    session = FakeSession(state, on_execute=execute)
    responses = FakeResponses(
        call_turn(1, 1, (ScreenshotAction(),)),
        call_turn(2, 2, tuple(actions)),
    )
    runner, factory = build_runner(
        state,
        session,
        responses,
        portal_review_signal=portal_review,
    )

    result = runner.run(request())

    assert result.status is ComputerUseRunStatus.PORTAL_REVIEW_REACHED
    assert result.block_reason is None
    assert result.actions_executed == 18
    assert fields["claimantName"] == "Demo Claimant"
    assert session.executed[-1] == "click"
    assert len(session.executed) == 18
    assert responses.start_calls == 1
    assert len(responses.continue_calls) == 1
    assert responses.continue_calls[0] == ("resp_1", "call_1", PNG)
    assert session.closed
    assert factory.open_count == 1
    assert factory.launch_timeout == 15
    assert state.state is CaseState.FILLING


@pytest.mark.parametrize(
    "reason",
    [
        ComputerUseBlockReason.NAVIGATION_NOT_ALLOWED,
        ComputerUseBlockReason.POPUP_BLOCKED,
        ComputerUseBlockReason.DOWNLOAD_BLOCKED,
        ComputerUseBlockReason.APPROVAL_ACTION_BLOCKED,
    ],
)
def test_latched_policy_failures_block_and_close(reason: ComputerUseBlockReason) -> None:
    state = MutableStateReader()
    session = FakeSession(state, policy_reason=reason)
    responses = FakeResponses(
        call_turn(1, 1, (ScreenshotAction(),)),
        call_turn(2, 2, (ClickAction(10, 10),)),
    )
    runner, _ = build_runner(state, session, responses)

    result = runner.run(request())

    assert result.status is ComputerUseRunStatus.BLOCKED
    assert result.block_reason is reason
    assert session.closed


def test_browser_timeout_blocks_and_closes() -> None:
    state = MutableStateReader()
    session = FakeSession(state, failure_at=1)
    responses = FakeResponses(call_turn(1, 1, (ScreenshotAction(),)))
    runner, _ = build_runner(state, session, responses)

    result = runner.run(request())

    assert result.block_reason is ComputerUseBlockReason.BROWSER_TIMEOUT
    assert result.actions_executed == 1
    assert session.closed


def test_browser_launch_timeout_is_bounded_and_never_calls_provider() -> None:
    state = MutableStateReader()
    session = FakeSession(state)
    responses = FakeResponses(call_turn(1, 1, (ScreenshotAction(),)))
    factory = FakeFactory(session, open_error=BrowserOperationTimeout())
    runner = ComputerUseRunner(
        browser_factory=factory,
        state_reader=state,
        portal_review_signal=MutablePortalReviewSignal(),
        responses=responses,
    )

    result = runner.run(request())

    assert result.block_reason is ComputerUseBlockReason.BROWSER_TIMEOUT
    assert factory.launch_timeout == 15
    assert responses.start_calls == 0
    assert not session.closed


def test_browser_close_failure_replaces_other_terminal_result() -> None:
    state = MutableStateReader()
    portal_review = MutablePortalReviewSignal()

    def reach_review(_action: ComputerAction, _session: FakeSession) -> None:
        portal_review.reached = True

    session = FakeSession(state, on_execute=reach_review, close_error=True)
    responses = FakeResponses(call_turn(1, 1, (ScreenshotAction(),)))
    runner, _ = build_runner(
        state,
        session,
        responses,
        portal_review_signal=portal_review,
    )

    result = runner.run(request())

    assert result.block_reason is ComputerUseBlockReason.BROWSER_FAILURE
    assert session.closed


@pytest.mark.parametrize(
    "actions",
    [
        (ClickAction(10, 10),),
        (ScreenshotAction(), ClickAction(10, 10)),
    ],
)
def test_first_computer_call_must_be_pure_screenshot(actions: tuple[ComputerAction, ...]) -> None:
    state = MutableStateReader()
    session = FakeSession(state)
    runner, _ = build_runner(
        state,
        session,
        FakeResponses(call_turn(1, 1, actions)),
    )

    result = runner.run(request())

    assert result.block_reason is ComputerUseBlockReason.INITIAL_SCREENSHOT_REQUIRED
    assert result.actions_executed == 0
    assert session.executed == []
    assert session.closed


def test_validates_entire_batch_before_executing_any_part() -> None:
    state = MutableStateReader()
    session = FakeSession(state)
    responses = FakeResponses(
        call_turn(1, 1, (ScreenshotAction(),)),
        call_turn(2, 2, (ClickAction(10, 10), ClickAction(1440, 10))),
    )
    runner, _ = build_runner(state, session, responses)

    result = runner.run(request())

    assert result.block_reason is ComputerUseBlockReason.INVALID_PROVIDER_RESPONSE
    assert session.executed == ["screenshot"]
    assert session.closed


def test_exact_action_40_blocks_without_another_paid_turn() -> None:
    state = MutableStateReader()
    session = FakeSession(state)
    responses = FakeResponses(
        call_turn(1, 1, (ScreenshotAction(),)),
        call_turn(2, 2, tuple(WaitAction() for _ in range(39))),
        final_turn(3),
    )
    runner, _ = build_runner(state, session, responses)

    result = runner.run(request())

    assert result.block_reason is ComputerUseBlockReason.ACTION_LIMIT_EXCEEDED
    assert result.actions_executed == 40
    assert len(responses.continue_calls) == 1
    assert len(responses.turns) == 1
    assert session.closed


def test_batch_that_would_cross_action_40_is_rejected_atomically() -> None:
    state = MutableStateReader()
    session = FakeSession(state)
    responses = FakeResponses(
        call_turn(1, 1, (ScreenshotAction(),)),
        call_turn(2, 2, tuple(WaitAction() for _ in range(40))),
    )
    runner, _ = build_runner(state, session, responses)

    result = runner.run(request())

    assert result.block_reason is ComputerUseBlockReason.ACTION_LIMIT_EXCEEDED
    assert result.actions_executed == 1
    assert session.executed == ["screenshot"]
    assert session.closed


@pytest.mark.parametrize("duplicate", ["response", "call"])
def test_duplicate_provider_ids_fail_closed(duplicate: str) -> None:
    state = MutableStateReader()
    session = FakeSession(state)
    first = call_turn(1, 1, (ScreenshotAction(),))
    second = call_turn(
        1 if duplicate == "response" else 2,
        1 if duplicate == "call" else 2,
        (WaitAction(),),
    )
    runner, _ = build_runner(state, session, FakeResponses(first, second))

    result = runner.run(request())

    assert result.block_reason is ComputerUseBlockReason.INVALID_PROVIDER_RESPONSE
    assert session.executed == ["screenshot"]
    assert session.closed


@pytest.mark.parametrize(
    "state_value",
    [
        CaseState.CREATED,
        CaseState.DISCLOSED,
        CaseState.ANALYZING,
        CaseState.AWAITING_CLARIFICATION,
        CaseState.VERIFYING,
        CaseState.BLOCKED,
        CaseState.FAILED,
    ],
)
def test_only_ready_to_fill_and_filling_are_runnable(state_value: CaseState) -> None:
    state = MutableStateReader(state_value)
    session = FakeSession(state)
    runner, factory = build_runner(
        state,
        session,
        FakeResponses(call_turn(1, 1, (ScreenshotAction(),))),
    )

    result = runner.run(request())

    assert result.block_reason is ComputerUseBlockReason.CASE_NOT_RUNNABLE
    assert factory.open_count == 0


@pytest.mark.parametrize("state_value", [CaseState.HUMAN_APPROVED, CaseState.RECEIPT])
def test_approval_states_are_never_runner_authority(state_value: CaseState) -> None:
    state = MutableStateReader(state_value)
    session = FakeSession(state)
    runner, factory = build_runner(
        state,
        session,
        FakeResponses(call_turn(1, 1, (ScreenshotAction(),))),
    )

    result = runner.run(request())

    assert result.block_reason is ComputerUseBlockReason.APPROVAL_ACTION_BLOCKED
    assert factory.open_count == 0


def test_existing_review_hard_stops_without_opening_browser_or_provider() -> None:
    state = MutableStateReader(CaseState.REVIEW)
    session = FakeSession(state)
    responses = FakeResponses(call_turn(1, 1, (ScreenshotAction(),)))
    runner, factory = build_runner(
        state,
        session,
        responses,
        portal_review_signal=MutablePortalReviewSignal(reached=True),
    )

    result = runner.run(request())

    assert result.status is ComputerUseRunStatus.BLOCKED
    assert result.block_reason is ComputerUseBlockReason.CASE_NOT_RUNNABLE
    assert factory.open_count == 0
    assert responses.start_calls == 0


def test_portal_review_at_ready_to_fill_is_a_stale_signal_and_blocks() -> None:
    state = MutableStateReader(CaseState.READY_TO_FILL)
    portal_review = MutablePortalReviewSignal(reached=True)
    session = FakeSession(state)
    responses = FakeResponses(call_turn(1, 1, (ScreenshotAction(),)))
    runner, factory = build_runner(
        state,
        session,
        responses,
        portal_review_signal=portal_review,
    )

    result = runner.run(request())

    assert result.status is ComputerUseRunStatus.BLOCKED
    assert result.block_reason is ComputerUseBlockReason.PORTAL_REVIEW_SIGNAL_INVALID
    assert state.state is CaseState.READY_TO_FILL
    assert portal_review.reads == 1
    assert factory.open_count == 1
    assert responses.start_calls == 0
    assert session.closed


def test_portal_review_signal_cannot_override_backend_state_drift() -> None:
    state = MutableStateReader(CaseState.FILLING)
    portal_review = MutablePortalReviewSignal(reached=True)
    portal_review.on_read = lambda: setattr(state, "state", CaseState.VERIFYING)
    session = FakeSession(state)
    responses = FakeResponses(call_turn(1, 1, (ScreenshotAction(),)))
    runner, factory = build_runner(
        state,
        session,
        responses,
        portal_review_signal=portal_review,
    )

    result = runner.run(request())

    assert result.status is ComputerUseRunStatus.BLOCKED
    assert result.block_reason is ComputerUseBlockReason.CASE_NOT_RUNNABLE
    assert state.state is CaseState.VERIFYING
    assert portal_review.reads == 1
    assert factory.open_count == 1
    assert responses.start_calls == 0
    assert session.closed


def test_portal_review_signal_failure_blocks_without_provider_call() -> None:
    state = MutableStateReader(CaseState.FILLING)
    portal_review = MutablePortalReviewSignal()

    def fail_signal() -> None:
        raise RuntimeError("private portal adapter detail")

    portal_review.on_read = fail_signal
    session = FakeSession(state)
    responses = FakeResponses(call_turn(1, 1, (ScreenshotAction(),)))
    runner, _ = build_runner(
        state,
        session,
        responses,
        portal_review_signal=portal_review,
    )

    result = runner.run(request())

    assert result.status is ComputerUseRunStatus.BLOCKED
    assert result.block_reason is ComputerUseBlockReason.PORTAL_REVIEW_SIGNAL_INVALID
    assert "private portal adapter detail" not in repr(result)
    assert responses.start_calls == 0
    assert session.closed


def test_deadline_after_provider_call_is_not_mislabeled_provider_failure() -> None:
    state = MutableStateReader()
    session = FakeSession(state)
    clock = ManualClock()
    responses = FakeResponses(
        call_turn(1, 1, (ScreenshotAction(),)),
        on_start=lambda: clock.advance(90),
    )
    runner, _ = build_runner(state, session, responses, clock=clock)

    result = runner.run(request())

    assert result.block_reason is ComputerUseBlockReason.DEADLINE_EXCEEDED
    assert result.provider_failure is None
    assert session.closed


def test_provider_timeout_is_sanitized_and_browser_is_closed() -> None:
    state = MutableStateReader()
    session = FakeSession(state)
    responses = FakeResponses(start_error=TimeoutError("must not be retained"))
    runner, _ = build_runner(state, session, responses)

    result = runner.run(request())

    assert result.block_reason is ComputerUseBlockReason.PROVIDER_FAILURE
    assert result.provider_failure is not None
    assert result.provider_failure.category is ProviderFailureCategory.TIMEOUT
    assert "must not be retained" not in repr(result)
    assert session.closed


def test_pending_provider_safety_check_blocks_without_action_or_retry() -> None:
    state = MutableStateReader()
    session = FakeSession(state)
    responses = FakeResponses(start_error=PendingComputerSafetyCheckError())
    runner, _ = build_runner(state, session, responses)

    result = runner.run(request())

    assert result.block_reason is ComputerUseBlockReason.PROVIDER_SAFETY_CHECK_BLOCKED
    assert result.provider_failure is None
    assert result.actions_executed == 0
    assert responses.start_calls == 1
    assert responses.continue_calls == []
    assert session.closed


def test_later_mixed_batch_treats_screenshot_as_counted_no_op() -> None:
    state = MutableStateReader()
    portal_review = MutablePortalReviewSignal()

    def reach_review(action: ComputerAction, _session: FakeSession) -> None:
        if isinstance(action, TypeAction):
            state.state = CaseState.FILLING
            portal_review.reached = True

    session = FakeSession(state, on_execute=reach_review)
    responses = FakeResponses(
        call_turn(1, 1, (ScreenshotAction(),)),
        call_turn(
            2,
            2,
            (ClickAction(10, 10), ScreenshotAction(), TypeAction("synthetic")),
        ),
    )
    runner, _ = build_runner(
        state,
        session,
        responses,
        portal_review_signal=portal_review,
    )

    result = runner.run(request())

    assert result.status is ComputerUseRunStatus.PORTAL_REVIEW_REACHED
    assert result.actions_executed == 4
    assert session.executed == ["screenshot", "click", "screenshot", "type"]
    assert session.screenshots == 1
    assert len(responses.continue_calls) == 1
    assert session.closed


@pytest.mark.parametrize(
    ("code", "category"),
    [
        ("insufficient_quota", ProviderFailureCategory.QUOTA_EXHAUSTED),
        ("billing_hard_limit_reached", ProviderFailureCategory.BILLING_LIMIT),
    ],
)
def test_quota_and_billing_failures_are_terminal_sanitized_and_never_retried(
    code: str,
    category: ProviderFailureCategory,
) -> None:
    state = MutableStateReader()
    session = FakeSession(state)
    responses = FakeResponses(start_error=FakeProviderError(code))
    runner, _ = build_runner(state, session, responses)

    result = runner.run(request())

    assert result.block_reason is ComputerUseBlockReason.PROVIDER_FAILURE
    assert result.provider_failure is not None
    assert result.provider_failure.category is category
    assert result.provider_failure.terminal
    assert not result.provider_failure.retryable
    assert responses.start_calls == 1
    assert responses.continue_calls == []
    assert "private remote provider detail" not in repr(result)
    assert session.closed


def test_provider_final_before_portal_review_blocks() -> None:
    state = MutableStateReader()
    session = FakeSession(state)
    responses = FakeResponses(
        call_turn(1, 1, (ScreenshotAction(),)),
        final_turn(2),
    )
    runner, _ = build_runner(state, session, responses)

    result = runner.run(request())

    assert (
        result.block_reason
        is ComputerUseBlockReason.PROVIDER_COMPLETED_BEFORE_PORTAL_REVIEW
    )
    assert session.closed


def test_request_repr_does_not_expose_prompt_values() -> None:
    assert "synthetic-secret" not in repr(
        ComputerUseRunRequest(case_id="case_cu_001", task="synthetic-secret")
    )


@pytest.mark.parametrize("task", [" " * 12_000, "x" + " " * 12_000])
def test_request_rejects_whitespace_only_or_raw_oversize_task(task: str) -> None:
    with pytest.raises(ValueError):
        ComputerUseRunRequest(case_id="case_cu_001", task=task)
