"""Ports for the bank import/reconciliation bounded context (US-514).

The application layer talks to persistence and file readers ONLY
through these ports — it never imports infrastructure (spec §6.3).

- :class:`BankRepositoryPort` owns column mappings, import batches +
  rows, learned match rules and recorded matches (all HesabYar-side
  tables; the engine owns the statements themselves).
- :class:`StatementFileReaderPort` turns uploaded bytes into a cell
  grid; the domain parser (``parsing.rows_from_grid``) does the rest.
  The reader owns format libraries (openpyxl, pdfplumber) and CSV
  encoding detection — the application layer never sees them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from hesabyar.domain.bank.models import (
    BankColumnMapping,
    BankImportBatch,
    BankImportRow,
    BankMatchRule,
    BankRule,
    BankRuleSuggestion,
    BankRuleSuggestionStatus,
    BankStatementMatch,
    StatementFileFormat,
)


@dataclass(frozen=True)
class DetectedFile:
    """What a file reader found: format, grid, and detection notes."""

    file_format: StatementFileFormat
    grid: list[list[str | None]]
    encoding: str | None = None
    detection_notes: tuple[str, ...] = ()


class StatementFileReaderPort(ABC):
    """Abstract port for statement file readers (xlsx / csv / pdf)."""

    @abstractmethod
    def read_file(self, content: bytes, filename: str) -> DetectedFile:
        """Read bytes into a cell grid (raises BankImportFileError when unreadable)."""
        ...


class BankRepositoryPort(ABC):
    """Abstract port for bank import/reconciliation persistence."""

    # -- column mappings ---------------------------------------------------

    @abstractmethod
    def list_mappings(
        self, tenant_id: str, bank_key: str | None = None, company_id: str | None = None
    ) -> Sequence[BankColumnMapping]:
        """Saved mappings (active first, newest first)."""
        ...

    @abstractmethod
    def get_mapping(self, mapping_id: str, tenant_id: str) -> BankColumnMapping | None:
        """One mapping for the owning tenant, or None."""
        ...

    @abstractmethod
    def save_mapping(self, mapping: BankColumnMapping) -> BankColumnMapping:
        """Insert a new mapping; returns it with timestamps set."""
        ...

    @abstractmethod
    def update_mapping(self, mapping: BankColumnMapping) -> BankColumnMapping:
        """Persist a changed mapping (status/columns); returns it."""
        ...

    # -- import batches + rows ----------------------------------------------

    @abstractmethod
    def list_batches(
        self, tenant_id: str, company_id: str | None = None
    ) -> Sequence[BankImportBatch]:
        """Import batches of one tenant, newest first."""
        ...

    @abstractmethod
    def get_batch(self, batch_id: str, tenant_id: str) -> BankImportBatch | None:
        """One batch for the owning tenant, or None."""
        ...

    @abstractmethod
    def save_batch(self, batch: BankImportBatch, rows: Sequence[BankImportRow]) -> BankImportBatch:
        """Insert a batch with its rows; returns the batch with timestamps set."""
        ...

    @abstractmethod
    def update_batch(self, batch: BankImportBatch) -> BankImportBatch:
        """Persist a changed batch (status/engine statement/review item); returns it."""
        ...

    @abstractmethod
    def list_rows(self, batch_id: str, tenant_id: str) -> Sequence[BankImportRow]:
        """Rows of one batch, in file order."""
        ...

    @abstractmethod
    def existing_bank_refs(
        self, tenant_id: str, company_id: str, bank_key: str, refs: Sequence[tuple[str, str]]
    ) -> set[tuple[str, str]]:
        """Which (bank_ref, iso-date) pairs already exist (dedup layer 1).

        ``refs`` are (bank_ref, Gregorian ISO date) pairs; the returned
        set is the subset already imported for this tenant/company/bank.
        """
        ...

    @abstractmethod
    def existing_match_keys(
        self, tenant_id: str, company_id: str, bank_key: str, keys: Sequence[str]
    ) -> set[str]:
        """Which match keys already exist (dedup layer 2)."""
        ...

    # -- learned match rules -------------------------------------------------

    @abstractmethod
    def list_rules(
        self, tenant_id: str, company_id: str | None = None
    ) -> Sequence[BankMatchRule]:
        """Learned rules of one tenant, newest first."""
        ...

    @abstractmethod
    def get_rule(self, rule_id: str, tenant_id: str) -> BankMatchRule | None:
        """One rule for the owning tenant, or None."""
        ...

    @abstractmethod
    def find_rule(
        self,
        tenant_id: str,
        company_id: str,
        bank_key: str,
        pattern: str,
        direction: str,
        target_account_id: int,
    ) -> BankMatchRule | None:
        """An existing rule for the same (pattern, direction, target), if any."""
        ...

    @abstractmethod
    def save_rule(self, rule: BankMatchRule) -> BankMatchRule:
        """Insert a new rule; returns it with timestamps set."""
        ...

    @abstractmethod
    def update_rule(self, rule: BankMatchRule) -> BankMatchRule:
        """Persist a changed rule (mode/confirmations); returns it."""
        ...

    # -- recorded matches ----------------------------------------------------

    @abstractmethod
    def list_matches_for_statement(
        self, tenant_id: str, statement_id: int
    ) -> Sequence[BankStatementMatch]:
        """Matches recorded for one engine statement, oldest first."""
        ...

    @abstractmethod
    def list_matches_for_company(
        self, tenant_id: str, company_id: str
    ) -> Sequence[BankStatementMatch]:
        """Matches recorded for one company, oldest first (report input)."""
        ...

    @abstractmethod
    def get_match_for_line(
        self, tenant_id: str, statement_id: int, statement_line_id: int
    ) -> BankStatementMatch | None:
        """The latest non-rejected match for one statement line, if any."""
        ...

    @abstractmethod
    def save_match(self, match: BankStatementMatch) -> BankStatementMatch:
        """Insert a match record; returns it with timestamps set."""
        ...

    @abstractmethod
    def update_match(self, match: BankStatementMatch) -> BankStatementMatch:
        """Persist a changed match (status/review item); returns it."""
        ...


class BankRulesRepositoryPort(ABC):
    """Abstract port for user-defined bank auto-coding rules (US-565, W3).

    A separate port from :class:`BankRepositoryPort` on purpose: the
    W3 rules (``bank_rules`` / ``bank_rule_suggestions``) are a
    different store from the B2 learned rules (``bank_match_rules``),
    and the existing bank fakes stay untouched. The reconcile service
    takes this port as an optional dependency — without it, no W3
    rule fires and US-514 behaviour is unchanged.
    """

    # -- bank rules ----------------------------------------------------------

    @abstractmethod
    def list_bank_rules(
        self,
        tenant_id: str,
        company_id: str | None = None,
        enabled: bool | None = None,
        mode: str | None = None,
    ) -> Sequence[BankRule]:
        """Bank rules of one tenant, in firing order (priority, created, id)."""
        ...

    @abstractmethod
    def get_bank_rule(self, rule_id: str, tenant_id: str) -> BankRule | None:
        """One bank rule for the owning tenant, or None."""
        ...

    @abstractmethod
    def save_bank_rule(self, rule: BankRule) -> BankRule:
        """Insert a new bank rule; returns it with timestamps set."""
        ...

    @abstractmethod
    def update_bank_rule(self, rule: BankRule) -> BankRule:
        """Persist a changed bank rule (pattern/target/mode/enabled); returns it."""
        ...

    @abstractmethod
    def delete_bank_rule(self, rule_id: str, tenant_id: str) -> bool:
        """Delete a bank rule; its suggestions survive with rule_id NULL.

        Returns True when a row was deleted, False when the id did not
        exist for the tenant.
        """
        ...

    # -- bank-rule suggestions -------------------------------------------------

    @abstractmethod
    def list_suggestions(
        self,
        tenant_id: str,
        rule_id: str | None = None,
        status: BankRuleSuggestionStatus | None = None,
        company_id: str | None = None,
    ) -> Sequence[BankRuleSuggestion]:
        """Suggestions of one tenant, newest first."""
        ...

    @abstractmethod
    def get_suggestion(self, suggestion_id: str, tenant_id: str) -> BankRuleSuggestion | None:
        """One suggestion for the owning tenant, or None."""
        ...

    @abstractmethod
    def find_pending_suggestion(
        self, rule_id: str, tenant_id: str, statement_id: int, statement_line_id: int
    ) -> BankRuleSuggestion | None:
        """The pending suggestion for one (rule, statement, line), if any.

        The reconcile dedup: a re-run that re-suggests the same line
        from the same rule must not record a second pending row.
        """
        ...

    @abstractmethod
    def save_suggestion(self, suggestion: BankRuleSuggestion) -> BankRuleSuggestion:
        """Insert a new suggestion; returns it with timestamps set."""
        ...

    @abstractmethod
    def update_suggestion(self, suggestion: BankRuleSuggestion) -> BankRuleSuggestion:
        """Persist a changed suggestion (status/confirmed_by); returns it."""
        ...
