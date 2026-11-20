"""Bank-statement reconciliation use cases (US-514, B2).

Reconcile replaces the Phase 2 typed-501 stub. It runs the pure
three-priority matcher over a draft statement's lines, applies the
auto decisions through the engine port (line-field writes only —
``related_to``/account/party), records every decision, and files
``bank_unmatched`` review items for lines a human must decide.

Boundaries, documented in docs/bank-import.md:

- Reconcile never validates or posts the statement. The accountant
  validates in the engine, where Tryton's own ``Line.reconcile``
  reconciles lines whose ``related_to`` is set on validate.
- Priority 2 is amount + date (±``match_window_days``) + direction.
  The destination-Shaba check is per-candidate-impossible with
  today's data (``account.payment.journal`` has no bank-account link
  in Tryton 8.2 core); the statement journal's Shaba is recorded on
  every match instead, and the per-candidate destination filter is a
  5پ/5ث follow-up.
- Unmatched deposits NEVER auto-post to revenue. They wait in the
  review queue with mapping suggestions.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import Sequence

from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.errors import ReviewInvalidStateError
from hesabyar.domain.accounting.commands import UpdateBankStatementLine
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import (
    AccountRef,
    BankStatementLineRef,
    BankStatementRef,
    CompanyRef,
    InvoiceRef,
    PartyRef,
)
from hesabyar.domain.accounting.value_objects import (
    BankStatementLineView,
    BankStatementView,
    StatementJournalView,
)
from hesabyar.domain.bank.errors import (
    BankStatementLineNotFoundError,
    BankStatementNotDraftError,
)
from hesabyar.domain.bank.matching import LineToMatch, MatchCandidate, match_statement_line
from hesabyar.domain.bank.models import (
    BankMatchRule,
    BankStatementMatch,
    MatchStatus,
    MatchTargetType,
    ReconcileLineResult,
    ReconcileOutcome,
    ReconcileResult,
)
from hesabyar.domain.bank.ports import BankRepositoryPort
from hesabyar.domain.common.errors import BankStatementNotFoundError
from hesabyar.domain.integrations.ports import IntegrationsRegistryPort
from hesabyar.domain.review_queue.models import ReviewItemType, ReviewSeverity
from hesabyar.domain.review_queue.ports import ReviewQueuePort

#: Payment states open enough to match against (failed payments are dead).
MATCHABLE_PAYMENT_STATES = ("draft", "submitted", "approved", "processing", "succeeded")

#: Invoice states matchable — ``related_to`` requires a posted invoice.
MATCHABLE_INVOICE_STATES = ("posted", "paid")

#: Default match window when no installation settings row exists (F3 default).
DEFAULT_MATCH_WINDOW_DAYS = 2


class BankReconcileService:
    """Three-priority statement reconciliation over the engine port."""

    def __init__(
        self,
        engine: AccountingEnginePort,
        bank: BankRepositoryPort,
        review: ReviewQueueCommandService,
        queue: ReviewQueuePort,
        registry: IntegrationsRegistryPort,
        *,
        now: datetime.datetime | None = None,
    ) -> None:
        self._engine = engine
        self._bank = bank
        self._review = review
        self._queue = queue
        self._registry = registry
        self._now = now

    def _clock(self) -> datetime.datetime:
        if self._now is not None:
            return self._now
        return datetime.datetime.now(datetime.UTC)

    def reconcile(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        company: CompanyRef,
        company_id: str,
        statement_id: int,
        actor: str,
        window_days: int | None = None,
    ) -> ReconcileResult:
        """Reconcile one draft statement. Idempotent per line.

        Lines with a live match (auto-applied or human-confirmed) are
        reported ``already_matched`` and untouched; lines with a prior
        suggestion or unmatched record are re-decided, and a new
        review item is filed only when the old one is closed or
        missing — re-running never duplicates queue items.
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
                "only draft statements can be reconciled.",
                details={"statement_id": statement_id, "state": view.state},
            )
        window = window_days
        if window is None:
            settings = self._registry.get_installation_settings(installation_id)
            window = (
                settings.matching_window_days if settings is not None else DEFAULT_MATCH_WINDOW_DAYS
            )
        journals = {journal.id: journal for journal in self._engine.list_statement_journals(company)}
        journal = journals.get(view.journal_id)
        candidates = self._load_candidates(company)
        rules = list(self._bank.list_rules(tenant_id, company_id))
        prior = {
            match.statement_line_id: match
            for match in self._bank.list_matches_for_statement(tenant_id, statement_id)
            if match.status != MatchStatus.REJECTED
        }
        results: list[ReconcileLineResult] = []
        items_filed = 0
        for line in view.lines:
            live = prior.get(line.id)
            if live is not None and live.status in (
                MatchStatus.AUTO_APPLIED,
                MatchStatus.CONFIRMED,
            ):
                results.append(
                    ReconcileLineResult(
                        line_id=line.id,
                        line_date=line.date,
                        amount_irr=line.amount.amount,
                        description=line.description,
                        outcome=ReconcileOutcome.ALREADY_MATCHED,
                        priority=live.priority,
                        confidence=live.confidence,
                        target_type=live.target_type,
                        target_id=live.target_id if live.target_id is not None else live.target_ref,
                        target_label=live.target_label,
                        review_item_id=live.review_item_id,
                    )
                )
                continue
            outcome = match_statement_line(
                LineToMatch(
                    line_id=line.id,
                    date=line.date,
                    amount_irr=line.amount.amount,
                    description=line.description,
                    number=line.number,
                    related_invoice_id=(
                        line.related_invoice.engine_id if line.related_invoice else None
                    ),
                ),
                candidates,
                rules,
                window,
            )
            result, filed = self._apply_outcome(
                tenant_id=tenant_id,
                company_id=company_id,
                statement_id=statement_id,
                statement=view,
                line=line,
                journal=journal,
                outcome=outcome,
                prior=live,
                candidates=candidates,
                rules=rules,
                actor=actor,
            )
            results.append(result)
            items_filed += filed
        return ReconcileResult(
            statement_id=statement_id,
            company_id=company_id,
            total_lines=len(results),
            already_matched_count=sum(
                1 for r in results if r.outcome == ReconcileOutcome.ALREADY_MATCHED
            ),
            matched_count=sum(1 for r in results if r.outcome == ReconcileOutcome.MATCHED),
            priority1_count=sum(
                1
                for r in results
                if r.outcome == ReconcileOutcome.MATCHED and r.priority == 1
            ),
            priority2_count=sum(
                1
                for r in results
                if r.outcome == ReconcileOutcome.MATCHED and r.priority == 2
            ),
            suggested_count=sum(1 for r in results if r.outcome == ReconcileOutcome.SUGGESTED),
            ambiguous_count=sum(1 for r in results if r.outcome == ReconcileOutcome.AMBIGUOUS),
            unmatched_count=sum(1 for r in results if r.outcome == ReconcileOutcome.UNMATCHED),
            matched_total_irr=sum(
                abs(r.amount_irr) for r in results if r.outcome == ReconcileOutcome.MATCHED
            ),
            unmatched_total_irr=sum(
                abs(r.amount_irr)
                for r in results
                if r.outcome
                in (ReconcileOutcome.UNMATCHED, ReconcileOutcome.AMBIGUOUS)
            ),
            review_items_filed=items_filed,
            lines=tuple(results),
        )

    # -- candidates --------------------------------------------------------------

    def _load_candidates(self, company: CompanyRef) -> list[MatchCandidate]:
        candidates: list[MatchCandidate] = []
        for invoice in self._engine.list_invoices(company):
            if invoice.state not in MATCHABLE_INVOICE_STATES:
                continue
            if invoice.invoice_date is None and invoice.accounting_date is None:
                continue
            amount = invoice.total_amount.amount
            candidates.append(
                MatchCandidate(
                    kind=MatchTargetType.INVOICE,
                    id=invoice.ref.engine_id,
                    reference=invoice.reference or invoice.number or "",
                    number=invoice.number,
                    # The matcher compares magnitudes; the direction field carries the sign.
                    amount_irr=amount,
                    date=invoice.invoice_date or invoice.accounting_date,  # type: ignore[arg-type]
                    direction="in" if invoice.invoice_type == "out" else "out",
                    party_id=invoice.party.engine_id,
                    party_name=invoice.party_name,
                    account_id=invoice.account.engine_id,
                    label=f"فاکتور {invoice.number or invoice.ref.engine_id} — {invoice.party_name}",
                )
            )
        for payment in self._engine.list_payments(company):
            if payment.state not in MATCHABLE_PAYMENT_STATES:
                continue
            amount = payment.amount.amount
            candidates.append(
                MatchCandidate(
                    kind=MatchTargetType.PAYMENT,
                    id=payment.ref.engine_id,
                    reference=payment.reference or payment.number,
                    number=payment.number,
                    # The matcher compares magnitudes; the direction field carries the sign.
                    amount_irr=amount,
                    date=payment.date,
                    direction="in" if payment.kind == "receivable" else "out",
                    party_id=payment.party.engine_id,
                    party_name=payment.party_name,
                    account_id=None,
                    label=f"پرداخت {payment.number} — {payment.party_name}",
                )
            )
        return candidates

    # -- per-line application ------------------------------------------------------

    def _apply_outcome(
        self,
        *,
        tenant_id: str,
        company_id: str,
        statement_id: int,
        statement: BankStatementView,
        line: BankStatementLineView,
        journal: StatementJournalView | None,
        outcome: ReconcileLineResult,
        prior: BankStatementMatch | None,
        candidates: Sequence[MatchCandidate],
        rules: Sequence[BankMatchRule],
        actor: str,
    ) -> tuple[ReconcileLineResult, int]:
        if outcome.outcome in (ReconcileOutcome.ALREADY_MATCHED, ReconcileOutcome.SKIPPED_ZERO):
            return outcome, 0
        if outcome.outcome == ReconcileOutcome.MATCHED:
            self._apply_match(statement_id, line, outcome, candidates, rules)
            if prior is not None:
                self._supersede_prior(tenant_id, prior, actor, note="در اجرای تازهٔ تطبیق، خودکار تطبیق خورد")
            match = self._bank.save_match(
                self._new_match(
                    tenant_id=tenant_id,
                    company_id=company_id,
                    statement_id=statement_id,
                    line=line,
                    journal=journal,
                    outcome=outcome,
                    status=MatchStatus.AUTO_APPLIED,
                    actor="reconcile",
                )
            )
            return dataclasses.replace(outcome, review_item_id=match.review_item_id), 0
        if outcome.outcome in (ReconcileOutcome.SUGGESTED, ReconcileOutcome.AMBIGUOUS):
            return self._apply_needs_human(
                tenant_id=tenant_id,
                company_id=company_id,
                statement_id=statement_id,
                statement=statement,
                line=line,
                journal=journal,
                outcome=outcome,
                prior=prior,
                actor=actor,
            )
        # UNMATCHED — record that we looked, so a re-run does not re-file.
        if prior is not None and prior.status == MatchStatus.UNMATCHED:
            item_id = self._ensure_open_item(
                tenant_id=tenant_id,
                company_id=company_id,
                statement_id=statement_id,
                statement=statement,
                line=line,
                outcome=outcome,
                prior=prior,
                actor=actor,
            )
            return dataclasses.replace(outcome, review_item_id=item_id), (
                1 if item_id != prior.review_item_id else 0
            )
        if prior is not None:
            self._supersede_prior(tenant_id, prior, actor, note="در اجرای تازهٔ تطبیق، پیشنهادی پیدا نشد")
        match = self._bank.save_match(
            self._new_match(
                tenant_id=tenant_id,
                company_id=company_id,
                statement_id=statement_id,
                line=line,
                journal=journal,
                outcome=outcome,
                status=MatchStatus.UNMATCHED,
                actor="reconcile",
            )
        )
        item = self._file_unmatched_item(
            tenant_id=tenant_id,
            company_id=company_id,
            statement_id=statement_id,
            statement=statement,
            line=line,
            outcome=outcome,
            actor=actor,
        )
        self._bank.update_match(dataclasses.replace(match, review_item_id=item.id))
        return dataclasses.replace(outcome, review_item_id=item.id), 1

    def _apply_needs_human(
        self,
        *,
        tenant_id: str,
        company_id: str,
        statement_id: int,
        statement: BankStatementView,
        line: BankStatementLineView,
        journal: StatementJournalView | None,
        outcome: ReconcileLineResult,
        prior: BankStatementMatch | None,
        actor: str,
    ) -> tuple[ReconcileLineResult, int]:
        if not outcome.suggestions:
            # A suggested outcome with no suggestions is a matcher bug;
            # degrade to unmatched rather than recording a broken record.
            return self._apply_outcome(
                tenant_id=tenant_id,
                company_id=company_id,
                statement_id=statement_id,
                statement=statement,
                line=line,
                journal=journal,
                outcome=dataclasses.replace(outcome, outcome=ReconcileOutcome.UNMATCHED),
                prior=prior,
                candidates=(),
                rules=(),
                actor=actor,
            )
        top = outcome.suggestions[0]
        top_ref = str(top.target_id)
        if (
            prior is not None
            and prior.status == MatchStatus.SUGGESTED
            and prior.target_type == top.target_type
            and prior.target_ref == top_ref
        ):
            item_id = self._ensure_open_item(
                tenant_id=tenant_id,
                company_id=company_id,
                statement_id=statement_id,
                statement=statement,
                line=line,
                outcome=outcome,
                prior=prior,
                actor=actor,
            )
            return dataclasses.replace(outcome, review_item_id=item_id), (
                1 if item_id != prior.review_item_id else 0
            )
        if prior is not None:
            self._supersede_prior(tenant_id, prior, actor, note="پیشنهاد تازه‌ای جایگزین شد")
        match = self._bank.save_match(
            self._new_match(
                tenant_id=tenant_id,
                company_id=company_id,
                statement_id=statement_id,
                line=line,
                journal=journal,
                outcome=dataclasses.replace(
                    outcome,
                    target_type=top.target_type,
                    target_id=top.target_id,
                    target_label=top.target_label,
                    confidence=top.confidence,
                ),
                status=MatchStatus.SUGGESTED,
                actor="reconcile",
            )
        )
        item = self._file_unmatched_item(
            tenant_id=tenant_id,
            company_id=company_id,
            statement_id=statement_id,
            statement=statement,
            line=line,
            outcome=outcome,
            actor=actor,
        )
        self._bank.update_match(dataclasses.replace(match, review_item_id=item.id))
        return dataclasses.replace(outcome, review_item_id=item.id), 1

    # -- engine writes ---------------------------------------------------------------

    def _apply_match(
        self,
        statement_id: int,
        line: BankStatementLineView,
        outcome: ReconcileLineResult,
        candidates: Sequence[MatchCandidate],
        rules: Sequence[BankMatchRule],
    ) -> None:
        """Write one auto decision onto the engine line (no validate/post)."""
        if outcome.target_type == MatchTargetType.RULE:
            rule = next((r for r in rules if r.id == str(outcome.target_id)), None)
            if rule is None:
                raise BankStatementLineNotFoundError(
                    f"Rule {outcome.target_id} vanished mid-reconcile.",
                    details={"statement_id": statement_id, "line_id": line.id},
                )
            update = UpdateBankStatementLine(
                statement=BankStatementRef(statement_id),
                line=BankStatementLineRef(line.id),
                account=AccountRef(rule.target_account_id),
                party=PartyRef(rule.target_party_id) if rule.target_party_id else None,
            )
        elif outcome.target_type == MatchTargetType.INVOICE:
            candidate = next(
                (c for c in candidates if c.kind == MatchTargetType.INVOICE and c.id == outcome.target_id),
                None,
            )
            if candidate is None:
                raise BankStatementLineNotFoundError(
                    f"Invoice {outcome.target_id} vanished mid-reconcile.",
                    details={"statement_id": statement_id, "line_id": line.id},
                )
            update = UpdateBankStatementLine(
                statement=BankStatementRef(statement_id),
                line=BankStatementLineRef(line.id),
                account=AccountRef(candidate.account_id)
                if candidate.account_id is not None
                else None,
                party=PartyRef(candidate.party_id) if candidate.party_id else None,
                related_invoice=InvoiceRef(candidate.id),
            )
        elif outcome.target_type == MatchTargetType.PAYMENT:
            candidate = next(
                (c for c in candidates if c.kind == MatchTargetType.PAYMENT and c.id == outcome.target_id),
                None,
            )
            if candidate is None:
                raise BankStatementLineNotFoundError(
                    f"Payment {outcome.target_id} vanished mid-reconcile.",
                    details={"statement_id": statement_id, "line_id": line.id},
                )
            # Payments carry no account on the view (and related_to is
            # invoices-only): the match sets the party and keeps the
            # line's coded account.
            update = UpdateBankStatementLine(
                statement=BankStatementRef(statement_id),
                line=BankStatementLineRef(line.id),
                party=PartyRef(candidate.party_id) if candidate.party_id else None,
            )
        else:
            raise BankStatementLineNotFoundError(
                f"Cannot apply a match with no target for line {line.id}.",
                details={"statement_id": statement_id, "line_id": line.id},
            )
        self._engine.update_bank_statement_line(update)

    # -- match records + review items ---------------------------------------------------

    def _new_match(
        self,
        *,
        tenant_id: str,
        company_id: str,
        statement_id: int,
        line: BankStatementLineView,
        journal: StatementJournalView | None,
        outcome: ReconcileLineResult,
        status: MatchStatus,
        actor: str,
    ) -> BankStatementMatch:
        target_id = outcome.target_id
        return BankStatementMatch(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            company_id=company_id,
            statement_id=statement_id,
            statement_line_id=line.id,
            line_date=line.date,
            line_amount_irr=line.amount.amount,
            line_description=line.description,
            target_type=outcome.target_type or MatchTargetType.MANUAL,
            target_id=target_id if isinstance(target_id, int) else None,
            target_ref=str(target_id) if target_id is not None else "",
            target_label=outcome.target_label or "",
            priority=outcome.priority or 3,
            confidence=outcome.confidence or 0,
            status=status,
            outcome=outcome.outcome,
            destination_account_id=journal.account.engine_id if journal else None,
            destination_shaba=journal.bank_account_number if journal else None,
            matched_at=self._clock() if status in (MatchStatus.AUTO_APPLIED,) else None,
            matched_by=actor if status == MatchStatus.AUTO_APPLIED else None,
        )

    def _supersede_prior(
        self, tenant_id: str, prior: BankStatementMatch, actor: str, *, note: str
    ) -> None:
        """Mark a prior suggestion/unmatched record superseded and resolve its item."""
        self._bank.update_match(dataclasses.replace(prior, status=MatchStatus.REJECTED))
        if prior.review_item_id is None:
            return
        try:
            self._review.perform_action(
                item_id=prior.review_item_id,
                tenant_id=tenant_id,
                action_code="manual_match",
                actor=actor,
                note=note,
            )
        except ReviewInvalidStateError:
            return

    def _ensure_open_item(
        self,
        *,
        tenant_id: str,
        company_id: str,
        statement_id: int,
        statement: BankStatementView,
        line: BankStatementLineView,
        outcome: ReconcileLineResult,
        prior: BankStatementMatch,
        actor: str,
    ) -> str | None:
        """Reuse a prior record's still-open review item, or file a fresh one."""
        if prior.review_item_id is not None:
            item = self._queue.get_item(prior.review_item_id, tenant_id)
            if item is not None and not item.is_terminal:
                return item.id
        item = self._file_unmatched_item(
            tenant_id=tenant_id,
            company_id=company_id,
            statement_id=statement_id,
            statement=statement,
            line=line,
            outcome=outcome,
            actor=actor,
        )
        self._bank.update_match(dataclasses.replace(prior, review_item_id=item.id))
        return item.id

    def _file_unmatched_item(
        self,
        *,
        tenant_id: str,
        company_id: str,
        statement_id: int,
        statement: BankStatementView,
        line: BankStatementLineView,
        outcome: ReconcileLineResult,
        actor: str,
    ):
        direction = "واریز" if line.amount.amount > 0 else "برداشت"
        suggestions = "\n".join(
            f"• {s.target_label} (اطمینان {s.confidence}٪)"
            for s in outcome.suggestions
        )
        reason = (
            f"ردیف {direction} {abs(line.amount.amount):,} ریال در صورت‌حساب «{statement.name}» "
            f"تطبیق نخورد — {line.description or 'بدون شرح'}"
        )
        if suggestions:
            reason += f"\nپیشنهادها:\n{suggestions}"
        return self._review.file_item(
            tenant_id=tenant_id,
            item_type=ReviewItemType.BANK_UNMATCHED,
            severity=ReviewSeverity.WARNING if line.amount.amount > 0 else ReviewSeverity.INFO,
            reason=reason,
            suggested_action=(
                "در صفحهٔ تطبیق صورت‌حساب، این ردیف را به فاکتور یا پرداخت درست وصل کنید "
                "یا حساب آن را به‌صورت دستی مشخص کنید؛ واریزهای تطبیق‌نخورده هرگز خودکار به درآمد نمی‌روند."
            ),
            company_id=company_id,
            source_event_ref=f"bank-statement:{statement_id}:line:{line.id}",
            amount_irr=abs(line.amount.amount),
            actor=actor,
        )
