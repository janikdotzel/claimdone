"""Idempotent demo reset orchestration."""

from claimdone_api.persistence import SqliteCaseRepository

from .ports import CaseResourceCleaner


class DemoResetService:
    """Remove external resources first, then cascade all persisted demo cases."""

    def __init__(
        self,
        repository: SqliteCaseRepository,
        resource_cleaner: CaseResourceCleaner,
    ) -> None:
        self._repository = repository
        self._resource_cleaner = resource_cleaner

    def reset(self) -> int:
        self._resource_cleaner.reset_resources()
        return self._repository.reset_cases()
