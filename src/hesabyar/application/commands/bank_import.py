"""Bank-statement import use cases (US-514, B1).

The import pipeline (phased design §4 §2.2): upload → detect bank +
format (unknown format → quarantine, never guessed) → parse +
validate → two-layer dedup → accounting-effect preview → human
approval (``manual_import_review`` queue item) → write to Tryton
``account_statement`` as a draft.

The service owns the pipeline; the file reader owns bytes→grid, the
domain parser owns grid→rows, the engine port owns the statement.
File bytes are never stored — only the filename for provenance.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import Sequence

from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.errors import ReviewInvalidStateError
from hesabyar.domain.accounting.commands import (
    CreateBankStatement,
    CreateBankStatementLine,
    UpdateBankStatementLine,
)
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import (
    AccountRef,
    BankStatementLineRef,
    BankStatementRef,
    CompanyRef,
    InvoiceRef,
    PartyRef,
    PaymentRef,
    StatementJournalRef,
)
from hesabyar.domain.bank.errors import (
    BankColumnMappingInvalidError,
    BankColumnMappingNotFoundError,
    BankImportFileError,
    BankImportInvalidStateError,
    BankImportNotFoundError,
    BankLineAlreadyMatchedError,
    BankMatchRuleNotFoundError,
    BankStatementJournalNotFoundError,
    BankStatementLineNotFoundError,
    BankStatementNotDraftError,
)
from hesabyar.domain.bank.models import (
    BankColumnMapping,
    BankImportBatch,
    BankImportRow,
    BankMatchRule,
    BankStatementMatch,
    ColumnMappingStatus,
    FileCurrencyUnit,
    ImportBatchStatus,
    MatchStatus,
    MatchTargetType,
    StatementFileFormat,
)
from hesabyar.domain.bank.parsing import (
    ParsedRow,
    build_match_key,
    normalize_persian_text,
    rows_from_grid,
    suggest_mapping,
    validate_mapping_columns,
)
from hesabyar.domain.bank.ports import BankRepositoryPort, StatementFileReaderPort
from hesabyar.domain.common.errors import (
    AccountNotFoundError,
    BankStatementNotFoundError,
    InvoiceNotFoundError,
    PaymentNotFoundError,
)
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.integrations.models import SuggestionMode
from hesabyar.domain.review_queue.models import ReviewItemType, ReviewSeverity


@dataclasses.dataclass(frozen=True)
class FileDetection:
    """What the detect step found (no persistence — a pure preview)."""

    file_format: StatementFileFormat
    encoding: str | None
    columns: tuple[str, ...]
    sample_rows: tuple[tuple[str | None, ...], ...]
    suggested_mapping: dict[str, str]
    missing_fields: tuple[str, ...]
    detection_notes: tuple[str, ...]


class BankImportService:
    """Statement import: detect → parse → preview → confirm/cancel."""

    def __init__(
        self,
        engine: AccountingEnginePort,
        bank: BankRepositoryPort,
        review: ReviewQueueCommandService,
        reader: StatementFileReaderPort,
        *,
        now: datetime.datetime | None = None,
    ) -> None:
        self._engine = engine
        self._bank = bank
        self._review = review
        self._reader = reader
        self._now = now

    def _clock(self) -> datetime.datetime:
        if self._now is not None:
            return self._now
        return datetime.datetime.now(datetime.UTC)

    # -- detection -----------------------------------------------------------

    def detect_file(self, *, content: bytes, filename: str) -> FileDetection:
        """Read a file and suggest a column mapping (no persistence).

        The header row is the grid's first row; the sample is the next
        five data rows. Unknown formats raise BankImportFileError —
        the file is quarantined, never processed on a guess.
        """
        detected = self._reader.read_file(content, filename)
        if not detected.grid:
            raise BankImportFileError("The file has no rows.")
        headers = [str(cell or "") for cell in detected.grid[0]]
        suggested = suggest_mapping(headers)
        problems = validate_mapping_columns(suggested)
        sample = tuple(
            tuple(str(cell) if cell is not None else None for cell in row)
            for row in detected.grid[1:6]
        )
        return FileDetection(
            file_format=detected.file_format,
            encoding=detected.encoding,
            columns=tuple(headers),
            sample_rows=sample,
            suggested_mapping=suggested,
            missing_fields=tuple(problems),
            detection_notes=detected.detection_notes,
        )

    # -- column mappings -----------------------------------------------------

    def save_mapping(
        self,
        *,
        tenant_id: str,
        bank_key: str,
        name: str,
        file_format: StatementFileFormat,
        columns: dict[str, str],
        date_kind: str = "jalali",
        currency_unit: FileCurrencyUnit = FileCurrencyUnit.RIAL,
        header_row: int = 0,
        company_id: str | None = None,
    ) -> BankColumnMapping:
        """Save a per-bank column mapping (validates the canonical fields)."""
        problems = validate_mapping_columns(columns)
        if date_kind not in ("jalali", "gregorian"):
            problems.append(f"unknown date_kind {date_kind!r} (expected 'jalali' or 'gregorian')")
        if header_row < 0:
            problems.append("header_row must be >= 0")
        if problems:
            raise BankColumnMappingInvalidError(
                f"Column mapping is invalid: {'; '.join(problems)}.",
                details={"problems": problems},
            )
        now = self._clock()
        return self._bank.save_mapping(
            BankColumnMapping(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                bank_key=bank_key,
                name=name,
                file_format=file_format,
                columns=dict(columns),
                date_kind=date_kind,
                currency_unit=currency_unit,
                header_row=header_row,
                company_id=company_id,
                status=ColumnMappingStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
        )

    def supersede_mapping(self, *, tenant_id: str, mapping_id: str) -> BankColumnMapping:
        """Supersede a saved mapping (it stays for provenance, unused for new imports)."""
        mapping = self._bank.get_mapping(mapping_id, tenant_id)
        if mapping is None:
            raise BankColumnMappingNotFoundError(
                f"Column mapping {mapping_id!r} does not exist.",
                details={"mapping_id": mapping_id},
            )
        return self._bank.update_mapping(
            dataclasses.replace(mapping, status=ColumnMappingStatus.SUPERSEDED)
        )

    # -- import ---------------------------------------------------------------

    def create_import(
        self,
        *,
        tenant_id: str,
        company: CompanyRef,
        company_id: str,
        bank_key: str,
        statement_journal_id: int,
        filename: str,
        content: bytes,
        currency_unit: FileCurrencyUnit,
        default_account_id: int,
        mapping_id: str | None = None,
        mapping_columns: dict[str, str] | None = None,
        date_kind: str = "jalali",
        header_row: int = 0,
        actor: str,
    ) -> BankImportBatch:
        """Parse an uploaded statement file into a batch awaiting review.

        Either ``mapping_id`` (a saved mapping) or ``mapping_columns``
        (an inline mapping) must be given. The batch is born
        ``awaiting_review`` with its preview computed and a
        ``manual_import_review`` queue item filed; nothing reaches the
        engine until a human confirms.
        """
        mapping = self._resolve_mapping(
            tenant_id=tenant_id,
            bank_key=bank_key,
            mapping_id=mapping_id,
            mapping_columns=mapping_columns,
            date_kind=date_kind,
            header_row=header_row,
            currency_unit=currency_unit,
        )
        self._require_statement_journal(company, statement_journal_id)
        self._require_default_account(company, default_account_id)
        detected = self._reader.read_file(content, filename)
        if mapping_id is not None and detected.file_format != mapping.file_format:
            raise BankImportFileError(
                f"The mapping is for {mapping.file_format.value} files but the "
                f"upload is {detected.file_format.value}.",
                details={
                    "mapping_format": mapping.file_format.value,
                    "file_format": detected.file_format.value,
                },
            )
        parsed = rows_from_grid(detected.grid, mapping, currency_unit)
        batch_id = str(uuid.uuid4())
        rows = self._dedup_rows(
            tenant_id=tenant_id,
            company_id=company_id,
            bank_key=bank_key,
            parsed=parsed,
            batch_id=batch_id,
        )
        rules = self._bank.list_rules(tenant_id, company_id)
        rows = self._suggest_coding(rows, rules, default_account_id)
        batch = self._build_batch(
            batch_id=batch_id,
            tenant_id=tenant_id,
            company_id=company_id,
            bank_key=bank_key,
            statement_journal_id=statement_journal_id,
            filename=filename,
            file_format=detected.file_format,
            currency_unit=currency_unit,
            default_account_id=default_account_id,
            column_mapping_id=mapping.id if mapping_id is not None else None,
            rows=rows,
        )
        saved = self._bank.save_batch(batch, rows)
        item = self._review.file_item(
            tenant_id=tenant_id,
            item_type=ReviewItemType.MANUAL_IMPORT_REVIEW,
            severity=ReviewSeverity.WARNING,
            reason=(
                f"ایمپورت صورت‌حساب {bank_key} ({filename}): {saved.new_rows} ردیف جدید، "
                f"{saved.duplicate_rows} تکراری، {saved.invalid_rows} نامعتبر؛ "
                f"واریز {saved.total_deposits_irr:,} ریال، برداشت {saved.total_withdrawals_irr:,} ریال."
            ),
            suggested_action="پیش‌نمایش اثر حسابداری را بررسی و تأیید کنید، یا بچ را لغو کنید.",
            company_id=company_id,
            channel=bank_key,
            source_event_ref=f"bank-import:{saved.id}",
            amount_irr=saved.gross_irr,
            actor=actor,
        )
        saved = self._bank.update_batch(dataclasses.replace(saved, review_item_id=item.id))
        return saved

    def confirm_import(
        self, *, tenant_id: str, batch_id: str, company: CompanyRef, actor: str
    ) -> BankImportBatch:
        """Write an awaiting-review batch to the engine as a draft statement.

        The statement is created in state ``draft`` with the preview's
        coding; the linked review item is resolved with
        ``approve_preview`` (already-resolved items are tolerated —
        the import stands, the bookkeeping follows).
        """
        batch = self._require_batch(batch_id, tenant_id)
        if batch.status != ImportBatchStatus.AWAITING_REVIEW:
            raise BankImportInvalidStateError(
                f"Import batch {batch_id} is {batch.status.value} and cannot be confirmed.",
                details={"batch_id": batch_id, "status": batch.status.value},
            )
        rows = [row for row in self._bank.list_rows(batch_id, tenant_id) if row.is_new]
        if not rows:
            raise BankImportInvalidStateError(
                f"Import batch {batch_id} has no new rows to import.",
                details={"batch_id": batch_id},
            )
        dated = [row for row in rows if row.row_date is not None and row.amount_irr is not None]
        first_date = min(row.row_date for row in dated if row.row_date is not None)
        last_date = max(row.row_date for row in dated if row.row_date is not None)
        lines: list[CreateBankStatementLine] = []
        for row in dated:
            account_id = (
                row.suggested_account_id
                if row.suggested_account_id is not None
                else batch.default_account_id
            )
            lines.append(
                CreateBankStatementLine(
                    date=row.row_date,  # type: ignore[arg-type]  # dated ⇒ present
                    amount=MoneyIRR(row.amount_irr),  # type: ignore[arg-type]
                    account=AccountRef(account_id),
                    description=row.description,
                    number=row.bank_ref,
                    party=PartyRef(row.suggested_party_id)
                    if row.suggested_party_id is not None
                    else None,
                    related_invoice=InvoiceRef(row.suggested_invoice_id)
                    if row.suggested_invoice_id is not None
                    else None,
                )
            )
        statement = self._engine.create_bank_statement(
            CreateBankStatement(
                company=company,
                journal=StatementJournalRef(batch.statement_journal_id),
                name=(
                    f"{batch.bank_key} "
                    f"{first_date.gregorian_date.isoformat()}..{last_date.gregorian_date.isoformat()} "
                    f"[{batch.id[:8]}]"
                ),
                date=last_date,
                lines=tuple(lines),
                start_balance=MoneyIRR(batch.start_balance_irr)
                if batch.start_balance_irr is not None
                else None,
                end_balance=MoneyIRR(batch.end_balance_irr)
                if batch.end_balance_irr is not None
                else None,
            )
        )
        confirmed = self._bank.update_batch(
            dataclasses.replace(
                batch,
                status=ImportBatchStatus.CONFIRMED,
                engine_statement_id=statement.engine_id,
                confirmed_at=self._clock(),
            )
        )
        if batch.review_item_id is not None:
            self._resolve_review_item(
                tenant_id, batch.review_item_id, "approve_preview", actor
            )
        return confirmed

    def cancel_import(
        self, *, tenant_id: str, batch_id: str, actor: str
    ) -> BankImportBatch:
        """Cancel an awaiting-review batch (nothing reaches the engine)."""
        batch = self._require_batch(batch_id, tenant_id)
        if batch.status != ImportBatchStatus.AWAITING_REVIEW:
            raise BankImportInvalidStateError(
                f"Import batch {batch_id} is {batch.status.value} and cannot be cancelled.",
                details={"batch_id": batch_id, "status": batch.status.value},
            )
        cancelled = self._bank.update_batch(
            dataclasses.replace(
                batch, status=ImportBatchStatus.CANCELLED, cancelled_at=self._clock()
            )
        )
        if batch.review_item_id is not None:
            self._resolve_review_item(tenant_id, batch.review_item_id, "cancel_batch", actor)
        return cancelled

    # -- learned rules ----------------------------------------------------------

    def set_rule_mode(
        self, *, tenant_id: str, rule_id: str, mode: SuggestionMode
    ) -> BankMatchRule:
        """Promote/demote a learned rule between Suggest and Auto (a human decision)."""
        rule = self._bank.get_rule(rule_id, tenant_id)
        if rule is None:
            raise BankMatchRuleNotFoundError(
                f"Match rule {rule_id!r} does not exist.",
                details={"rule_id": rule_id},
            )
        return self._bank.update_rule(dataclasses.replace(rule, mode=mode))

    def learn_rule(
        self,
        *,
        tenant_id: str,
        company_id: str,
        bank_key: str,
        pattern: str,
        direction: str,
        target_account_id: int,
        target_party_id: int | None = None,
        target_kind: str = "account",
    ) -> BankMatchRule:
        """Record a confirmed manual mapping as a learned rule (Suggest → Auto).

        Re-confirming the same (pattern, direction, target) increments
        ``confirmations`` instead of duplicating the rule.
        """
        normalised = normalize_persian_text(pattern)
        existing = self._bank.find_rule(
            tenant_id, company_id, bank_key, normalised, direction, target_account_id
        )
        if existing is not None:
            return self._bank.update_rule(
                dataclasses.replace(
                    existing,
                    confirmations=existing.confirmations + 1,
                    target_party_id=target_party_id or existing.target_party_id,
                )
            )
        now = self._clock()
        return self._bank.save_rule(
            BankMatchRule(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                company_id=company_id,
                bank_key=bank_key,
                pattern=normalised,
                direction=direction,
                target_account_id=target_account_id,
                target_party_id=target_party_id,
                target_kind=target_kind,
                mode=SuggestionMode.SUGGEST,
                confirmations=1,
                created_at=now,
                updated_at=now,
            )
        )

    # -- manual match -----------------------------------------------------------

    def apply_manual_match(
        self,
        *,
        tenant_id: str,
        company: CompanyRef,
        company_id: str,
        statement_id: int,
        line_id: int,
        target_type: MatchTargetType,
        target_id: str,
        account_id: int | None = None,
        party_id: int | None = None,
        save_as_rule: bool = False,
        review_item_id: str | None = None,
        actor: str,
    ) -> BankStatementMatch:
        """Apply a human-confirmed match to one draft statement line.

        The line's ``related_to``/account/party are written through the
        engine port; the match is recorded CONFIRMED; with
        ``save_as_rule`` the mapping is learned for the future. A line
        that already has a live match is refused (409) — matching is
        one decision per line.
        """
        view = self._engine.get_bank_statement(BankStatementRef(statement_id))
        if view is None:
            raise BankStatementNotFoundError(
                f"Bank statement {statement_id} does not exist in the engine.",
                details={"statement_id": statement_id},
            )
        if view.state != "draft":
            raise BankStatementNotDraftError(
                f"Bank statement {statement_id} is {view.state}, not a draft; "
                "its lines are read-only.",
                details={"statement_id": statement_id, "state": view.state},
            )
        line = next((item for item in view.lines if item.id == line_id), None)
        if line is None:
            raise BankStatementLineNotFoundError(
                f"Statement line {line_id} is not on statement {statement_id}.",
                details={"statement_id": statement_id, "line_id": line_id},
            )
        existing = self._bank.get_match_for_line(tenant_id, statement_id, line_id)
        if existing is not None and existing.status in (
            MatchStatus.AUTO_APPLIED,
            MatchStatus.CONFIRMED,
        ):
            raise BankLineAlreadyMatchedError(
                f"Statement line {line_id} already has a {existing.status.value} match.",
                details={"statement_id": statement_id, "line_id": line_id},
            )
        journals = {j.id: j for j in self._engine.list_statement_journals(company)}
        journal = journals.get(view.journal_id)
        target_label, account, party, invoice = self._resolve_match_target(
            company=company,
            target_type=target_type,
            target_id=target_id,
            account_id=account_id,
            party_id=party_id,
        )
        self._engine.update_bank_statement_line(
            UpdateBankStatementLine(
                statement=BankStatementRef(statement_id),
                line=BankStatementLineRef(line_id),
                account=account,
                party=party,
                related_invoice=invoice,
            )
        )
        # A prior suggestion for this line is superseded by the human decision.
        for prior in self._bank.list_matches_for_statement(tenant_id, statement_id):
            if prior.statement_line_id == line_id and prior.status == MatchStatus.SUGGESTED:
                self._bank.update_match(dataclasses.replace(prior, status=MatchStatus.REJECTED))
        match = self._bank.save_match(
            BankStatementMatch(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                company_id=company_id,
                statement_id=statement_id,
                statement_line_id=line_id,
                line_date=line.date,
                line_amount_irr=line.amount.amount,
                line_description=line.description,
                target_type=target_type,
                target_id=int(target_id) if target_id.isdigit() else None,
                target_ref=target_id,
                target_label=target_label,
                priority=3,
                confidence=100,
                status=MatchStatus.CONFIRMED,
                destination_account_id=journal.account.engine_id if journal else None,
                destination_shaba=journal.bank_account_number if journal else None,
                review_item_id=review_item_id,
                matched_at=self._clock(),
                matched_by=actor,
            )
        )
        if save_as_rule and account is not None and line.description.strip():
            self.learn_rule(
                tenant_id=tenant_id,
                company_id=company_id,
                bank_key="",
                pattern=line.description,
                direction="in" if line.amount.is_positive else "out",
                target_account_id=account.engine_id,
                target_party_id=party.engine_id if party is not None else None,
            )
        return match

    # -- helpers ----------------------------------------------------------------

    def _require_batch(self, batch_id: str, tenant_id: str) -> BankImportBatch:
        batch = self._bank.get_batch(batch_id, tenant_id)
        if batch is None:
            raise BankImportNotFoundError(
                f"Import batch {batch_id!r} does not exist.",
                details={"batch_id": batch_id},
            )
        return batch

    def _resolve_mapping(
        self,
        *,
        tenant_id: str,
        bank_key: str,
        mapping_id: str | None,
        mapping_columns: dict[str, str] | None,
        date_kind: str,
        header_row: int,
        currency_unit: FileCurrencyUnit,
    ) -> BankColumnMapping:
        if mapping_id is not None:
            mapping = self._bank.get_mapping(mapping_id, tenant_id)
            if mapping is None:
                raise BankColumnMappingNotFoundError(
                    f"Column mapping {mapping_id!r} does not exist.",
                    details={"mapping_id": mapping_id},
                )
            return mapping
        if mapping_columns is None:
            raise BankColumnMappingInvalidError(
                "An import needs a saved mapping (mapping_id) or an inline mapping (columns).",
                details={},
            )
        problems = validate_mapping_columns(mapping_columns)
        if date_kind not in ("jalali", "gregorian"):
            problems.append(f"unknown date_kind {date_kind!r}")
        if header_row < 0:
            problems.append("header_row must be >= 0")
        if problems:
            raise BankColumnMappingInvalidError(
                f"Column mapping is invalid: {'; '.join(problems)}.",
                details={"problems": problems},
            )
        return BankColumnMapping(
            id="",
            tenant_id=tenant_id,
            bank_key=bank_key,
            name="inline",
            file_format=StatementFileFormat.CSV,
            columns=dict(mapping_columns),
            date_kind=date_kind,
            currency_unit=currency_unit,
            header_row=header_row,
        )

    def _require_statement_journal(self, company: CompanyRef, journal_id: int) -> None:
        journals = self._engine.list_statement_journals(company)
        if all(journal.id != journal_id for journal in journals):
            raise BankStatementJournalNotFoundError(
                f"Statement journal {journal_id} does not exist for this company.",
                details={"journal_id": journal_id, "company_id": company.engine_id},
            )

    def _require_default_account(self, company: CompanyRef, account_id: int) -> None:
        accounts = self._engine.get_chart_of_accounts(company)
        account = next((item for item in accounts if item.ref.engine_id == account_id), None)
        if account is None:
            raise AccountNotFoundError(
                f"Account {account_id} does not exist for this company.",
                details={"account_id": account_id, "company_id": company.engine_id},
            )
        if account.closed or not account.active:
            raise AccountNotFoundError(
                f"Account {account_id} is closed or inactive.",
                details={"account_id": account_id, "company_id": company.engine_id},
            )

    def _dedup_rows(
        self,
        *,
        tenant_id: str,
        company_id: str,
        bank_key: str,
        parsed: list[ParsedRow],
        batch_id: str,
    ) -> list[BankImportRow]:
        """Apply both dedup layers (plus within-file duplicates)."""
        valid = [row for row in parsed if row.is_valid]
        refs = [
            (row.bank_ref, row.row_date.gregorian_date.isoformat())
            for row in valid
            if row.bank_ref is not None and row.row_date is not None
        ]
        existing_refs = self._bank.existing_bank_refs(tenant_id, company_id, bank_key, refs)
        keys = {
            build_match_key(row.row_date, row.amount_irr, row.description): row
            for row in valid
            if row.row_date is not None and row.amount_irr is not None
        }
        existing_keys = self._bank.existing_match_keys(
            tenant_id, company_id, bank_key, list(keys)
        )
        seen_refs: set[tuple[str, str]] = set()
        seen_keys: set[str] = set()
        rows: list[BankImportRow] = []
        for parsed_row in parsed:
            is_duplicate = False
            reason: str | None = None
            match_key: str | None = None
            if parsed_row.is_valid and parsed_row.row_date is not None and parsed_row.amount_irr is not None:
                match_key = build_match_key(
                    parsed_row.row_date, parsed_row.amount_irr, parsed_row.description
                )
                if parsed_row.bank_ref is not None:
                    ref_pair = (parsed_row.bank_ref, parsed_row.row_date.gregorian_date.isoformat())
                    if ref_pair in existing_refs or ref_pair in seen_refs:
                        is_duplicate, reason = True, "bank_ref"
                    seen_refs.add(ref_pair)
                if not is_duplicate and (
                    match_key in existing_keys or match_key in seen_keys
                ):
                    is_duplicate, reason = True, "match_key"
                seen_keys.add(match_key)
            rows.append(
                BankImportRow(
                    id=str(uuid.uuid4()),
                    batch_id=batch_id,
                    row_number=parsed_row.row_number,
                    row_date=parsed_row.row_date,
                    amount_irr=parsed_row.amount_irr,
                    description=parsed_row.description,
                    bank_ref=parsed_row.bank_ref,
                    balance_irr=parsed_row.balance_irr,
                    match_key=match_key,
                    is_duplicate=is_duplicate,
                    duplicate_reason=reason,
                    parse_errors=parsed_row.errors,
                    was_rounded=parsed_row.was_rounded,
                )
            )
        return rows

    def _suggest_coding(
        self,
        rows: list[BankImportRow],
        rules: Sequence[BankMatchRule],
        default_account_id: int,
    ) -> list[BankImportRow]:
        """Attach the preview coding to new rows (learned rules, else the default account)."""
        coded: list[BankImportRow] = []
        for row in rows:
            if not row.is_new:
                coded.append(row)
                continue
            direction = "in" if (row.amount_irr or 0) > 0 else "out"
            description = normalize_persian_text(row.description)
            hit: BankMatchRule | None = None
            for rule in rules:
                pattern = normalize_persian_text(rule.pattern)
                if not pattern or pattern not in description:
                    continue
                if rule.direction not in ("any", direction):
                    continue
                if hit is None or len(pattern) > len(normalize_persian_text(hit.pattern)):
                    hit = rule
            if hit is not None:
                coded.append(
                    dataclasses.replace(
                        row,
                        suggested_account_id=hit.target_account_id,
                        suggested_party_id=hit.target_party_id,
                        suggestion_source="rule",
                    )
                )
            else:
                coded.append(
                    dataclasses.replace(
                        row,
                        suggested_account_id=default_account_id,
                        suggestion_source="default",
                    )
                )
        return coded

    def _build_batch(
        self,
        *,
        batch_id: str,
        tenant_id: str,
        company_id: str,
        bank_key: str,
        statement_journal_id: int,
        filename: str,
        file_format: StatementFileFormat,
        currency_unit: FileCurrencyUnit,
        default_account_id: int,
        column_mapping_id: str | None,
        rows: list[BankImportRow],
    ) -> BankImportBatch:
        new_rows = [row for row in rows if row.is_new]
        deposits = sum(row.amount_irr or 0 for row in new_rows if (row.amount_irr or 0) > 0)
        withdrawals = sum(
            -(row.amount_irr or 0) for row in new_rows if (row.amount_irr or 0) < 0
        )
        start_balance: int | None = None
        end_balance: int | None = None
        balanced = [row for row in new_rows if row.balance_irr is not None]
        if balanced:
            first = balanced[0]
            end_balance = balanced[-1].balance_irr
            if first.amount_irr is not None and first.balance_irr is not None:
                start_balance = first.balance_irr - first.amount_irr
        return BankImportBatch(
            id=batch_id,
            tenant_id=tenant_id,
            company_id=company_id,
            bank_key=bank_key,
            statement_journal_id=statement_journal_id,
            filename=filename,
            file_format=file_format,
            currency_unit=currency_unit,
            status=ImportBatchStatus.AWAITING_REVIEW,
            total_rows=len(rows),
            new_rows=len(new_rows),
            duplicate_rows=sum(1 for row in rows if row.is_duplicate),
            invalid_rows=sum(1 for row in rows if not row.is_valid),
            total_deposits_irr=deposits,
            total_withdrawals_irr=withdrawals,
            default_account_id=default_account_id,
            column_mapping_id=column_mapping_id,
            start_balance_irr=start_balance,
            end_balance_irr=end_balance,
            created_at=self._clock(),
        )

    def _resolve_review_item(
        self, tenant_id: str, item_id: str, action_code: str, actor: str
    ) -> None:
        """Resolve a linked review item; an already-terminal item is tolerated."""
        try:
            self._review.perform_action(
                item_id=item_id, tenant_id=tenant_id, action_code=action_code, actor=actor
            )
        except ReviewInvalidStateError:
            # The item was already resolved elsewhere (e.g. from the
            # queue page); the import decision stands either way.
            return

    def _resolve_match_target(
        self,
        *,
        company: CompanyRef,
        target_type: MatchTargetType,
        target_id: str,
        account_id: int | None,
        party_id: int | None,
    ) -> tuple[str, AccountRef | None, PartyRef | None, InvoiceRef | None]:
        """Resolve a manual-match target to (label, account, party, invoice)."""
        if target_type == MatchTargetType.INVOICE:
            if not target_id.isdigit():
                raise InvoiceNotFoundError(
                    f"Invoice {target_id!r} is not a valid invoice id.",
                    details={"invoice_id": target_id},
                )
            invoice = self._engine.get_invoice(InvoiceRef(int(target_id)))
            if invoice is None:
                raise InvoiceNotFoundError(
                    f"Invoice {target_id} does not exist in the engine.",
                    details={"invoice_id": int(target_id)},
                )
            label = f"فاکتور {invoice.number or invoice.ref.engine_id} — {invoice.party_name}"
            return label, invoice.account, invoice.party, invoice.ref
        if target_type == MatchTargetType.PAYMENT:
            if not target_id.isdigit():
                raise PaymentNotFoundError(
                    f"Payment {target_id!r} is not a valid payment id.",
                    details={"payment_id": target_id},
                )
            payment = self._engine.get_payment(PaymentRef(int(target_id)))
            if payment is None:
                raise PaymentNotFoundError(
                    f"Payment {target_id} does not exist in the engine.",
                    details={"payment_id": int(target_id)},
                )
            if account_id is None:
                raise BankImportInvalidStateError(
                    "A payment match needs an explicit coding account.",
                    details={"payment_id": int(target_id)},
                )
            label = f"پرداخت {payment.number} — {payment.party_name}"
            return (
                label,
                AccountRef(account_id),
                payment.party,
                None,
            )
        if target_type == MatchTargetType.RULE:
            raise BankImportInvalidStateError(
                "A rule target cannot be applied by id alone; apply the rule's account as a manual match.",
                details={"rule_id": target_id},
            )
        # MANUAL: the operator codes the line to an account directly.
        if account_id is None:
            raise BankImportInvalidStateError(
                "A manual match needs a coding account.",
                details={},
            )
        return (
            "تطبیق دستی",
            AccountRef(account_id),
            PartyRef(party_id) if party_id is not None else None,
            None,
        )
