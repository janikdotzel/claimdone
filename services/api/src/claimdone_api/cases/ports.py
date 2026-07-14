"""Ports that let media storage participate in deletion without coupling packages."""

from typing import Protocol


class CaseResourceCleaner(Protocol):
    """Delete resources owned outside the SQLite case repository."""

    def delete_case_resources(self, case_id: str) -> None:
        """Delete every external resource belonging to one case."""

    def reset_resources(self) -> None:
        """Delete all external demo resources managed by this cleaner."""


class NoOpCaseResourceCleaner:
    """Default cleaner used before the media worktree is integrated."""

    def delete_case_resources(self, case_id: str) -> None:
        del case_id

    def reset_resources(self) -> None:
        return
