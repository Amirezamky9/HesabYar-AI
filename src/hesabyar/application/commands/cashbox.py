"""Cash-box write use cases (US-516, Phase 5 wave 2, stream K2).

Cash-box registration, counting sessions (the Iranian three-step
method: supervised count → signed minutes → shortage/surplus
journal), variance-journal posting after manager approval, and
recounts. Like every command service, this layer talks to
persistence ONLY through the domain ports — never infrastructure
(spec §6.3).

The hard rules, enforced here and nowhere else:

- The supervisor is a DIFFERENT user than the cashier (internal
  control; ``CASHBOX_SUPERVISOR_REQUIRED`` defaults to True).
- The book balance is snapshotted read-only from the engine at
  session start — it is never entered by hand.
- Signed minutes (cashier + supervisor + manager) are required
  exactly when the variance is non-zero; with a zero variance,
  supervisor attendance is enough.
- The shortage/surplus journal is a DRAFT until a manager approves
  it in the review queue (``cashbox_variance`` /
  ``approve_variance_entry``) — a cash shortage is NEVER auto-posted.
  Posting is a separate, explicit step that re-checks the approval
  in the review item's audit trail before it touches the engine.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import Sequence

from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.errors import (
    CashBoxInvalidError,
    CashBoxJournalConflictError,
    CashBoxNotFoundError,
    CashBoxSettingsInvalidError,
    CountDenominationsMismatchError,
    CountMinutesRequiredError,
    CountRecountNotRequestedError,
    CountReviewNotApprovedError,
    CountSessionAlreadyOpenError,
    CountSessionInvalidStateError,
    CountSessionNotFoundError,
    CountSupervisorMustDifferError,
    CountWrongActorError,
    ReviewInvalidStateError,
    translate_port_errors,
)
from hesabyar.application.queries.companies import CompanyQueryService
from hesabyar.application.queries.review_queue import ReviewQueueQueryService
from hesabyar.domain.accounting.commands import CreateJournalDraft, PostJournal
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import AccountRef, CompanyRef, JournalDraftRef
from hesabyar.domain.cashbox.models import (
    COUNT_SCHEDULES,
    DEFAULT_COUNT_REMINDER_DAYS,
    DEFAULT_SHORTAGE_SETTLEMENT_HOURS,
    CashBox,
    CashBoxKind,
    CashBoxSettings,
    CountSession,
    CountSessionStatus,
    DenominationCount,
    VariancePolicy,
    variance_account_code,
    variance_journal_lines,
)
from hesabyar.domain.cashbox.ports import CashBoxPort
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import (
    AccountNotFoundError,
    JournalDraftNotFoundError,
)
from hesabyar.domain.review_queue.models import (
    ReviewItemStatus,
    ReviewItemType,
    ReviewResolution,
    ReviewSeverity,
)

#: The review-queue action a manager performs to approve a variance
#: journal (phased design §7.2 — the only resolve action on
#: ``cashbox_variance`` items, and it requires a manager).
APPROVE_VARIANCE_ACTION = "approve_variance_entry"

#: The review-queue action a manager performs to ask for a recount.
RECOUNT_ACTION = "recount"

#: Signature roles on the counting session's minutes.
SIGN_ROLES: tuple[str, ...] = ("cashier", "supervisor", "manager")


def _format_irr(amount_irr: int) -> str:
    """Format an IRR amount for a review-item reason (Latin digits, grouped)."""
    return f"{amount_irr:,}"


class CashBoxCommandService:
    """Write use cases for cash boxes and counting sessions."""

    def __init__(
        self,
        cashbox: CashBoxPort,
        engine: AccountingEnginePort,
        review_commands: ReviewQueueCommandService,
        review_queries: ReviewQueueQueryService,
        *,
        now: datetime.datetime | None = None,
    ) -> None:
        self._cashbox = cashbox
        self._engine = engine
        self._review_commands = review_commands
        self._review_queries = review_queries
        self._companies = CompanyQueryService(engine)
        self._now = now

    def _clock(self) -> datetime.datetime:
        if self._now is not None:
            return self._now
        return datetime.datetime.now(datetime.UTC)

    def _today(self) -> FiscalDate:
        return FiscalDate(self._clock().date())

    def _settings(self, tenant_id: str) -> CashBoxSettings:
        stored = self._cashbox.get_settings(tenant_id)
        if stored is not None:
            return stored
        return CashBoxSettings(tenant_id=tenant_id)

    def _require_box(self, box_id: str, tenant_id: str) -> CashBox:
        box = self._cashbox.get_cash_box(box_id, tenant_id)
        if box is None:
            raise CashBoxNotFoundError(
                f"Cash box {box_id!r} does not exist.",
                details={"cash_box_id": box_id},
            )
        return box

    def _require_session(self, session_id: str, tenant_id: str) -> CountSession:
        session = self._cashbox.get_session(session_id, tenant_id)
        if session is None:
            raise CountSessionNotFoundError(
                f"Counting session {session_id!r} does not exist.",
                details={"session_id": session_id},
            )
        return session

    @staticmethod
    def _validate_ceiling(kind: CashBoxKind, ceiling_irr: int | None) -> None:
        """A petty-cash box needs a positive ceiling; other boxes take none."""
        if kind is CashBoxKind.PETTY_CASH:
            if ceiling_irr is None or ceiling_irr <= 0:
                raise CashBoxInvalidError(
                    "A petty-cash box needs a positive ceiling (ceiling_irr).",
                    details={"kind": kind.value, "ceiling_irr": ceiling_irr},
                )
        elif ceiling_irr is not None:
            raise CashBoxInvalidError(
                "Only a petty-cash box carries a ceiling.",
                details={"kind": kind.value, "ceiling_irr": ceiling_irr},
            )

    # -- cash-box registry -------------------------------------------------

    def register_cash_box(
        self,
        *,
        tenant_id: str,
        company: CompanyRef,
        company_row_id: str,
        name_fa: str,
        kind: CashBoxKind,
        statement_journal_id: int,
        ceiling_irr: int | None = None,
    ) -> CashBox:
        """Register a cash box on one of the company's Statement Journals.

        The box's cash account is the journal's account (a Statement
        Journal carries exactly one account), resolved from the live
        engine read — never typed in by hand. One box per journal: a
        journal that already has a box is a conflict. A missing
        company raises ``CompanyNotFoundError``; an unknown journal
        (or one of another company) is a 422 validation failure.
        """
        cleaned_name = name_fa.strip()
        if not cleaned_name:
            raise CashBoxInvalidError(
                "A cash box needs a name.",
                details={"name_fa": name_fa},
            )
        self._validate_ceiling(kind, ceiling_irr)
        self._companies.get_company(company)
        existing = self._cashbox.get_cash_box_by_journal(
            tenant_id, company_row_id, statement_journal_id
        )
        if existing is not None:
            raise CashBoxJournalConflictError(
                f"Statement journal {statement_journal_id} already has cash box "
                f"{existing.id!r} ({existing.name_fa}).",
                details={
                    "statement_journal_id": statement_journal_id,
                    "cash_box_id": existing.id,
                },
            )
        with translate_port_errors():
            journals = self._engine.list_statement_journals(company)
        journal = next(
            (candidate for candidate in journals if candidate.ref.engine_id == statement_journal_id),
            None,
        )
        if journal is None:
            raise CashBoxInvalidError(
                f"Statement journal {statement_journal_id} does not exist for this company.",
                details={"statement_journal_id": statement_journal_id},
            )
        if not journal.active:
            raise CashBoxInvalidError(
                f"Statement journal {statement_journal_id} ({journal.name}) is inactive.",
                details={"statement_journal_id": statement_journal_id},
            )
        now = self._clock()
        box = CashBox(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            company_id=company_row_id,
            name_fa=cleaned_name,
            kind=kind,
            statement_journal_id=statement_journal_id,
            cash_account_id=journal.account.engine_id,
            cash_account_code=journal.account_code,
            engine_company_id=company.engine_id,
            ceiling_irr=ceiling_irr,
            active=True,
            created_at=now,
            updated_at=now,
        )
        return self._cashbox.save_cash_box(box)

    def update_cash_box(
        self,
        *,
        tenant_id: str,
        box_id: str,
        name_fa: str,
        kind: CashBoxKind,
        ceiling_irr: int | None = None,
        active: bool = True,
    ) -> CashBox:
        """Update a cash box's name, kind, ceiling and active flag.

        The Statement Journal and cash account never change — a box
        that moves to another journal is a new registration, not an
        update (the daybook is the box's identity).
        """
        box = self._require_box(box_id, tenant_id)
        cleaned_name = name_fa.strip()
        if not cleaned_name:
            raise CashBoxInvalidError(
                "A cash box needs a name.",
                details={"name_fa": name_fa},
            )
        self._validate_ceiling(kind, ceiling_irr)
        updated = dataclasses.replace(
            box,
            name_fa=cleaned_name,
            kind=kind,
            ceiling_irr=ceiling_irr,
            active=active,
            updated_at=self._clock(),
        )
        return self._cashbox.update_cash_box(updated)

    # -- settings ------------------------------------------------------------

    def update_settings(
        self,
        *,
        tenant_id: str,
        variance_policy: VariancePolicy,
        material_variance_threshold_irr: int | None = None,
        count_reminder_days: int = DEFAULT_COUNT_REMINDER_DAYS,
        count_schedule: str = "daily",
        supervisor_required: bool = True,
        shortage_settlement_hours: int = DEFAULT_SHORTAGE_SETTLEMENT_HOURS,
    ) -> CashBoxSettings:
        """Set the installation's cash-box policy settings (admin).

        ``material_variance_threshold_irr`` is None (no materiality
        split) or a non-negative amount; ``count_reminder_days`` is
        at least 1; ``count_schedule`` is one of the configured
        schedules; ``shortage_settlement_hours`` is at least 1.
        """
        problems: list[str] = []
        if material_variance_threshold_irr is not None and material_variance_threshold_irr < 0:
            problems.append("material_variance_threshold_irr must be >= 0")
        if count_reminder_days < 1:
            problems.append("count_reminder_days must be >= 1")
        if count_schedule not in COUNT_SCHEDULES:
            problems.append(f"count_schedule must be one of {', '.join(COUNT_SCHEDULES)}")
        if shortage_settlement_hours < 1:
            problems.append("shortage_settlement_hours must be >= 1")
        if problems:
            raise CashBoxSettingsInvalidError(
                "Invalid cash-box settings: " + "; ".join(problems),
                details={
                    "material_variance_threshold_irr": material_variance_threshold_irr,
                    "count_reminder_days": count_reminder_days,
                    "count_schedule": count_schedule,
                    "shortage_settlement_hours": shortage_settlement_hours,
                },
            )
        settings = CashBoxSettings(
            tenant_id=tenant_id,
            variance_policy=variance_policy,
            material_variance_threshold_irr=material_variance_threshold_irr,
            count_reminder_days=count_reminder_days,
            count_schedule=count_schedule,
            supervisor_required=supervisor_required,
            shortage_settlement_hours=shortage_settlement_hours,
            updated_at=self._clock(),
        )
        return self._cashbox.upsert_settings(settings)

    # -- counting sessions ---------------------------------------------------

    def start_session(
        self,
        *,
        tenant_id: str,
        company: CompanyRef,
        company_row_id: str,
        cash_box_id: str,
        cashier: str,
        supervisor: str,
    ) -> CountSession:
        """Start a counting session on a cash box.

        The supervisor must be a different user than the cashier
        (internal control — enforced unless the installation
        deliberately disabled it). The book balance is snapshotted
        read-only from the engine's posted moves at start. One open
        session per box: starting while one is open is a conflict.
        """
        box = self._require_box(cash_box_id, tenant_id)
        if box.company_id != company_row_id:
            raise CashBoxNotFoundError(
                f"Cash box {cash_box_id!r} does not belong to this company.",
                details={"cash_box_id": cash_box_id, "company_id": company_row_id},
            )
        if not box.active:
            raise CountSessionInvalidStateError(
                f"Cash box {box.name_fa!r} is inactive and cannot be counted.",
                details={"cash_box_id": cash_box_id},
            )
        cleaned_cashier = cashier.strip()
        cleaned_supervisor = supervisor.strip()
        if not cleaned_cashier or not cleaned_supervisor:
            raise CashBoxInvalidError(
                "A counting session needs both a cashier and a supervisor.",
                details={"cashier": cashier, "supervisor": supervisor},
            )
        settings = self._settings(tenant_id)
        if settings.supervisor_required and cleaned_supervisor == cleaned_cashier:
            raise CountSupervisorMustDifferError(
                "The supervisor must be a different user than the cashier.",
                details={"cashier": cleaned_cashier, "supervisor": cleaned_supervisor},
            )
        open_session = self._cashbox.get_open_session_for_box(cash_box_id, tenant_id)
        if open_session is not None:
            raise CountSessionAlreadyOpenError(
                f"Cash box {box.name_fa!r} already has an open counting session "
                f"({open_session.id}, {open_session.status.value}).",
                details={"cash_box_id": cash_box_id, "session_id": open_session.id},
            )
        self._companies.get_company(company)
        with translate_port_errors():
            balance = self._engine.get_account_balance(
                company, AccountRef(box.cash_account_id), self._today()
            )
        now = self._clock()
        session = CountSession(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            company_id=company_row_id,
            cash_box_id=cash_box_id,
            cashier=cleaned_cashier,
            supervisor=cleaned_supervisor,
            status=CountSessionStatus.IN_PROGRESS,
            book_balance_irr=balance.balance.amount,
            created_at=now,
            engine_company_id=company.engine_id,
            updated_at=now,
        )
        return self._cashbox.save_session(session)

    def enter_count(
        self,
        *,
        tenant_id: str,
        session_id: str,
        counted_amount_irr: int,
        denominations: Sequence[DenominationCount] = (),
        actor: str,
    ) -> CountSession:
        """Enter (or correct) the counted amount of a session.

        Only the session's cashier enters the count. The count may be
        corrected while the session is still in progress or awaiting
        the supervisor — but never after the supervisor has attended
        or anyone has signed the minutes: at that point the count is
        locked and only a manager-requested recount reopens it. When
        a denomination breakdown is given, it must sum exactly to the
        counted amount.
        """
        session = self._require_session(session_id, tenant_id)
        if session.status not in (
            CountSessionStatus.IN_PROGRESS,
            CountSessionStatus.AWAITING_SUPERVISOR,
        ):
            raise CountSessionInvalidStateError(
                f"Counting session {session_id!r} is {session.status.value}; "
                "its count can no longer be entered.",
                details={"session_id": session_id, "status": session.status.value},
            )
        if actor.strip() != session.cashier:
            raise CountWrongActorError(
                f"Only the session's cashier ({session.cashier}) may enter the count.",
                details={"session_id": session_id, "actor": actor, "cashier": session.cashier},
            )
        if session.supervisor_attended or session.minutes_signed or (
            session.minutes_cashier_signed_at is not None
            or session.minutes_supervisor_signed_at is not None
            or session.minutes_manager_signed_at is not None
        ):
            raise CountSessionInvalidStateError(
                f"Counting session {session_id!r} is already signed; "
                "its count is locked — ask the manager for a recount.",
                details={"session_id": session_id, "status": session.status.value},
            )
        if counted_amount_irr < 0:
            raise CashBoxInvalidError(
                "The counted amount cannot be negative.",
                details={"counted_amount_irr": counted_amount_irr},
            )
        breakdown = tuple(denominations)
        for line in breakdown:
            if line.denomination_irr <= 0 or line.count <= 0:
                raise CashBoxInvalidError(
                    "Denomination values and counts must be positive.",
                    details={
                        "denomination_irr": line.denomination_irr,
                        "count": line.count,
                    },
                )
        if breakdown and sum(line.total_irr for line in breakdown) != counted_amount_irr:
            raise CountDenominationsMismatchError(
                "The denomination breakdown does not sum to the counted amount.",
                details={
                    "counted_amount_irr": counted_amount_irr,
                    "denominations_total_irr": sum(line.total_irr for line in breakdown),
                },
            )
        updated = dataclasses.replace(
            session,
            counted_amount_irr=counted_amount_irr,
            denominations=breakdown,
            status=CountSessionStatus.AWAITING_SUPERVISOR,
            updated_at=self._clock(),
        )
        return self._cashbox.update_session(updated)

    def sign_session(
        self,
        *,
        tenant_id: str,
        session_id: str,
        role: str,
        actor: str,
        minutes_text: str | None = None,
    ) -> CountSession:
        """Record one signature on a counting session.

        Roles: ``cashier`` / ``supervisor`` / ``manager``. The
        supervisor's signature records attendance in every session
        and signs the minutes when the variance is non-zero; the
        cashier's and manager's signatures sign the minutes (only
        when the variance is non-zero — a balanced count has no
        minutes to sign). The cashier's signature carries the minutes
        text. Each role signs at most once; the manager must differ
        from both the cashier and the supervisor.

        Completion: a balanced session archives as soon as the
        supervisor attends; a variance session moves to
        ``awaiting_manager`` — with its DRAFT variance journal
        created and its ``cashbox_variance`` review item filed — as
        soon as all three minutes signatures are present.
        """
        if role not in SIGN_ROLES:
            raise CashBoxInvalidError(
                f"Signature role must be one of {', '.join(SIGN_ROLES)}.",
                details={"role": role},
            )
        session = self._require_session(session_id, tenant_id)
        if session.status is not CountSessionStatus.AWAITING_SUPERVISOR:
            raise CountSessionInvalidStateError(
                f"Counting session {session_id!r} is {session.status.value}; "
                "it does not accept signatures now.",
                details={"session_id": session_id, "status": session.status.value},
            )
        cleaned_actor = actor.strip()
        if not cleaned_actor:
            raise CashBoxInvalidError(
                "A signature needs an actor.",
                details={"role": role},
            )
        now = self._clock()
        if session.minutes_signed:
            # Fully signed but still awaiting the supervisor: the
            # variance completion failed after the signatures were
            # recorded (e.g. the variance account was missing from
            # the chart). A re-sign by one of the three signers
            # retries the completion instead of raising a
            # duplicate-signature error — otherwise the session
            # could never complete.
            expected_signer = {
                "cashier": session.minutes_cashier_signed_by,
                "supervisor": session.minutes_supervisor_signed_by,
                "manager": session.minutes_manager_signed_by,
            }[role]
            if cleaned_actor != expected_signer:
                raise CountWrongActorError(
                    f"Only the session's {role} ({expected_signer}) may retry this completion.",
                    details={"session_id": session_id, "actor": cleaned_actor, "role": role},
                )
            return self._maybe_complete_session(session)
        if role == "cashier":
            if cleaned_actor != session.cashier:
                raise CountWrongActorError(
                    f"Only the session's cashier ({session.cashier}) may sign as cashier.",
                    details={"session_id": session_id, "actor": cleaned_actor},
                )
            if not session.minutes_required:
                raise CountSessionInvalidStateError(
                    "A balanced count has no minutes to sign.",
                    details={"session_id": session_id},
                )
            if session.minutes_cashier_signed_at is not None:
                raise CountSessionInvalidStateError(
                    "The cashier has already signed these minutes.",
                    details={"session_id": session_id},
                )
            cleaned_text = (minutes_text or "").strip()
            if not cleaned_text:
                raise CountMinutesRequiredError(
                    "The cashier's signature must carry the minutes text.",
                    details={"session_id": session_id},
                )
            updated = dataclasses.replace(
                session,
                minutes_text=cleaned_text,
                minutes_cashier_signed_by=cleaned_actor,
                minutes_cashier_signed_at=now,
                updated_at=now,
            )
        elif role == "supervisor":
            if cleaned_actor != session.supervisor:
                raise CountWrongActorError(
                    f"Only the session's supervisor ({session.supervisor}) may sign as supervisor.",
                    details={"session_id": session_id, "actor": cleaned_actor},
                )
            if session.supervisor_attended:
                raise CountSessionInvalidStateError(
                    "The supervisor has already attended this session.",
                    details={"session_id": session_id},
                )
            updated = dataclasses.replace(
                session,
                witnessed_by=cleaned_actor,
                witnessed_at=now,
                minutes_supervisor_signed_by=(
                    cleaned_actor if session.minutes_required else session.minutes_supervisor_signed_by
                ),
                minutes_supervisor_signed_at=now if session.minutes_required else None,
                updated_at=now,
            )
        else:  # manager
            if cleaned_actor in (session.cashier, session.supervisor):
                raise CountWrongActorError(
                    "The manager signing the minutes must differ from the cashier and the supervisor.",
                    details={"session_id": session_id, "actor": cleaned_actor},
                )
            if not session.minutes_required:
                raise CountSessionInvalidStateError(
                    "A balanced count has no minutes to sign.",
                    details={"session_id": session_id},
                )
            if session.minutes_manager_signed_at is not None:
                raise CountSessionInvalidStateError(
                    "The manager has already signed these minutes.",
                    details={"session_id": session_id},
                )
            updated = dataclasses.replace(
                session,
                minutes_manager_signed_by=cleaned_actor,
                minutes_manager_signed_at=now,
                updated_at=now,
            )
        saved = self._cashbox.update_session(updated)
        return self._maybe_complete_session(saved)

    def _maybe_complete_session(self, session: CountSession) -> CountSession:
        """Archive a balanced session, or file a variance session's journal + review item."""
        if session.status is not CountSessionStatus.AWAITING_SUPERVISOR:
            return session
        if session.is_balanced:
            if not session.supervisor_attended:
                return session
            # Zero variance: supervisor attendance is enough — no
            # minutes, no journal, no review item. Except on a
            # recount: the linked item's figures are stale, so they
            # are refreshed to zero (the manager closes an honest
            # item; the post gate still refuses — the session is
            # archived, not awaiting the manager). If the manager
            # already closed the item while the recount was in
            # flight, the item is history and the session archives
            # without touching it.
            if session.review_item_id is not None:
                try:
                    box = self._require_box(session.cash_box_id, session.tenant_id)
                    self._review_commands.update_item_figures(
                        item_id=session.review_item_id,
                        tenant_id=session.tenant_id,
                        reason=(
                            f"شمارش مجدد صندوق «{box.name_fa}»: مبلغ شمارش‌شده "
                            f"({_format_irr(session.counted_amount_irr or 0)} ریال) با ماندهٔ دفتری "
                            "برابر است؛ کسری یا اضافه‌ای وجود ندارد."
                        ),
                        suggested_action="صورتجلسهٔ شمارش مجدد را بررسی کنید و پرونده را ببندید.",
                        amount_irr=0,
                        severity=ReviewSeverity.INFO,
                        actor=session.cashier,
                        note="شمارش مجدد انجام شد؛ ارقام به‌روز شدند.",
                    )
                except ReviewInvalidStateError:
                    pass
            archived = dataclasses.replace(
                session,
                status=CountSessionStatus.ARCHIVED,
                updated_at=self._clock(),
            )
            return self._cashbox.update_session(archived)
        if not session.minutes_signed:
            return session
        # Variance session, minutes complete: create the DRAFT
        # variance journal and file (or refresh) the review item.
        return self._complete_variance_session(session)

    def _complete_variance_session(self, session: CountSession) -> CountSession:
        variance = session.variance_irr
        assert variance is not None and variance != 0  # minutes_signed implies it
        box = self._require_box(session.cash_box_id, session.tenant_id)
        settings = self._settings(session.tenant_id)
        company = CompanyRef(engine_id=session.engine_company_id)
        variance_code = variance_account_code(settings.variance_policy, variance)
        # Targeted read, not the whole chart: the chart fetch costs
        # ~28s against a remote engine — nearly the BFF's whole 30s
        # budget for the manager-sign request that triggers this.
        with translate_port_errors():
            variance_account = self._engine.get_account_by_code(company, variance_code)
        if variance_account is None or not variance_account.is_postable:
            raise AccountNotFoundError(
                f"Variance account {variance_code} is not in the company's chart "
                "(the HesabYar chart extension may not be installed).",
                details={"account_code": variance_code},
            )
        lines = variance_journal_lines(
            variance_irr=variance,
            cash_account=AccountRef(box.cash_account_id),
            variance_account=variance_account.ref,
        )
        direction = "کسری" if variance < 0 else "اضافه"
        description = (
            f"سند {direction} صندوق «{box.name_fa}» — جلسهٔ شمارش {session.id[:8]} "
            f"(ماندهٔ دفتری {_format_irr(session.book_balance_irr)} ریال، "
            f"مبلغ شمارش‌شده {_format_irr(session.counted_amount_irr or 0)} ریال)"
        )
        with translate_port_errors():
            journal = self._engine.get_general_journal(company)
            draft = self._engine.create_journal_draft(
                CreateJournalDraft(
                    company=company,
                    date=self._today(),
                    lines=lines,
                    description=description,
                    journal=journal,
                )
            )
        reason, suggested_action, severity = self._review_item_text(session, box, variance, settings)
        review_item_id = session.review_item_id
        if review_item_id is not None:
            # Recount: the same item carries the new figures — unless
            # the manager already closed it while the recount was in
            # flight, in which case the recount's result files a NEW
            # item (a terminal item's figures are history).
            try:
                self._review_commands.update_item_figures(
                    item_id=review_item_id,
                    tenant_id=session.tenant_id,
                    reason=reason,
                    suggested_action=suggested_action,
                    amount_irr=abs(variance),
                    severity=severity,
                    actor=session.cashier,
                    note="شمارش مجدد انجام شد؛ ارقام به‌روز شدند.",
                )
            except ReviewInvalidStateError:
                review_item_id = None
        if review_item_id is None:
            item = self._review_commands.file_item(
                tenant_id=session.tenant_id,
                item_type=ReviewItemType.CASHBOX_VARIANCE,
                severity=severity,
                reason=reason,
                suggested_action=suggested_action,
                company_id=session.company_id,
                source_event_ref=f"cashbox-session:{session.id}",
                amount_irr=abs(variance),
                actor=session.cashier,
            )
            review_item_id = item.id
        updated = dataclasses.replace(
            session,
            status=CountSessionStatus.AWAITING_MANAGER,
            draft_journal_id=draft.engine_id,
            review_item_id=review_item_id,
            updated_at=self._clock(),
        )
        return self._cashbox.update_session(updated)

    def _review_item_text(
        self,
        session: CountSession,
        box: CashBox,
        variance: int,
        settings: CashBoxSettings,
    ) -> tuple[str, str, ReviewSeverity]:
        """Reason, suggested action and severity for the variance review item."""
        direction = "کسری" if variance < 0 else "اضافه"
        reason = (
            f"{direction} صندوق «{box.name_fa}»: ماندهٔ دفتری "
            f"{_format_irr(session.book_balance_irr)} ریال، مبلغ شمارش‌شده "
            f"{_format_irr(session.counted_amount_irr or 0)} ریال، "
            f"{direction} {_format_irr(abs(variance))} ریال. "
            f"صورتجلسه با امضای صندوقدار ({session.cashier})، ناظر "
            f"({session.supervisor}) و مدیریت تهیه شده است."
        )
        suggested_action = (
            "صورتجلسهٔ امضاشده را بررسی کنید؛ در صورت تأیید، سند "
            f"{direction} را تأیید کنید تا در دفتر ثبت شود. اگر به نتیجهٔ "
            "شمارش مشکوک هستید، شمارش مجدد بخواهید."
        )
        threshold = settings.material_variance_threshold_irr
        severity = (
            ReviewSeverity.CRITICAL
            if threshold is not None and abs(variance) >= threshold
            else ReviewSeverity.WARNING
        )
        return reason, suggested_action, severity

    # -- posting + recount -----------------------------------------------------

    def post_variance_journal(
        self,
        *,
        tenant_id: str,
        session_id: str,
        actor: str,
    ) -> CountSession:
        """Post a variance session's DRAFT journal — only after manager approval.

        The linked ``cashbox_variance`` review item must be resolved
        with the ``approve_variance_entry`` action (checked in the
        item's audit trail, not just its status). Without that
        approval the post is refused: a cash shortage is NEVER
        auto-posted. After posting, the session is archived.
        """
        session = self._require_session(session_id, tenant_id)
        if session.status is not CountSessionStatus.AWAITING_MANAGER:
            raise CountSessionInvalidStateError(
                f"Counting session {session_id!r} is {session.status.value}; "
                "it has no variance journal awaiting approval.",
                details={"session_id": session_id, "status": session.status.value},
            )
        if session.draft_journal_id is None or session.review_item_id is None:
            raise CountSessionInvalidStateError(
                f"Counting session {session_id!r} has no draft journal or review item.",
                details={"session_id": session_id},
            )
        view = self._review_queries.get_item(session.review_item_id, tenant_id)
        approved = (
            view.item.status is ReviewItemStatus.RESOLVED
            and view.item.resolution is ReviewResolution.ADJUSTED
            and any(entry.action == APPROVE_VARIANCE_ACTION for entry in view.item.audit)
        )
        if not approved:
            raise CountReviewNotApprovedError(
                f"The variance journal of session {session_id!r} has not been "
                "approved by a manager in the review queue yet.",
                details={
                    "session_id": session_id,
                    "review_item_id": session.review_item_id,
                    "review_status": view.item.status.value,
                },
            )
        with translate_port_errors():
            posted = self._engine.post_journal(PostJournal(JournalDraftRef(session.draft_journal_id)))
        updated = dataclasses.replace(
            session,
            status=CountSessionStatus.ARCHIVED,
            posted_journal_id=posted.engine_id,
            updated_at=self._clock(),
        )
        return self._cashbox.update_session(updated)

    def recount(
        self,
        *,
        tenant_id: str,
        session_id: str,
        actor: str,
    ) -> CountSession:
        """Reopen a variance session for a recount, as the manager requested.

        The linked review item must be ``in_review`` with a ``recount``
        action in its audit trail — a recount without a manager's
        request is refused. The superseded DRAFT variance journal is
        deleted from the engine (a draft that is already gone is
        tolerated — the desired end state is "no draft"); the count,
        denominations, minutes and signatures are cleared, and the
        session returns to ``in_progress``. The review item stays
        linked: when the recount completes, its figures are refreshed
        on the same item.
        """
        session = self._require_session(session_id, tenant_id)
        if session.status is not CountSessionStatus.AWAITING_MANAGER:
            raise CountSessionInvalidStateError(
                f"Counting session {session_id!r} is {session.status.value}; "
                "only a session awaiting the manager can be recounted.",
                details={"session_id": session_id, "status": session.status.value},
            )
        if session.review_item_id is None:
            raise CountRecountNotRequestedError(
                f"Counting session {session_id!r} has no review item to request a recount from.",
                details={"session_id": session_id},
            )
        view = self._review_queries.get_item(session.review_item_id, tenant_id)
        requested = view.item.status is ReviewItemStatus.IN_REVIEW and any(
            entry.action == RECOUNT_ACTION for entry in view.item.audit
        )
        if not requested:
            raise CountRecountNotRequestedError(
                f"The manager has not requested a recount for session {session_id!r}.",
                details={
                    "session_id": session_id,
                    "review_item_id": session.review_item_id,
                    "review_status": view.item.status.value,
                },
            )
        if session.draft_journal_id is not None:
            try:
                with translate_port_errors():
                    self._engine.delete_journal_draft(JournalDraftRef(session.draft_journal_id))
            except JournalDraftNotFoundError:
                # Already deleted out-of-band: the desired end state
                # is "no draft", so the recount proceeds.
                pass
        updated = dataclasses.replace(
            session,
            status=CountSessionStatus.IN_PROGRESS,
            counted_amount_irr=None,
            denominations=(),
            minutes_text=None,
            witnessed_by=None,
            witnessed_at=None,
            minutes_cashier_signed_by=None,
            minutes_cashier_signed_at=None,
            minutes_supervisor_signed_by=None,
            minutes_supervisor_signed_at=None,
            minutes_manager_signed_by=None,
            minutes_manager_signed_at=None,
            draft_journal_id=None,
            updated_at=self._clock(),
        )
        return self._cashbox.update_session(updated)
