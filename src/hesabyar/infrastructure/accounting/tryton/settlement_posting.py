"""Settlement posting implementations (US-522 engine + US-523 seam).

The tie-out service posts the bank leg of a settled batch (Dr bank /
Cr clearing) through :class:`SettlementPostingPort`, only after the
mandatory tie-out holds and the policy gate allows auto-post.

:class:`EngineSettlementPosting` is the wired implementation: it
fans the batch out to one :class:`SettlementPostingService`
``post_bank_receipt`` call per tranche (the US-522 tranche-level
engine posting), resolving each tranche's wiring from the mapping
center. The batch-level command carries no engine wiring of its
own, so the fan-out is the adapter between the batch-level port
and the tranche-level service.

PARTIAL POSTING (read before changing this): each tranche posts
its own engine move, and there is no cross-tranche transaction —
if tranche 2 fails after tranche 1 posted, tranche 1 STAYS posted
(its move and its ``bank_receipt`` ledger row stand) and the call
raises :class:`SettlementPostingError`, which the tie-out service
catches: the batch stays in exception and a review item is filed
naming the tranches already posted. A retry is safe: tranches
whose ``bank_receipt`` postings already cover their received
amount are SKIPPED via the posting ledger (never re-posted), and
a duplicate-posting error from the service is treated as
already-posted (belt and braces). Partially posted tranches are
NOT skipped — the service posts only the newly received remainder,
and their earlier partial moves count toward the batch total.

:class:`UnconfiguredSettlementPosting` is the fail-closed stub
kept for installations with no engine posting wired: it raises
:class:`SettlementPostingNotConfiguredError` so the batch stays
in exception instead of being silently left unposted. It is no
longer the default wiring.
"""

from __future__ import annotations

from typing import Protocol

from hesabyar.application.commands.settlement_posting import (
    BankReceiptResult,
    ClearingResult,
)
from hesabyar.domain.accounting.refs import CompanyRef
from hesabyar.domain.common.errors import HesabYarError
from hesabyar.domain.settlement.commands import (
    PostBankReceipt,
    PostBatchBankReceipt,
    PostTrancheClearing,
    SettlementPostingWiring,
)
from hesabyar.domain.settlement.errors import (
    SettlementPostingError,
    SettlementPostingExistsError,
    SettlementPostingNotConfiguredError,
    SettlementPostingPreconditionError,
)
from hesabyar.domain.settlement.models import SettlementPostingKind
from hesabyar.domain.settlement.ports import (
    BankReceiptPosting,
    SettlementPostingPort,
    SettlementRepositoryPort,
)

__all__ = ["BankReceiptService", "EngineSettlementPosting", "UnconfiguredSettlementPosting"]


class BankReceiptService(Protocol):
    """The narrow slice of SettlementPostingService this adapter needs.

    A Protocol (not the concrete service) so the adapter depends on
    the two methods it calls, and tests can substitute a fake. The
    real :class:`SettlementPostingService` satisfies it structurally.
    """

    def resolve_wiring_for_tranche(
        self,
        installation_id: str,
        tranche_id: str,
        company: CompanyRef,
    ) -> SettlementPostingWiring:
        """Resolve a tranche's posting wiring via its payment."""
        ...

    def post_bank_receipt(
        self, installation_id: str, command: PostBankReceipt
    ) -> BankReceiptResult:
        """Post a received tranche's bank receipt (Dr bank / Cr clearing)."""
        ...

    def post_tranche_clearing(
        self, installation_id: str, command: PostTrancheClearing
    ) -> ClearingResult:
        """Post a tranche's clearing transfer (Dr clearing / Cr receivable).

        The batch flow clears late: a file-imported tranche reaches the
        tie-out already RECEIVED, and its clearing transfer was never
        posted — the adapter posts it here, before the bank receipt.
        """
        ...


