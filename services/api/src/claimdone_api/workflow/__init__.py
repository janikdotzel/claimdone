"""Isolated read-model prototype retained only for its focused component tests.

Production composition must use ``claimdone_api.cases.create_workflow_router``
with the atomic ``CaseService``. Nothing in this package is mounted by
``claimdone_api.main`` and it must not become a parallel HTTP authority.
"""

from .errors import (
    WorkflowCaseNotFoundError,
    WorkflowCursorError,
    WorkflowDataIntegrityError,
    WorkflowVersionChurnError,
)
from .events import EventStreamConfig, WorkflowEventStreamer
from .router import DEFAULT_WORKFLOW_PREFIX, create_workflow_router
from .snapshots import SnapshotAssembler
from .transcript import (
    MAX_TRANSCRIPT_TEXT_BYTES,
    MAX_TRANSCRIPT_TEXT_CHARACTERS,
    MediaTranscriptTextReader,
)

__all__ = [
    "DEFAULT_WORKFLOW_PREFIX",
    "MAX_TRANSCRIPT_TEXT_BYTES",
    "MAX_TRANSCRIPT_TEXT_CHARACTERS",
    "EventStreamConfig",
    "MediaTranscriptTextReader",
    "SnapshotAssembler",
    "WorkflowCaseNotFoundError",
    "WorkflowCursorError",
    "WorkflowDataIntegrityError",
    "WorkflowEventStreamer",
    "WorkflowVersionChurnError",
    "create_workflow_router",
]
