"""Internal values for separated agent and human capabilities."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from claimdone_api.contracts import PortalVariant

type AuthorityRole = Literal["agent", "human"]
type AuthorityPurpose = Literal["portal_run", "human_approve"]


@dataclass(frozen=True, slots=True)
class IssuedCapability:
    """A plaintext capability returned exactly once to its trusted caller."""

    token: str = field(repr=False)
    case_id: str
    bound_case_version: int
    role: AuthorityRole
    purpose: AuthorityPurpose
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AuthorizedHumanApproval:
    """Secret-free result of classifying a bearer capability."""

    digest: bytes = field(repr=False)
    case_id: str
    bound_case_version: int
    variant: PortalVariant
    checked_at: datetime
    issued_at: datetime
    expires_at: datetime
