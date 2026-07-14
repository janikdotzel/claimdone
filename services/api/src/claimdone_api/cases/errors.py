"""Domain errors exposed by the case service."""

from claimdone_api.contracts import CaseState


class CaseServiceError(RuntimeError):
    """Base class for expected case workflow failures."""


class CaseNotFoundError(CaseServiceError):
    def __init__(self, case_id: str) -> None:
        self.case_id = case_id
        super().__init__(f"Case not found: {case_id}")


class CaseVersionConflictError(CaseServiceError):
    def __init__(self, case_id: str, expected_version: int, current_version: int) -> None:
        self.case_id = case_id
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(
            f"Case {case_id} is at version {current_version}, expected {expected_version}"
        )


class InvalidCaseStateTransitionError(CaseServiceError):
    def __init__(self, current: CaseState, target: CaseState) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Invalid case transition: {current.value} -> {target.value}")


class CaseSnapshotValidationError(CaseServiceError):
    """Raised when payloads disagree with the canonical case snapshot."""
