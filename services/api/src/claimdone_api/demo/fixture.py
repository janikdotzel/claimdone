"""Versioned, server-owned synthetic facts for the INT-002 deterministic demo.

These constants deliberately duplicate the committed public fixture manifest.  The
runtime demo must not read a mutable file from another worktree, and it must never
silently accept a provider copy whose bytes differ from the staged V1 images.
"""

from dataclasses import dataclass
from hashlib import sha256
from typing import Final, Literal


@dataclass(frozen=True, slots=True)
class Int002ImageFixture:
    """One ordered image identity copied from ``fixtures/int002/manifest.json``."""

    semantic_id: str
    filename: str
    sha256: str


INT002_FIXTURE_VERSION: Final[Literal["claimdone-int002-main-v1"]] = "claimdone-int002-main-v1"
INT002_PROVIDER_COPY_MUST_RETAIN_DIGEST: Final[Literal[True]] = True
INT002_IMAGE_FIXTURES: Final = (
    Int002ImageFixture(
        semantic_id="int002-image-overview",
        filename="01-synthetic-overview.png",
        sha256="188060aa5b755558c507d3ca0fc6390d03ba63488bf2fba642957a5151755435",
    ),
    Int002ImageFixture(
        semantic_id="int002-image-rear-detail",
        filename="02-synthetic-rear-detail.png",
        sha256="a3a12e69f712373c53bd3962b1ae06715d7c27859e3ac783765a7dd5e67081db",
    ),
    Int002ImageFixture(
        semantic_id="int002-image-context",
        filename="03-synthetic-context.png",
        sha256="76d3dc0b43872966e74c87c03e59cdd60e215a3902f7a1ccff1b789d4bab081d",
    ),
)
INT002_INCIDENT_TIME = "14:30:00"
INT002_CLARIFICATION_QUESTION = "What time did the incident happen?"
INT002_SYNTHETIC_STATEMENT_TEXT = (
    "Synthetic ClaimDone Build Week demo. On 2026-07-14 at Demo Street 1, Berlin, "
    "a staged second vehicle contacted the rear of the demo vehicle. Demo claimant: "
    "Demo Claimant. Demo policy: DEMO-POLICY-001. Demo registration: DEMO-CD-1. "
    "Counterparty known: yes. No one was injured and there is no immediate danger. "
    "The incident time is not yet provided."
)
INT002_SYNTHETIC_STATEMENT_SHA256 = sha256(
    INT002_SYNTHETIC_STATEMENT_TEXT.encode("utf-8")
).hexdigest()

if INT002_SYNTHETIC_STATEMENT_SHA256 != (
    "d85c66b4989cbff4adbfda59c7f1e78cba967f04fb64063f23aa2e32d7ac1de7"
):
    raise RuntimeError("INT-002 statement constant diverged from its committed manifest")
