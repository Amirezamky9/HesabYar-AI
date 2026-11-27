"""Cheque clearing service (US-534, phased design §5 §1.5).

Layer 3 runs ON the US-514 statement-import infrastructure: it
reads a draft statement through the engine port, matches its lines
against in-clearing cheques with the pure matcher in
:mod:`hesabyar.domain.cheques.clearing`, records every decision as
a :class:`BankStatementMatch` (so a re-run never re-decides a live
match and the generic reconcile never double-matches the line), and
files review items for everything a human must decide.

- ``exact`` + received commercial cheque -> the engine ``clear``
  transition (سند ۳, through the policy gate: auto-post only with
  explicit opt-in, else draft + one-click confirm) and then the
  clearing-leg reconciliation: the deposit move's clearing-account
  debit (سند ۲) reconciled with the clear move's clearing-account
  credit (سند ۳) via ``reconcile_move_lines`` (``account_statement``
  reconcile). A reconciliation failure never un-clears the cheque —
  it files a ``clearing_reconcile_failed`` review item.
- ``exact`` + issued cheque -> review item only: issued-cheque
  clearing lands with US-532 (the engine refuses issued
  transitions until then); the match is recorded, never applied.
- ``probable`` / ``ambiguous`` -> review queue with the candidate
  list; a human confirms with :meth:`confirm_match`. ``ambiguous``
  is NEVER guessed.
- ``unmatched`` -> the line stays unmatched (the generic bank
  reconcile files its ``bank_unmatched`` item); the cheque stays in
  ``in_clearing`` for the aging report.

NEVER auto-bounce: this service has no bounce path at all. A stale
in-clearing cheque only appears flagged in
:meth:`aging_report` / :meth:`flag_stale_in_clearing` (report 4).
Partial clearing does not exist: a mismatched deposit is
``unmatched`` + manual follow-up, never a partial clear.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import uuid
from dataclasses import dataclass

from hesabyar.application.commands.cheques import ChequeCommandService
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.errors import ReviewInvalidStateError
from hesabyar.domain.accounting.commands import ReconcileMoveLines
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import (
    BankStatementRef,
    CompanyRef,
    JournalDraftRef,
    MoveLineRef,
)
from hesabyar.domain.bank.errors import BankStatementNotDraftError
from hesabyar.domain.bank.models import (
    BankStatementMatch,
    MatchStatus,
    MatchTargetType,
    ReconcileOutcome,
)
from hesabyar.domain.bank.ports import BankRepositoryPort
from hesabyar.domain.cash_forecast.forecast import jalali_month_key
from hesabyar.domain.cheques.clearing import (
    ClearingAging,
    ClearingCandidate,
    ClearingConfidence,
    ClearingLine,
    ClearingMatch,
    aging_for,
    match_clearing_line,
)
from hesabyar.domain.cheques.errors import (
    ChequeNotFoundError,
    ChequeValidationError,
)
from hesabyar.domain.cheques.models import (
    Cheque,
    ChequeKind,
    ChequePurpose,
    ChequeState,
    MonthlyMatchHistory,
)
from hesabyar.domain.cheques.ports import ChequeRepositoryPort
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import BankStatementNotFoundError
from hesabyar.domain.review_queue.models import ReviewItemType, ReviewSeverity
from hesabyar.domain.review_queue.ports import ReviewItemFilters, ReviewQueuePort

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ClearingLineDecision:
    """What one statement line's clearing decision did."""

    line_id: int
    confidence: ClearingConfidence
    cheque_id: str | None = None
    sayad_id: str | None = None
    action: str = "none"  # 'cleared' | 'review' | 'issued_pending' | 'already_matched' | 'none'
    reconciliation_id: int | None = None
    review_item_id: str | None = None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ChequeClearingResult:
    """Outcome of one cheque-clearing run over a statement."""

    statement_id: int
    total_lines: int
    exact_count: int
    probable_count: int
    ambiguous_count: int
    unmatched_count: int
    cleared_count: int
    review_items_filed: int
    lines: tuple[ClearingLineDecision, ...] = ()


@dataclass(frozen=True, slots=True)
class StaleFlagReport:
    """Outcome of one stale in-clearing flagging run."""

    checked: int = 0
    stale: int = 0
    items_filed: int = 0
    cheque_ids: tuple[str, ...] = ()


