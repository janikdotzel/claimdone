"""Fixed, redacted failures for the human approval boundary."""

from dataclasses import dataclass

from claimdone_api.contracts import GateReasonCode


@dataclass(frozen=True, slots=True)
class AuthorityError(RuntimeError):
    code: str
    safe_message: str
    status_code: int
    reason_codes: tuple[GateReasonCode, ...] = ()
    current_version: int | None = None

    def __str__(self) -> str:
        return self.safe_message


def agent_forbidden() -> AuthorityError:
    return AuthorityError(
        code="AUTH_AGENT_FORBIDDEN",
        safe_message="Agent credentials cannot approve a sandbox case.",
        status_code=403,
        reason_codes=(GateReasonCode.G9_AGENT_FORBIDDEN,),
    )


def token_invalid() -> AuthorityError:
    return AuthorityError(
        code="AUTH_TOKEN_INVALID",
        safe_message="The human approval capability is invalid or no longer usable.",
        status_code=403,
        reason_codes=(GateReasonCode.G9_TOKEN_INVALID,),
    )


def role_invalid() -> AuthorityError:
    return AuthorityError(
        code="AUTH_ROLE_INVALID",
        safe_message="The supplied capability has no approval authority.",
        status_code=403,
        reason_codes=(GateReasonCode.G9_ROLE_INVALID,),
    )


def case_not_found() -> AuthorityError:
    return AuthorityError(
        code="AUTH_CASE_NOT_FOUND",
        safe_message="The sandbox case does not exist.",
        status_code=404,
    )


def state_conflict(*, current_version: int | None = None) -> AuthorityError:
    return AuthorityError(
        code="AUTH_STATE_CONFLICT",
        safe_message="The sandbox case is not at the required approval boundary.",
        status_code=409,
        current_version=current_version,
    )
