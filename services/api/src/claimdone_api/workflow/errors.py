"""Closed read-side failures for workflow snapshots and event replay."""


class WorkflowReadError(RuntimeError):
    """Base class for failures safe to map at the HTTP boundary."""


class WorkflowCaseNotFoundError(WorkflowReadError):
    """The requested case does not exist."""


class WorkflowVersionChurnError(WorkflowReadError):
    """A stable snapshot could not be assembled after one bounded retry."""

    def __init__(self, current_version: int | None) -> None:
        self.current_version = current_version
        super().__init__("The workflow case changed while its snapshot was assembled.")


class WorkflowDataIntegrityError(WorkflowReadError):
    """Persisted or adapter-owned workflow data violates canonical contracts."""


class WorkflowCursorError(WorkflowReadError):
    """An SSE replay cursor is malformed, ambiguous, or outside SQLite bounds."""