class ChequeClearingService:
    """Match statement lines to in-clearing cheques and clear the exact ones."""

    def __init__(
        self,
        engine: AccountingEnginePort,
        cheques: ChequeRepositoryPort,
        bank: BankRepositoryPort,
        cheque_service: ChequeCommandService,
        review: ReviewQueueCommandService,
        queue: ReviewQueuePort | None = None,
        *,
        today: datetime.date | None = None,
    ) -> None:
        self._engine = engine
        self._cheques = cheques
        self._bank = bank
        self._cheque_service = cheque_service
        self._review = review
        self._queue = queue
        self._today = today

    def _clock_date(self) -> datetime.date:
        return self._today or datetime.date.today()

    def get_cheque(
        self, cheque_id: str, *, tenant_id: str, installation_id: str
    ) -> Cheque:
        """Load one cheque, tenant/installation-scoped (router access checks use this)."""
        return self._require_cheque(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )

    # -- run -------------------------------------------------------------------

    def match_statement(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        company: CompanyRef,
        company_id: str,
        statement_id: int,
        actor: str,
        bank_key: str | None = None,
    ) -> ChequeClearingResult:
        """Run clearing matching over one draft statement.

        Idempotent per line: a line with a live (auto-applied or
        confirmed) match is reported ``already_matched`` and
        untouched; a re-run re-decides suggested/unmatched lines and
        supersedes their prior records (and resolves their items).
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
                "only draft statements can be clearing-matched.",
                details={"statement_id": statement_id, "state": view.state},
            )
        journals = {
            journal.id: journal for journal in self._engine.list_statement_journals(company)
        }
        journal = journals.get(view.journal_id)
        destination_account_id = journal.account.engine_id if journal else None
        settings = self._cheques.get_settings(installation_id)
        window_days = settings.clearing_window_days if settings is not None else None
        candidates = self._load_candidates(
            tenant_id=tenant_id, installation_id=installation_id
        )
        prior = {
            match.statement_line_id: match
            for match in self._bank.list_matches_for_statement(tenant_id, statement_id)
            if match.status != MatchStatus.REJECTED
        }
        decisions: list[ClearingLineDecision] = []
        items_filed = 0
        for line in view.lines:
            live = prior.get(line.id)
            if live is not None and live.status in (
                MatchStatus.AUTO_APPLIED,
                MatchStatus.CONFIRMED,
            ):
                decisions.append(
                    ClearingLineDecision(
                        line_id=line.id,
                        confidence=ClearingConfidence.EXACT,
                        action="already_matched",
                        review_item_id=live.review_item_id,
                        reasons=("Line already has a live match; untouched.",),
                    )
                )
                continue
            clearing_line = ClearingLine(
                line_id=line.id,
                statement_id=statement_id,
                date=line.date,
                amount_irr=line.amount.amount,
                description=line.description,
                number=line.number,
                destination_account_id=destination_account_id,
                bank_key=bank_key,
            )
            match = match_clearing_line(
                clearing_line, candidates, clearing_window_days=window_days
            )
            decision, filed = self._apply_match(
                tenant_id=tenant_id,
                installation_id=installation_id,
                company=company,
                company_id=company_id,
                statement_id=statement_id,
                line=clearing_line,
                journal_shaba=journal.bank_account_number if journal else None,
                match=match,
                prior=live,
                actor=actor,
            )
            decisions.append(decision)
            items_filed += filed
        self._record_match_history(
            tenant_id=tenant_id,
            installation_id=installation_id,
            decisions=decisions,
        )
        return ChequeClearingResult(
            statement_id=statement_id,
            total_lines=len(decisions),
            exact_count=sum(
                1 for d in decisions if d.confidence == ClearingConfidence.EXACT
            ),
            probable_count=sum(
                1 for d in decisions if d.confidence == ClearingConfidence.PROBABLE
            ),
            ambiguous_count=sum(
                1 for d in decisions if d.confidence == ClearingConfidence.AMBIGUOUS
            ),
            unmatched_count=sum(
                1 for d in decisions if d.confidence == ClearingConfidence.UNMATCHED
            ),
            cleared_count=sum(1 for d in decisions if d.action == "cleared"),
            review_items_filed=items_filed,
            lines=tuple(decisions),
        )

    def _record_match_history(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        decisions: list[ClearingLineDecision],
    ) -> None:
        """Record this run's newly decided lines in the monthly history (US-553).

        Report 12 renders FROM the stored history, so every run
        adds its counts to its Jalali month. Only NEWLY decided
        lines count: a re-run's ``already_matched`` lines were
        recorded by the run that decided them — counting them again
        would inflate the month. A history-write failure must never
        fail the clearing run (the run's job is clearing); it is
        logged loudly instead, and the month simply misses this run.
        """
        newly_decided = [d for d in decisions if d.action != "already_matched"]
        if not newly_decided:
            return
        entry = MonthlyMatchHistory(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            installation_id=installation_id,
            month_jalali=jalali_month_key(self._clock_date()),
            exact_count=sum(
                1 for d in newly_decided if d.confidence == ClearingConfidence.EXACT
            ),
            probable_count=sum(
                1 for d in newly_decided if d.confidence == ClearingConfidence.PROBABLE
            ),
            ambiguous_count=sum(
                1 for d in newly_decided if d.confidence == ClearingConfidence.AMBIGUOUS
            ),
            unmatched_count=sum(
                1 for d in newly_decided if d.confidence == ClearingConfidence.UNMATCHED
            ),
        )
        try:
            self._cheques.record_match_history(entry)
        except Exception:  # noqa: BLE001 - a history failure must not fail the clearing run
            logger.exception(
                "Failed to record monthly match history for %s/%s",
                tenant_id,
                entry.month_jalali,
            )

    # -- human confirm of a probable/ambiguous match ---------------------------

    def confirm_match(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        company: CompanyRef,
        company_id: str,
        cheque_id: str,
        statement_id: int,
        line_id: int,
        actor: str,
    ) -> ClearingLineDecision:
        """Human-confirms a suggested clearing match: clear the cheque.

        The line must carry a SUGGESTED match pointing at this
        cheque (probable/ambiguous review). The cheque must be a
        received commercial cheque in ``in_clearing``; anything else
        fails closed with the reason.
        """
        cheque = self._require_cheque(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        if cheque.state != ChequeState.IN_CLEARING:
            raise ChequeValidationError(
                f"Cheque {cheque.sayad_id} is {cheque.state.value}, not in_clearing.",
                field_errors={"state": "Only an in-clearing cheque can be clearing-confirmed."},
            )
        if cheque.kind != ChequeKind.RECEIVED or cheque.purpose != ChequePurpose.PAYMENT:
            raise ChequeValidationError(
                "Only a received commercial cheque can be cleared this way.",
                field_errors={
                    "kind": f"Cheque is {cheque.kind.value}/{cheque.purpose.value}.",
                },
            )
        prior = next(
            (
                match
                for match in self._bank.list_matches_for_statement(tenant_id, statement_id)
                if match.statement_line_id == line_id
                and match.status == MatchStatus.SUGGESTED
                and match.target_ref == f"cheque:{cheque.id}"
            ),
            None,
        )
        if prior is None:
            raise ChequeValidationError(
                f"No suggested clearing match for cheque {cheque.sayad_id} on "
                f"statement {statement_id} line {line_id}.",
                field_errors={"line_id": "No suggested match to confirm."},
            )
        statement_ref = f"statement:{statement_id}:line:{line_id}"
        saved, reconciliation_id = self._clear_received(
            tenant_id=tenant_id,
            installation_id=installation_id,
            company=company,
            cheque=cheque,
            actor=actor,
            statement_ref=statement_ref,
            confidence="probable",
        )
        self._bank.update_match(
            dataclasses.replace(
                prior,
                status=MatchStatus.CONFIRMED,
                outcome=ReconcileOutcome.MATCHED,
                confidence=100,
            )
        )
        return ClearingLineDecision(
            line_id=line_id,
            confidence=ClearingConfidence.PROBABLE,
            cheque_id=saved.id,
            sayad_id=saved.sayad_id,
            action="cleared",
            reconciliation_id=reconciliation_id,
            reasons=("Human-confirmed probable match; cheque cleared.",),
        )

    # -- aging (report 4) ------------------------------------------------------

    def aging_report(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        today: datetime.date | None = None,
    ) -> tuple[ClearingAging, ...]:
        """Ages of all in-clearing cheques (stale ones only flagged, never bounced)."""
        day = FiscalDate(today or self._clock_date())
        settings = self._cheques.get_settings(installation_id)
        window_days = settings.clearing_window_days if settings is not None else None
        return tuple(
            aging_for(candidate, day, clearing_window_days=window_days)
            for candidate in self._load_candidates(
                tenant_id=tenant_id, installation_id=installation_id
            )
        )

    def flag_stale_in_clearing(
        self, *, tenant_id: str, installation_id: str, actor: str
    ) -> StaleFlagReport:
        """File ``stale_in_clearing`` items for cheques past the window.

        A stale cheque STAYS in_clearing — this only flags it for
        manual follow-up (a bounce still needs the bank's
        non-payment certificate or an inquiry). One open item per
        cheque; re-runs file nothing new while the item is open.
        """
        aged = self.aging_report(tenant_id=tenant_id, installation_id=installation_id)
        already_open = self._open_stale_refs(tenant_id)
        stale = [row for row in aged if row.stale]
        filed = 0
        for row in stale:
            ref = f"cheque:{row.cheque_id}"
            if ref in already_open:
                continue
            cheque = self._cheques.get(
                row.cheque_id, tenant_id=tenant_id, installation_id=installation_id
            )
            if cheque is None:
                continue
            try:
                self._review.file_item(
                    tenant_id=tenant_id,
                    item_type=ReviewItemType.CHEQUE_EXCEPTION,
                    severity=ReviewSeverity.WARNING,
                    reason=(
                        f"Cheque {row.sayad_id} has been in clearing for "
                        f"{row.days_in_clearing} day(s) (window "
                        f"{row.window_days} days) with no matching deposit. "
                        "It stays in_clearing — a bounce needs the bank's "
                        "non-payment certificate or an inquiry, never age alone."
                    ),
                    suggested_action=(
                        "Follow up with the bank for the deposit or the "
                        "non-payment certificate; do NOT auto-bounce on age."
                    ),
                    company_id=str(cheque.company_engine_id),
                    subtype="stale_in_clearing",
                    source_event_ref=ref,
                    amount_irr=cheque.amount_irr,
                    actor=actor,
                )
                filed += 1
            except Exception:  # noqa: BLE001 - one filing failure must not stop the run
                logger.exception("Failed to file stale-in-clearing item for %s", row.cheque_id)
        return StaleFlagReport(
            checked=len(aged),
            stale=len(stale),
            items_filed=filed,
            cheque_ids=tuple(row.cheque_id for row in stale),
        )

    # -- candidates --------------------------------------------------------------

    def _load_candidates(
        self, *, tenant_id: str, installation_id: str
    ) -> list[ClearingCandidate]:
        candidates: list[ClearingCandidate] = []
        for cheque in self._cheques.list(
            tenant_id=tenant_id,
            installation_id=installation_id,
            state=ChequeState.IN_CLEARING,
            limit=1000,
        ):
            candidates.append(
                ClearingCandidate(
                    cheque_id=cheque.id,
                    sayad_id=cheque.sayad_id,
                    leaf_number=cheque.leaf_number,
                    amount_irr=cheque.amount_irr,
                    deposit_date=cheque.deposit_date,
                    destination_bank_account_id=cheque.destination_bank_account_id,
                    direction="in" if cheque.kind == ChequeKind.RECEIVED else "out",
                    kind=cheque.kind.value,
                    state=cheque.state.value,
                )
            )
        return candidates

    def _require_cheque(
        self, cheque_id: str, *, tenant_id: str, installation_id: str
    ) -> Cheque:
        cheque = self._cheques.get(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        if cheque is None:
            raise ChequeNotFoundError(cheque_id)
        return cheque

    # -- per-line application ------------------------------------------------------

    def _apply_match(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        company: CompanyRef,
        company_id: str,
        statement_id: int,
        line: ClearingLine,
        journal_shaba: str | None,
        match: ClearingMatch,
        prior: BankStatementMatch | None,
        actor: str,
    ) -> tuple[ClearingLineDecision, int]:
        if prior is not None:
            self._supersede_prior(tenant_id, prior, actor)
        if match.confidence == ClearingConfidence.UNMATCHED:
            self._bank.save_match(
                self._new_match(
                    tenant_id=tenant_id,
                    company_id=company_id,
                    statement_id=statement_id,
                    line=line,
                    journal_shaba=journal_shaba,
                    match=match,
                    status=MatchStatus.UNMATCHED,
                    actor=actor,
                )
            )
            return ClearingLineDecision(
                line_id=line.line_id,
                confidence=ClearingConfidence.UNMATCHED,
                reasons=match.reasons,
            ), 0
        if match.confidence == ClearingConfidence.AMBIGUOUS:
            return self._apply_needs_human(
                tenant_id=tenant_id,
                installation_id=installation_id,
                company_id=company_id,
                statement_id=statement_id,
                line=line,
                journal_shaba=journal_shaba,
                match=match,
                subtype="clearing_ambiguous",
                severity=ReviewSeverity.WARNING,
                reason_prefix="Several same-amount cheques fit this line — never guessing.",
                actor=actor,
            )
        # exact / probable: one candidate.
        candidate = match.candidates[0]
        cheque = self._cheques.get(
            candidate.cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        if cheque is None or cheque.state != ChequeState.IN_CLEARING:
            return self._apply_needs_human(
                tenant_id=tenant_id,
                installation_id=installation_id,
                company_id=company_id,
                statement_id=statement_id,
                line=line,
                journal_shaba=journal_shaba,
                match=match,
                subtype="clearing_candidate_gone",
                severity=ReviewSeverity.WARNING,
                reason_prefix="The matched cheque is no longer in_clearing.",
                actor=actor,
            )
        if match.confidence == ClearingConfidence.EXACT and (
            cheque.kind == ChequeKind.RECEIVED and cheque.purpose == ChequePurpose.PAYMENT
        ):
            statement_ref = f"statement:{statement_id}:line:{line.line_id}"
            saved, reconciliation_id = self._clear_received(
                tenant_id=tenant_id,
                installation_id=installation_id,
                company=company,
                cheque=cheque,
                actor=actor,
                statement_ref=statement_ref,
                confidence="exact",
            )
            self._bank.save_match(
                self._new_match(
                    tenant_id=tenant_id,
                    company_id=company_id,
                    statement_id=statement_id,
                    line=line,
                    journal_shaba=journal_shaba,
                    match=match,
                    status=MatchStatus.AUTO_APPLIED,
                    actor=actor,
                    cheque=saved,
                )
            )
            return ClearingLineDecision(
                line_id=line.line_id,
                confidence=ClearingConfidence.EXACT,
                cheque_id=saved.id,
                sayad_id=saved.sayad_id,
                action="cleared",
                reconciliation_id=reconciliation_id,
                reasons=match.reasons,
            ), 0
        if match.confidence == ClearingConfidence.EXACT and cheque.kind == ChequeKind.ISSUED:
            return self._apply_needs_human(
                tenant_id=tenant_id,
                installation_id=installation_id,
                company_id=company_id,
                statement_id=statement_id,
                line=line,
                journal_shaba=journal_shaba,
                match=match,
                subtype="issued_clearing_pending_us532",
                severity=ReviewSeverity.INFO,
                reason_prefix=(
                    "An issued cheque matched this withdrawal exactly, but "
                    "issued-cheque clearing lands with US-532 (the engine "
                    "refuses issued transitions until then)."
                ),
                actor=actor,
                action="issued_pending",
            )
        # Probable (received or issued) and exact non-commercial: human decides.
        return self._apply_needs_human(
            tenant_id=tenant_id,
            installation_id=installation_id,
            company_id=company_id,
            statement_id=statement_id,
            line=line,
            journal_shaba=journal_shaba,
            match=match,
            subtype="clearing_probable",
            severity=ReviewSeverity.WARNING,
            reason_prefix="One candidate fits without a trusted leaf number.",
            actor=actor,
        )

    def _apply_needs_human(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        company_id: str,
        statement_id: int,
        line: ClearingLine,
        journal_shaba: str | None,
        match: ClearingMatch,
        subtype: str,
        severity: ReviewSeverity,
        reason_prefix: str,
        actor: str,
        action: str = "review",
    ) -> tuple[ClearingLineDecision, int]:
        candidate_lines = "\n".join(
            f"• cheque {candidate.sayad_id} ({candidate.kind}, "
            f"{candidate.amount_irr:,} Rial)"
            for candidate in match.candidates
        )
        item = self._review.file_item(
            tenant_id=tenant_id,
            item_type=ReviewItemType.CHEQUE_EXCEPTION,
            severity=severity,
            reason=(
                f"{reason_prefix} Statement {statement_id} line {line.line_id} "
                f"({abs(line.amount_irr):,} Rial, {line.description or 'no description'}).\n"
                f"Candidates:\n{candidate_lines}"
            ),
            suggested_action=(
                "Open the clearing match, verify the leaf number and "
                "issuing bank against the statement line, then confirm "
                "the right cheque (or clear it manually if the statement "
                "has not been imported yet)."
            ),
            company_id=company_id,
            subtype=subtype,
            source_event_ref=f"bank-statement:{statement_id}:line:{line.line_id}",
            amount_irr=abs(line.amount_irr),
            actor=actor,
        )
        self._bank.save_match(
            self._new_match(
                tenant_id=tenant_id,
                company_id=company_id,
                statement_id=statement_id,
                line=line,
                journal_shaba=journal_shaba,
                match=match,
                status=MatchStatus.SUGGESTED,
                actor=actor,
                review_item_id=item.id,
            )
        )
        return ClearingLineDecision(
            line_id=line.line_id,
            confidence=match.confidence,
            cheque_id=match.candidates[0].cheque_id if len(match.candidates) == 1 else None,
            sayad_id=match.candidates[0].sayad_id if len(match.candidates) == 1 else None,
            action=action,
            review_item_id=item.id,
            reasons=match.reasons,
        ), 1

    # -- clearing + leg reconciliation -------------------------------------------

    def _clear_received(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        company: CompanyRef,
        cheque: Cheque,
        actor: str,
        statement_ref: str,
        confidence: str,
    ) -> tuple[Cheque, int | None]:
        """Clear one received commercial cheque + reconcile its clearing legs.

        The engine ``clear`` runs first (سند ۳, through the policy
        gate). Then the deposit move's clearing-account debit (سند ۲)
        is reconciled with the clear move's clearing-account credit
        (سند ۳) — same account, same party, equal amounts. A
        reconciliation failure files a review item and returns None
        for the id; the cheque stays cleared.
        """
        saved = self._cheque_service.clear(
            tenant_id=tenant_id,
            installation_id=installation_id,
            cheque_id=cheque.id,
            actor=actor,
            statement_reference=statement_ref,
        )
        saved.clearing_confidence = confidence
        saved.clearing_statement_ref = statement_ref
        reconciliation_id: int | None = None
        try:
            reconciliation_id = self._reconcile_clearing_legs(company, saved)
        except Exception as exc:  # noqa: BLE001 - a failed reconcile must not un-clear the cheque
            logger.exception(
                "Clearing-leg reconciliation failed for cheque %s", saved.id
            )
            self._review.file_item(
                tenant_id=tenant_id,
                item_type=ReviewItemType.CHEQUE_EXCEPTION,
                severity=ReviewSeverity.WARNING,
                reason=(
                    f"Cheque {saved.sayad_id} cleared from {statement_ref} but "
                    f"its clearing legs did not reconcile: {exc}"
                ),
                suggested_action=(
                    "Reconcile the deposit move's clearing leg (سند ۲) with "
                    "the clear move's clearing leg (سند ۳) in the engine; "
                    "the cheque itself is cleared."
                ),
                company_id=str(saved.company_engine_id),
                subtype="clearing_reconcile_failed",
                source_event_ref=f"cheque:{saved.id}",
                amount_irr=saved.amount_irr,
                actor=actor,
            )
        if reconciliation_id is not None:
            saved.clearing_reconciliation_id = reconciliation_id
        saved.touch()
        return self._cheques.save(saved), reconciliation_id

    def _reconcile_clearing_legs(self, company: CompanyRef, cheque: Cheque) -> int | None:
        """Reconcile سند ۲'s clearing debit with سند ۳'s clearing credit.

        Returns the engine reconciliation id, or None when either
        move is missing (nothing to reconcile yet). Raises on a real
        engine failure so the caller can file the follow-up item.
        """
        deposit_move_id = cheque.move_ids.get("deposit")
        clear_move_id = cheque.move_ids.get("clear")
        if deposit_move_id is None or clear_move_id is None:
            return None
        deposit_lines = self._engine.list_move_lines(
            company, move=JournalDraftRef(engine_id=deposit_move_id)
        )
        clear_lines = self._engine.list_move_lines(
            company, move=JournalDraftRef(engine_id=clear_move_id)
        )
        deposit_leg = next(
            (line for line in deposit_lines if line.balance.amount == cheque.amount_irr),
            None,
        )
        clear_leg = next(
            (line for line in clear_lines if line.balance.amount == -cheque.amount_irr),
            None,
        )
        if deposit_leg is None or clear_leg is None:
            raise ChequeValidationError(
                "Clearing legs not found on the deposit/clear moves.",
                field_errors={
                    "cheque_id": cheque.id,
                    "deposit_move_id": str(deposit_move_id),
                    "clear_move_id": str(clear_move_id),
                },
            )
        if deposit_leg.account.engine_id != clear_leg.account.engine_id:
            raise ChequeValidationError(
                "Clearing legs are on different accounts; refusing to reconcile.",
                field_errors={
                    "cheque_id": cheque.id,
                    "deposit_account": str(deposit_leg.account.engine_id),
                    "clear_account": str(clear_leg.account.engine_id),
                },
            )
        result = self._engine.reconcile_move_lines(
            ReconcileMoveLines(
                company=company,
                lines=(MoveLineRef(deposit_leg.ref.engine_id), MoveLineRef(clear_leg.ref.engine_id)),
            )
        )
        return result.ref.engine_id

    # -- match records + review items --------------------------------------------

    def _new_match(
        self,
        *,
        tenant_id: str,
        company_id: str,
        statement_id: int,
        line: ClearingLine,
        journal_shaba: str | None,
        match: ClearingMatch,
        status: MatchStatus,
        actor: str,
        cheque: Cheque | None = None,
        review_item_id: str | None = None,
    ) -> BankStatementMatch:
        candidate = match.candidates[0] if len(match.candidates) == 1 else None
        matched_cheque_id = cheque.id if cheque is not None else (
            candidate.cheque_id if candidate is not None else None
        )
        matched_sayad_id = cheque.sayad_id if cheque is not None else (
            candidate.sayad_id if candidate is not None else None
        )
        outcome = {
            ClearingConfidence.EXACT: ReconcileOutcome.MATCHED,
            ClearingConfidence.PROBABLE: ReconcileOutcome.SUGGESTED,
            ClearingConfidence.AMBIGUOUS: ReconcileOutcome.AMBIGUOUS,
            ClearingConfidence.UNMATCHED: ReconcileOutcome.UNMATCHED,
        }[match.confidence]
        confidence = {
            ClearingConfidence.EXACT: 100,
            ClearingConfidence.PROBABLE: 70,
            ClearingConfidence.AMBIGUOUS: 70,
            ClearingConfidence.UNMATCHED: 0,
        }[match.confidence]
        return BankStatementMatch(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            company_id=company_id,
            statement_id=statement_id,
            statement_line_id=line.line_id,
            line_date=line.date,
            line_amount_irr=line.amount_irr,
            line_description=line.description,
            target_type=MatchTargetType.MANUAL,
            target_id=None,
            target_ref=f"cheque:{matched_cheque_id}" if matched_cheque_id else "",
            target_label=f"چک {matched_sayad_id}" if matched_sayad_id else "",
            priority=1 if match.confidence == ClearingConfidence.EXACT else 2,
            confidence=confidence,
            status=status,
            outcome=outcome,
            destination_account_id=line.destination_account_id,
            destination_shaba=journal_shaba,
            review_item_id=review_item_id,
            matched_at=(
                datetime.datetime.now(datetime.UTC)
                if status == MatchStatus.AUTO_APPLIED
                else None
            ),
            matched_by=actor if status == MatchStatus.AUTO_APPLIED else None,
        )

    def _supersede_prior(
        self, tenant_id: str, prior: BankStatementMatch, actor: str
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
                note="در اجرای تازهٔ تطبیق چک، پیشنهاد تازه‌ای جایگزین شد",
            )
        except ReviewInvalidStateError:
            return

    def _open_stale_refs(self, tenant_id: str) -> set[str]:
        """Source refs of still-open stale-in-clearing items (flagging idempotency)."""
        if self._queue is None:
            return set()
        try:
            items = self._queue.list_items(
                tenant_id,
                ReviewItemFilters(item_type=ReviewItemType.CHEQUE_EXCEPTION),
            )
        except Exception:  # noqa: BLE001 - a queue read failure degrades to filing (dupes possible)
            logger.exception("Stale-in-clearing flagging could not list open review items")
            return set()
        return {
            item.source_event_ref
            for item in items
            if item.subtype == "stale_in_clearing"
            and not item.is_terminal
            and item.source_event_ref is not None
        }