class EngineSettlementPosting(SettlementPostingPort):
    """Real posting port: batch → per-tranche engine postings (US-522).

    For each tranche in the batch command, resolves the wiring, posts
    the tranche's clearing transfer when it has none yet (the batch
    flow clears late — a file-imported tranche reaches the tie-out
    already RECEIVED), then posts the tranche's bank receipt through
    the settlement posting service. Fail-closed: ANY tranche failure
    (unknown tranche, unresolved wiring, tie-out gate inside the
    service, engine error) raises :class:`SettlementPostingError`
    with the tranches already posted listed in the details — the
    caller keeps the batch in exception. Partial posting is by design
    (see the module docstring): earlier tranches stay posted, and a
    retry skips them via the posting ledger (receipts AND clearing
    rows are both checked, so a retry never double-clears).
    """

    def __init__(
        self,
        service: BankReceiptService,
        repository: SettlementRepositoryPort,
    ) -> None:
        self._service = service
        self._repository = repository

    def post_bank_receipt(self, command: PostBatchBankReceipt) -> BankReceiptPosting:
        """Post the batch's bank receipt, one engine move per tranche."""
        if not command.tranche_ids:
            raise SettlementPostingError(
                "Cannot post a batch bank receipt with no tranches: the "
                "tie-out named no confirmed tranches.",
                details={
                    "import_batch_id": command.import_batch_id,
                    "channel_key": command.channel_key,
                },
            )
        if command.company_engine_id is None:
            raise SettlementPostingError(
                "Cannot post a batch bank receipt with no company: the "
                "posting wiring cannot be resolved without a company.",
                details={
                    "import_batch_id": command.import_batch_id,
                    "channel_key": command.channel_key,
                    "tranche_ids": list(command.tranche_ids),
                },
            )
        company = CompanyRef(command.company_engine_id)
        journal_ids: list[int] = []
        total_irr = 0
        posted_tranche_ids: list[str] = []
        skipped_tranche_ids: list[str] = []
        for tranche_id in command.tranche_ids:
            prior_total, prior_moves = self._receipt_state(
                command.installation_id, tranche_id
            )
            if self._is_fully_posted(command.installation_id, tranche_id, prior_total):
                skipped_tranche_ids.append(tranche_id)
                total_irr += prior_total
                journal_ids.extend(prior_moves)
                continue
            try:
                wiring = self._service.resolve_wiring_for_tranche(
                    command.installation_id, tranche_id, company
                )
                if not self._is_cleared(command.installation_id, tranche_id):
                    # Late clearing (batch flow): the tranche was never
                    # cleared — the bank receipt refuses an uncleared
                    # tranche, so post the clearing transfer first, with
                    # the tranche's scheduled payout date as the
                    # clearing date. The ledger check makes the retry
                    # idempotent: a tranche cleared by a failed first
                    # attempt is not cleared twice.
                    tranche = self._repository.get_tranche(
                        command.installation_id, tranche_id
                    )
                    if tranche is None:
                        raise SettlementPostingError(
                            f"Tranche {tranche_id} disappeared between wiring "
                            "resolution and its clearing transfer — refusing "
                            "to post the batch.",
                            details={
                                "import_batch_id": command.import_batch_id,
                                "channel_key": command.channel_key,
                                "tranche_id": tranche_id,
                                "posted_tranche_ids": list(posted_tranche_ids),
                            },
                        )
                    self._service.post_tranche_clearing(
                        command.installation_id,
                        PostTrancheClearing(
                            tranche_id=tranche_id,
                            wiring=wiring,
                            payout_date=tranche.expected_date,
                            actor=None,
                        ),
                    )
                result = self._service.post_bank_receipt(
                    command.installation_id,
                    PostBankReceipt(
                        tranche_id=tranche_id,
                        wiring=wiring,
                        settlement_event_id=command.settlement_event_id,
                        deposit_date=command.deposit_date,
                        actor=None,
                    ),
                )
            except (
                SettlementPostingExistsError,
                SettlementPostingPreconditionError,
            ) as exc:
                if isinstance(
                    exc, SettlementPostingPreconditionError
                ) and "already_posted_irr" not in exc.details:
                    raise self._wrap_failure(
                        command, tranche_id, posted_tranche_ids, exc
                    ) from exc
                # Belt and braces: the service says this tranche was
                # already posted (a concurrent post, or a ledger row
                # the pre-check missed) — the ledger MUST show it
                # now; if it does not, fail closed rather than
                # silently skip a tranche that was never posted.
                total_now, moves_now = self._receipt_state(
                    command.installation_id, tranche_id
                )
                if not self._is_fully_posted(
                    command.installation_id, tranche_id, total_now
                ):
                    raise SettlementPostingError(
                        f"Tranche {tranche_id} was reported as already "
                        "posted, but the posting ledger shows no bank "
                        "receipt covering its received amount — refusing "
                        "to skip it.",
                        details={
                            "import_batch_id": command.import_batch_id,
                            "channel_key": command.channel_key,
                            "tranche_id": tranche_id,
                            "posted_tranche_ids": list(posted_tranche_ids),
                        },
                    ) from exc
                skipped_tranche_ids.append(tranche_id)
                total_irr += total_now
                journal_ids.extend(moves_now)
                continue
            except HesabYarError as exc:
                raise self._wrap_failure(command, tranche_id, posted_tranche_ids, exc) from exc
            except Exception as exc:  # unexpected engine failure: fail closed, never half-silent
                raise self._wrap_failure(command, tranche_id, posted_tranche_ids, exc) from exc
            move_id = result.posting.tryton_move_id
            if move_id is None:
                # Defensive: the service always records the move id;
                # a missing one means the ledger row is unusable, so
                # the batch must not report success for this tranche.
                # (The move itself is already posted in the engine —
                # the retry's ledger pre-check skips this tranche.)
                raise SettlementPostingError(
                    f"Tranche {tranche_id} posted but the posting ledger "
                    "recorded no Tryton move id — the batch cannot be "
                    "reported as posted.",
                    details={
                        "import_batch_id": command.import_batch_id,
                        "channel_key": command.channel_key,
                        "tranche_id": tranche_id,
                        "posted_tranche_ids": list(posted_tranche_ids),
                        "posting_id": result.posting.id,
                    },
                )
            posted_tranche_ids.append(tranche_id)
            # A partially posted tranche keeps its earlier moves in
            # the result: the batch total covers the whole receipt.
            total_irr += prior_total + result.amount_irr
            journal_ids.extend([*prior_moves, move_id])
        if not journal_ids:
            # Every tranche was skipped but the ledger recorded no
            # move id for any of them — inconsistent state, fail closed.
            raise SettlementPostingError(
                "No bank receipt move id is recorded for this batch: every "
                "tranche was already posted, but the posting ledger holds "
                "no Tryton move id to report.",
                details={
                    "import_batch_id": command.import_batch_id,
                    "channel_key": command.channel_key,
                    "tranche_ids": list(command.tranche_ids),
                    "skipped_tranche_ids": skipped_tranche_ids,
                },
            )
        return BankReceiptPosting(
            tryton_journal_id=journal_ids[0],
            amount_irr=total_irr,
            tryton_journal_ids=tuple(journal_ids),
        )

    def _is_cleared(self, installation_id: str, tranche_id: str) -> bool:
        """True when a tranche already has a CLEARING ledger row.

        The bank receipt refuses an uncleared tranche, so the batch
        posting clears first — but only when no clearing row exists
        (a retry after a partial post must not double-clear).
        """
        return any(
            posting.kind == SettlementPostingKind.CLEARING
            for posting in self._repository.list_postings_for_tranche(
                installation_id, tranche_id
            )
        )

    def _receipt_state(
        self, installation_id: str, tranche_id: str
    ) -> tuple[int, list[int]]:
        """A tranche's ``bank_receipt`` ledger state: (total, move ids)."""
        receipts = [
            posting
            for posting in self._repository.list_postings_for_tranche(
                installation_id, tranche_id
            )
            if posting.kind == SettlementPostingKind.BANK_RECEIPT
        ]
        return sum(posting.amount_irr for posting in receipts), [
            posting.tryton_move_id
            for posting in receipts
            if posting.tryton_move_id is not None
        ]

    def _is_fully_posted(
        self, installation_id: str, tranche_id: str, posted_total: int
    ) -> bool:
        """True when a tranche's receipts already cover its received amount.

        A partially posted tranche is NOT fully posted: the service
        posts only the newly received remainder. An unknown tranche
        is not fully posted either — the service raises the
        not-found error, wrapped as a posting failure.
        """
        tranche = self._repository.get_tranche(installation_id, tranche_id)
        return (
            tranche is not None
            and tranche.received_net_irr > 0
            and posted_total >= tranche.received_net_irr
        )

    @staticmethod
    def _wrap_failure(
        command: PostBatchBankReceipt,
        tranche_id: str,
        posted_tranche_ids: list[str],
        exc: BaseException,
    ) -> SettlementPostingError:
        """Wrap ANY tranche failure as the error the tie-out catches.

        The details name the failed tranche AND the tranches already
        posted (partial progress stands — the retry skips them).
        """
        code = exc.code if isinstance(exc, HesabYarError) else type(exc).__name__
        return SettlementPostingError(
            f"Batch bank receipt failed on tranche {tranche_id}: {exc} "
            f"({len(posted_tranche_ids)} tranche(s) already posted in this "
            "batch stay posted; re-run the tie-out to post the rest).",
            details={
                "import_batch_id": command.import_batch_id,
                "channel_key": command.channel_key,
                "tranche_id": tranche_id,
                "posted_tranche_ids": list(posted_tranche_ids),
                "cause_code": code,
                "cause_message": str(exc),
            },
        )


class UnconfiguredSettlementPosting(SettlementPostingPort):
    """Fail-closed posting port: no engine implementation is wired yet."""

    def post_bank_receipt(self, command: PostBatchBankReceipt) -> BankReceiptPosting:
        """Raise: settlement posting is not configured for this installation.

        The tie-out passed and the policy gate allowed auto-post, but
        no engine posting implementation is wired (US-522). The
        caller keeps the batch in exception and files a review item.
        """
        raise SettlementPostingNotConfiguredError(
            "Settlement posting is not configured: no engine posting "
            "implementation is wired for this installation (US-522). "
            "The batch stays in exception; no automatic posting happened.",
            details={
                "import_batch_id": command.import_batch_id,
                "channel_key": command.channel_key,
                "amount_irr": command.amount_irr,
            },
        )
