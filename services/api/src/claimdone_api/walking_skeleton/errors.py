"""Expected flow failures mapped to stable API envelopes."""

from dataclasses import dataclass

from claimdone_api.contracts import GateDecision


@dataclass(slots=True)
class FlowError(RuntimeError):
    code: str
    message: str
    status_code: int
    current_version: int | None = None
    gate_decision: GateDecision | None = None
    field: str | None = None

    def __str__(self) -> str:
        return self.message


class PortalUnavailableError(RuntimeError):
    """The local sandbox portal failed before reaching review."""
