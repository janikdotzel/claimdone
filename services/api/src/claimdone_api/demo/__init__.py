"""Deterministic, provider-free INT-002 demo analysis boundary."""

from .fixture import (
    INT002_CLARIFICATION_QUESTION,
    INT002_FIXTURE_VERSION,
    INT002_IMAGE_FIXTURES,
    INT002_INCIDENT_TIME,
    INT002_PROVIDER_COPY_MUST_RETAIN_DIGEST,
    INT002_SYNTHETIC_STATEMENT_SHA256,
    INT002_SYNTHETIC_STATEMENT_TEXT,
    Int002ImageFixture,
)
from .models import (
    ApprovedDemoIntake,
    BoundDemoClarification,
    ConfirmedSyntheticStatement,
    DemoAnalysisInputError,
    DemoAnalysisRequest,
    DemoAnalysisResult,
    DemoClarificationResolution,
    DemoExecutionProof,
    DemoInitialPersistenceInputs,
    ReconstructedDemoContinuation,
)
from .service import analyze_int002_demo, reconstruct_int002_clarification

__all__ = [
    "INT002_CLARIFICATION_QUESTION",
    "INT002_FIXTURE_VERSION",
    "INT002_IMAGE_FIXTURES",
    "INT002_INCIDENT_TIME",
    "INT002_PROVIDER_COPY_MUST_RETAIN_DIGEST",
    "INT002_SYNTHETIC_STATEMENT_SHA256",
    "INT002_SYNTHETIC_STATEMENT_TEXT",
    "ApprovedDemoIntake",
    "BoundDemoClarification",
    "ConfirmedSyntheticStatement",
    "DemoAnalysisInputError",
    "DemoAnalysisRequest",
    "DemoAnalysisResult",
    "DemoClarificationResolution",
    "DemoExecutionProof",
    "DemoInitialPersistenceInputs",
    "Int002ImageFixture",
    "ReconstructedDemoContinuation",
    "analyze_int002_demo",
    "reconstruct_int002_clarification",
]
