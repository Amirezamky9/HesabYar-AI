"""Journal draft use cases over the accounting engine port (US-202).

REST, MCP and workers all call this service instead of the engine port
directly, so journal workflows have one implementation and one error
contract (design-eng spec §6.2/§6.3, §8.2). The service owns the
workflow — draft, validate, post, reverse — but no engine behaviour:
every state change goes through the port, and Tryton remains the only
posting authority.
"""

from __future__ import annotations

from collections.abc import Sequence

from hesabyar.application.errors import translate_port_errors
from hesabyar.application.queries.companies import CompanyQueryService
from hesabyar.domain.accounting.commands import (
    CreateJournalDraft,
    JournalDraftLine,
    PostJournal,
    ReverseJournal,
    UpdateJournalDraft,
)
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import (
    CompanyRef,
    JournalDraftRef,
    JournalRef,
    PostedJournalRef,
)
from hesabyar.domain.accounting.value_objects import (
    JournalDraftView,
    PostedJournalView,
    ValidationSummary,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import (
    HesabYarError,
    JournalAlreadyPostedError,
    JournalDraftNotFoundError,
    PostedJournalNotFoundError,
)


class JournalDraftService:
    """Journal draft workflows over an engine-neutral port."""

    def __init__(self, engine: AccountingEnginePort) -> None:
        self._engine = engine
        self._companies = CompanyQueryService(engine)

    # -- create / read ----------------------------------------------------

    def create_draft(
        self,
        company: CompanyRef,
        date: FiscalDate,
        lines: Sequence[JournalDraftLine],
        description: str = "",
        journal: JournalRef | None = None,
    ) -> JournalDraftRef:
        """Create a draft journal (an unposted move) in the engine.

        The company is verified first so a mapping row pointing at a
        company the engine does not know fails as ``COMPANY_NOT_FOUND``
        instead of surfacing as a confusing account error. The
        ``CreateJournalDraft`` command enforces the §8.2 invariants at
        construction (≥ 2 lines, one-sided lines, balanced, total > 0);
        the adapter additionally verifies accounts and the open period.
        """
        self._companies.get_company(company)
        command = CreateJournalDraft(
            company=company,
            date=date,
            lines=tuple(lines),
            description=description,
            journal=journal,
        )
        with translate_port_errors():
            return self._engine.create_journal_draft(command)

    def get_draft(self, draft: JournalDraftRef) -> JournalDraftView:
        """Return one draft journal, or raise ``JournalDraftNotFoundError``.

        The view carries the move's state, so a draft that has since
        been posted reads back with ``state == "posted"`` rather than
        vanishing — callers decide what a posted draft means for them.
        """
        with translate_port_errors():
            view = self._engine.get_journal_draft(draft)
        if view is None:
            raise JournalDraftNotFoundError(
                f"Journal draft {draft.engine_id} does not exist in the engine.",
                details={"draft_id": draft.engine_id},
            )
        return view

    def list_drafts(self, company: CompanyRef) -> Sequence[JournalDraftView]:
        """List a company's draft journals (unposted moves), newest first.

        The company is verified first so a mapping row pointing at a
        company the engine does not know fails as ``COMPANY_NOT_FOUND``
        instead of surfacing as an empty list.
        """
        self._companies.get_company(company)
        with translate_port_errors():
            return self._engine.list_journal_drafts(company)

    def update_draft(
        self,
        draft: JournalDraftRef,
        *,
        date: FiscalDate | None = None,
        description: str | None = None,
        lines: tuple[JournalDraftLine, ...] | None = None,
    ) -> JournalDraftView:
        """Update a draft journal's date, description or lines.

        Only the given fields change; ``lines`` replaces the draft's
        lines wholesale. The ``UpdateJournalDraft`` command enforces
        the §8.2 invariants at construction when lines are given
        (>= 2 lines, one-sided, balanced, total > 0) and rejects an
        empty update; the adapter additionally verifies accounts and
        the open period, and refuses posted journals
        (``LEDGER_POSTED_IMMUTABLE``). Returns the updated draft as
        read back from the engine.
        """
        command = UpdateJournalDraft(
            draft=draft, date=date, description=description, lines=lines
        )
        with translate_port_errors():
            return self._engine.update_journal_draft(command)

    # -- validate / post ---------------------------------------------------

    def validate_draft(self, draft: JournalDraftRef) -> ValidationSummary:
        """Validate a draft against the §8.2 invariants (never mutates)."""
        with translate_port_errors():
            return self._engine.validate_journal_draft(draft)

    def post_draft(self, draft: JournalDraftRef) -> PostedJournalRef:
        """Post a draft journal, atomically, in the engine.

        The adapter re-validates the draft first; unbalanced,
        unknown-account and closed-period drafts fail with stable
        ledger errors before anything is posted. Posting an
        already-posted journal raises ``JournalAlreadyPostedError``
        (``LEDGER_POSTED_IMMUTABLE``) — it is never a no-op, so a
        duplicate post attempt is always visible to the caller.
        """
        with translate_port_errors():
            return self._engine.post_journal(PostJournal(draft=draft))

    # -- posted journals / reversal ----------------------------------------

    def get_posted_journal(self, posted: PostedJournalRef) -> PostedJournalView:
        """Return one posted journal, or raise ``PostedJournalNotFoundError``.

        A move that exists but is not posted raises
        ``InvalidJournalError`` (the port's posted-only read contract).
        """
        with translate_port_errors():
            view = self._engine.get_journal(posted)
        if view is None:
            raise PostedJournalNotFoundError(
                f"Posted journal {posted.engine_id} does not exist in the engine.",
                details={"posted_id": posted.engine_id},
            )
        return view

    def reverse_journal(
        self, posted: PostedJournalRef, description: str = ""
    ) -> PostedJournalRef:
        """Reverse a posted journal with a new posted reversal journal.

        Reversal is NOT atomic across its two engine calls (create the
        reversal draft, then post it). When the post fails, the adapter
        attaches ``reversal_draft_id`` to the error's details and the
        draft survives — so this method retries the post of that draft
        once instead of assuming all-or-nothing:

        - retry posts → the reversal is returned (the first post's
          failure was transient);
        - retry raises ``JournalAlreadyPostedError`` → the first post
          actually landed and only its response was lost, so the
          reversal is returned as well;
        - retry fails otherwise → the ORIGINAL error is re-raised with
          ``reversal_draft_id`` intact, so the caller can recover the
          orphaned draft (post it, or leave it for a human) instead of
          blindly creating a second reversal.

        Reversal does not reconcile reconcile-account lines (no REST
        path exists); that limit lives in the adapter, documented in
        ``docs/adapter.md``.
        """
        try:
            with translate_port_errors():
                return self._engine.reverse_journal(
                    ReverseJournal(posted=posted, description=description)
                )
        except HesabYarError as exc:
            reversal_draft_id = exc.details.get("reversal_draft_id")
            if not isinstance(reversal_draft_id, int):
                raise
            draft = JournalDraftRef(reversal_draft_id)
            try:
                with translate_port_errors():
                    return self._engine.post_journal(PostJournal(draft=draft))
            except JournalAlreadyPostedError:
                # The first post landed; only its confirmation was lost.
                return PostedJournalRef(engine_id=reversal_draft_id)
            except HesabYarError:
                # Keep the original failure (with reversal_draft_id) so
                # the caller sees the first cause, not the retry's.
                raise exc from None
