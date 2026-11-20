"""Cheque query service (US-531 + US-532, Phase 5-4 ۵ث).

Read-only views over the cheque registry: one cheque with its
custody history + inquiry receipts, and the filtered registry list.
Every lookup is tenant/installation-scoped; a row of another
installation answers as if it did not exist.

US-532 adds the segregation seams: the list filters by kind /
purpose / endorsee_type / storage_location (commercial reports
list ``purpose=payment`` only — a collateral cheque appears in NO
commercial report, §2.2), the collateral summary (report 6 data:
received/issued memorandum totals + the secured-obligation
dimension), the endorsement summary (report 5: supplier vs other),
the cashbox/vault leaf list (§6 agreement: the counting session
must agree with this list), and the checkbook status (report 8:
engine leaf counts + the Fintech inquiry simulator).
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass

from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import CheckbookEngineRef, CompanyRef
from hesabyar.domain.cheques.checkbook import CheckbookStatus, checkbook_status
from hesabyar.domain.cheques.clearing import (
    ClearingAging,
    ClearingCandidate,
    aging_for,
)
from hesabyar.domain.cheques.errors import ChequeEngineError, ChequeNotFoundError
from hesabyar.domain.cheques.models import (
    Cheque,
    ChequeKind,
    ChequePurpose,
    ChequeSettings,
    ChequeState,
    CustodyLogEntry,
    InquiryReceipt,
    StorageLocation,
)
from hesabyar.domain.cheques.ports import ChequeRepositoryPort
from hesabyar.domain.common.dates import FiscalDate

__all__ = [
    "CashboxLeaf",
    "ChequeDetail",
    "ChequeQueryService",
    "CollateralSummary",
    "EndorsementSummary",
    "InquiryLedger",
]


@dataclass(frozen=True)
class InquiryLedger:
    """Aggregate inquiry ledger (report 13 inputs, US-534).

    Counts by channel, colour distribution (numeric codes 1–5) and
    the AGGREGATE cost: SMS 350 Toman / Ehrazino 1,700 Toman /
    Fintech per contract (stored cost_irr is 0 while the tariff is
    UNVERIFIED — the total therefore understates Fintech, by design).
    """

    total_receipts: int
    by_channel: dict[str, int]
    color_distribution: dict[int, int]
    total_cost_irr: int


@dataclass(frozen=True)
class ChequeDetail:
    """A cheque with its custody history and inquiry receipts."""

    cheque: Cheque
    custody_entries: tuple[CustodyLogEntry, ...]
    inquiry_receipts: tuple[InquiryReceipt, ...]


@dataclass(frozen=True)
class CollateralSummary:
    """Report 6 data: collateral cheques, memorandum-only (§2.2).

    ``received_total_irr`` / ``issued_total_irr`` are the
    memorandum balances (the 7.1.1 / 7.1.2 account balances equal
    these sums by construction — the journals are memorandum-only).
    ``by_obligation`` maps each secured obligation to its cheques
    (the dimension report 6 answers with); cheques without a
    recorded obligation land under ``None``.
    """

    cheques: tuple[Cheque, ...]
    received_total_irr: int
    issued_total_irr: int
    by_obligation: dict[str | None, tuple[Cheque, ...]]


@dataclass(frozen=True)
class EndorsementSummary:
    """Report 5 data: endorsed cheques, supplier vs other (§2.1).

    The endorsement to a non-supplier (``endorsee_type=other``) is
    shown separately — its journal is accountant-gated (§9.1 #12).
    """

    supplier: tuple[Cheque, ...]
    other: tuple[Cheque, ...]

    @property
    def supplier_total_irr(self) -> int:
        return sum(c.amount_irr for c in self.supplier)

    @property
    def other_total_irr(self) -> int:
        return sum(c.amount_irr for c in self.other)


@dataclass(frozen=True)
class CashboxLeaf:
    """One leaf the §6 cash-box count must agree with (§1.1).

    A leaf in the cashbox or the vault is a ``held`` (or
    ``called_in``) cheque whose storage is that location — nothing
    else counts.
    """

    cheque_id: str
    sayad_id: str
    leaf_number: str | None
    storage_location: StorageLocation
    amount_irr: int


class ChequeQueryService:
    """Read use cases for the cheque registry."""

    def __init__(
        self,
        repository: ChequeRepositoryPort,
        engine: AccountingEnginePort | None = None,
    ) -> None:
        self._repository = repository
        self._engine = engine

    def get_cheque(
        self, cheque_id: str, *, tenant_id: str, installation_id: str
    ) -> ChequeDetail:
        cheque = self._repository.get(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        if cheque is None:
            raise ChequeNotFoundError(cheque_id)
        return ChequeDetail(
            cheque=cheque,
            custody_entries=tuple(self._repository.list_custody_entries(cheque.id)),
            inquiry_receipts=tuple(self._repository.list_inquiry_receipts(cheque.id)),
        )

    def list_cheques(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        state: ChequeState | None = None,
        issuer_national_id: str | None = None,
        due_before: datetime.date | None = None,
        kind: ChequeKind | None = None,
        purpose: ChequePurpose | None = None,
        endorsee_type: str | None = None,
        storage_location: StorageLocation | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Cheque]:
        return self._repository.list(
            tenant_id=tenant_id,
            installation_id=installation_id,
            state=state,
            issuer_national_id=issuer_national_id,
            due_before=due_before,
            kind=kind,
            purpose=purpose,
            endorsee_type=endorsee_type,
            storage_location=storage_location,
            limit=limit,
            offset=offset,
        )

    def list_commercial_cheques(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Cheque]:
        """Commercial cheques only — the segregation seam (§2.2).

        Every commercial report, receivables/payables balance, cash
        forecast and sales/tax report reads through here (or an
        equivalent ``purpose=payment`` filter): a collateral cheque
        can never leak into one.
        """
        return self.list_cheques(
            tenant_id=tenant_id,
            installation_id=installation_id,
            purpose=ChequePurpose.PAYMENT,
            limit=limit,
            offset=offset,
        )

    def collateral_summary(
        self,
        *,
        tenant_id: str,
        installation_id: str,
    ) -> CollateralSummary:
        """Report 6 data: all collateral cheques + memorandum totals."""
        cheques = tuple(
            self._repository.list(
                tenant_id=tenant_id,
                installation_id=installation_id,
                purpose=ChequePurpose.COLLATERAL,
                limit=1000,
            )
        )
        received_total = sum(
            c.amount_irr for c in cheques if c.kind == ChequeKind.RECEIVED
        )
        issued_total = sum(
            c.amount_irr for c in cheques if c.kind == ChequeKind.ISSUED
        )
        by_obligation: dict[str | None, list[Cheque]] = {}
        for cheque in cheques:
            by_obligation.setdefault(cheque.secured_obligation, []).append(cheque)
        return CollateralSummary(
            cheques=cheques,
            received_total_irr=received_total,
            issued_total_irr=issued_total,
            by_obligation={
                key: tuple(value) for key, value in by_obligation.items()
            },
        )

    def endorsement_summary(
        self,
        *,
        tenant_id: str,
        installation_id: str,
    ) -> EndorsementSummary:
        """Report 5 data: endorsed cheques split by endorsee type."""
        endorsed = self._repository.list(
            tenant_id=tenant_id,
            installation_id=installation_id,
            state=ChequeState.ENDORSED,
            limit=1000,
        )
        return EndorsementSummary(
            supplier=tuple(
                c for c in endorsed if c.endorsee_type == "supplier"
            ),
            other=tuple(c for c in endorsed if c.endorsee_type == "other"),
        )

    def cashbox_vault_leaves(
        self,
        *,
        tenant_id: str,
        installation_id: str,
    ) -> tuple[CashboxLeaf, ...]:
        """Leaves the §6 cash-box count must agree with (§1.1).

        Held (or called-in) cheques stored in the cashbox or the
        vault — the registry side of the agreement. A custody move
        is never a journal, so this list is the only source of
        truth for which leaves the count must find.
        """
        leaves: list[CashboxLeaf] = []
        for state in (ChequeState.HELD, ChequeState.CALLED_IN):
            for location in (StorageLocation.CASHBOX, StorageLocation.VAULT):
                for cheque in self._repository.list(
                    tenant_id=tenant_id,
                    installation_id=installation_id,
                    state=state,
                    storage_location=location,
                    limit=1000,
                ):
                    leaves.append(
                        CashboxLeaf(
                            cheque_id=cheque.id,
                            sayad_id=cheque.sayad_id,
                            leaf_number=cheque.leaf_number,
                            storage_location=location,
                            amount_irr=cheque.amount_irr,
                        )
                    )
        leaves.sort(key=lambda leaf: leaf.sayad_id)
        return tuple(leaves)

    def get_checkbooks(
        self, *, company_engine_id: int
    ) -> tuple[CheckbookStatus, ...]:
        """Report 8 data: every checkbook with leaf counts + inquiry."""
        if self._engine is None:
            raise ChequeEngineError("No engine is wired for checkbook reads.")
        company = CompanyRef(engine_id=company_engine_id)
        return tuple(
            checkbook_status(view)
            for view in self._engine.list_engine_checkbooks(company)
        )

    def get_checkbook_status(
        self, *, company_engine_id: int, checkbook_engine_id: int
    ) -> CheckbookStatus:
        """Report 8 data for one checkbook (leaf counts + inquiry)."""
        if self._engine is None:
            raise ChequeEngineError("No engine is wired for checkbook reads.")
        view = self._engine.get_engine_checkbook(
            CompanyRef(engine_id=company_engine_id),
            CheckbookEngineRef(engine_id=checkbook_engine_id),
        )
        if view is None:
            raise ChequeNotFoundError(f"checkbook:{checkbook_engine_id}")
        return checkbook_status(view)
    def list_intake_queue(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Cheque]:
        """Registered cheques awaiting the physical leaf (US-533).

        The intake queue: oldest first, so the cheques waiting
        longest surface first — the day-3 and day-(N-1) reminders
        are filed by the daily Procrastinate job. A cheque leaves
        the queue when its leaf arrives (``registered -> held``).
        """
        registered = self._repository.list(
            tenant_id=tenant_id,
            installation_id=installation_id,
            state=ChequeState.REGISTERED,
            limit=limit + offset,
            offset=0,
        )
        oldest_first = sorted(registered, key=lambda c: (c.created_at, c.id))
        return oldest_first[offset : offset + limit]

    def get_settings(self, installation_id: str) -> ChequeSettings:
        settings = self._repository.get_settings(installation_id)
        if settings is not None:
            return settings
        return ChequeSettings(installation_id=installation_id)

    def list_in_clearing_aging(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        today: datetime.date | None = None,
    ) -> tuple[ClearingAging, ...]:
        """Ages of all in-clearing cheques (US-534, report 4 inputs).

        A stale cheque stays in_clearing — this only reports the
        flag; a bounce still needs the bank's non-payment
        certificate or an inquiry, never age alone.
        """
        day = FiscalDate(today or datetime.date.today())
        settings = self.get_settings(installation_id)
        cheques = self._repository.list(
            tenant_id=tenant_id,
            installation_id=installation_id,
            state=ChequeState.IN_CLEARING,
            limit=1000,
        )
        return tuple(
            aging_for(
                ClearingCandidate(
                    cheque_id=cheque.id,
                    sayad_id=cheque.sayad_id,
                    leaf_number=cheque.leaf_number,
                    amount_irr=cheque.amount_irr,
                    deposit_date=cheque.deposit_date,
                    destination_bank_account_id=cheque.destination_bank_account_id,
                    direction="in" if cheque.kind.value == "received" else "out",
                    kind=cheque.kind.value,
                    state=cheque.state.value,
                ),
                day,
                clearing_window_days=settings.clearing_window_days,
            )
            for cheque in cheques
        )

    def inquiry_ledger(
        self, *, tenant_id: str, installation_id: str
    ) -> InquiryLedger:
        """Aggregate inquiry ledger (US-534, report 13 inputs)."""
        cheques = self._repository.list(
            tenant_id=tenant_id,
            installation_id=installation_id,
            limit=1000,
        )
        by_channel: dict[str, int] = {}
        color_distribution: dict[int, int] = {}
        total_cost = 0
        total = 0
        for cheque in cheques:
            for receipt in self._repository.list_inquiry_receipts(cheque.id):
                total += 1
                by_channel[receipt.channel.value] = (
                    by_channel.get(receipt.channel.value, 0) + 1
                )
                total_cost += receipt.cost_irr
                if receipt.inquiry_type.value == "color" and receipt.result_code.startswith(
                    "color="
                ):
                    try:
                        code = int(receipt.result_code.split("=", 1)[1])
                    except ValueError:
                        continue
                    if 1 <= code <= 5:
                        color_distribution[code] = color_distribution.get(code, 0) + 1
        return InquiryLedger(
            total_receipts=total,
            by_channel=by_channel,
            color_distribution=color_distribution,
            total_cost_irr=total_cost,
        )
