"""Ports for the manual-import batch bounded context (US-554).

The application layer talks to persistence ONLY through this port —
it never imports infrastructure (spec §6.3).

:class:`ManualImportRepositoryPort` owns the unified batch table
(``manual_import_batches``) and the per-channel settlement column
mappings (``settlement_column_mappings``). The underlying imports
stay in their own repositories (bank / settlement) — the batch only
references them by id.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from hesabyar.domain.manual_imports.models import (
    ManualImportBatch,
    ManualImportBatchStatus,
    ManualImportKind,
    SettlementColumnMapping,
)


class ManualImportRepositoryPort(ABC):
    """Abstract port for manual-import batch persistence."""

    # -- batches -----------------------------------------------------------

    @abstractmethod
    def save_batch(self, batch: ManualImportBatch) -> ManualImportBatch:
        """Insert a new batch; returns it with timestamps set."""
        ...

    @abstractmethod
    def update_batch(self, batch: ManualImportBatch) -> ManualImportBatch:
        """Persist a changed batch (status/preview/review item/...); returns it."""
        ...

    @abstractmethod
    def get_batch(self, batch_id: str, tenant_id: str) -> ManualImportBatch | None:
        """One batch for the owning tenant, or None."""
        ...

    @abstractmethod
    def list_batches(
        self,
        tenant_id: str,
        *,
        kind: ManualImportKind | None = None,
        status: ManualImportBatchStatus | None = None,
        company_id: str | None = None,
        source_import_id: str | None = None,
    ) -> Sequence[ManualImportBatch]:
        """Batches of one tenant, newest first, optionally filtered."""
        ...

    @abstractmethod
    def get_batch_by_file_hash(
        self,
        tenant_id: str,
        kind: ManualImportKind,
        source_key: str,
        file_hash_sha256: str,
    ) -> ManualImportBatch | None:
        """The batch for a (kind, source, file hash), if already uploaded."""
        ...

    # -- settlement column mappings -----------------------------------------

    @abstractmethod
    def list_settlement_mappings(
        self,
        tenant_id: str,
        channel_key: str | None = None,
        company_id: str | None = None,
    ) -> Sequence[SettlementColumnMapping]:
        """Saved settlement mappings (active first, newest first)."""
        ...

    @abstractmethod
    def get_settlement_mapping(
        self, mapping_id: str, tenant_id: str
    ) -> SettlementColumnMapping | None:
        """One settlement mapping for the owning tenant, or None."""
        ...

    @abstractmethod
    def save_settlement_mapping(
        self, mapping: SettlementColumnMapping
    ) -> SettlementColumnMapping:
        """Insert a new settlement mapping; returns it with timestamps set."""
        ...

    @abstractmethod
    def update_settlement_mapping(
        self, mapping: SettlementColumnMapping
    ) -> SettlementColumnMapping:
        """Persist a changed settlement mapping (status/columns); returns it."""
        ...
