"""Persistent case-to-media ownership and restart-safe cleanup."""

from claimdone_api.persistence import SqliteCaseRepository

from .storage import CaseMediaStore
from .types import CaseHandle


class PersistentCaseMediaCleaner:
    """Resolve opaque handles from SQLite; never derive paths from case IDs."""

    def __init__(self, repository: SqliteCaseRepository, store: CaseMediaStore) -> None:
        self._repository = repository
        self._store = store

    def delete_case_resources(self, case_id: str) -> None:
        storage_name = self._repository.get_case_media_handle(case_id)
        if storage_name is not None:
            self._store.delete_case(CaseHandle(storage_name=storage_name))

    def reset_resources(self) -> None:
        for _case_id, storage_name in self._repository.list_case_media_handles():
            self._store.delete_case(CaseHandle(storage_name=storage_name))
        # Also remove interrupted-intake directories that were created after G0
        # but never bound to a case after G1.
        self._store.reset()
