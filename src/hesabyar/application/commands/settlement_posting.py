"""Settlement posting service (US-522, Phase 5-3 Wave B).

Posts the phased-design §3.5 journals through the
:class:`AccountingEnginePort`, following the US-520 spike decision
(Path 1b — button-driven, ``docs/tryton-spikes-520.md`` §3.8):

* sale recognition is the invoice itself (Dr gateway receivable gross
  / Cr sales revenue, posted by the store pipeline with
  ``alternative_payees=[gateway]`` and the tranche payment term);
  this service posts the fee + fee VAT at sale under policy A
  (default), or per tranche at confirmation under policy B;
* the clearing transfer on tranche confirmation is a per-tranche
  ``account.payment`` driven ``submit → process_rest → succeed``
  with the gateway deposit date as the clearing date, the clearing
  move posted immediately, and the tranche's receivable lines
  reconciled explicitly (the NET payment never reconciles the GROSS
  invoice line on its own — the fee leg completes the sum);
* the bank receipt on tranche receipt is a direct move (Dr bank / Cr
  clearing) plus the clearing-legs reconciliation — and it refuses
  to post unless the batch tie-out passes (computed here from the
  US-521 models; US-523 owns the tie-out record itself);
* fee variances and post-settlement refunds are adjustment moves,
  never silently absorbed, always with an exception + reason.

Every posting leaves one row in the ``settlement_postings`` ledger
(the idempotency key + the Tryton move/payment/reconciliation ids).
Money is integer IRR throughout; the fee % and VAT rate come from
the payment's contract version, never from a default.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass

from hesabyar.application.errors import IntegrationChannelNotFoundError
from hesabyar.domain.accounting.commands import (
    CreateJournalDraft,
    CreatePayment,
    JournalDraftLine,
    PostJournal,
    ReconcileMoveLines,
    SucceedPayment,
)
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import (
    AccountRef,
    CompanyRef,
    InvoiceRef,
    JournalDraftRef,
    JournalRef,
    MoveLineRef,
    PartyRef,
    PaymentJournalRef,
    PaymentRef,
)
from hesabyar.domain.accounting.value_objects import (
    MoveLineView,
    PaymentView,
    ReconciliationView,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.integrations.models import (
    GatewayContract,
    IntegrationMapping,
    MappingKind,
    PaymentChannel,
)
from hesabyar.domain.integrations.ports import IntegrationsRegistryPort
from hesabyar.domain.settlement.commands import (
    ApplyRefund,
    CreateSettlementException,
    PostBankReceipt,
    PostFeeVariance,
    PostSaleFee,
    PostTrancheClearing,
    SettlementPostingWiring,
)
from hesabyar.domain.settlement.errors import (
    SettlementContractMissingError,
    SettlementPaymentNotFoundError,
    SettlementPostingExistsError,
    SettlementPostingPreconditionError,
    SettlementReconciliationFailedError,
    SettlementTieOutFailedError,
    SettlementTrancheNotFoundError,
    SettlementWiringUnresolvedError,
)
from hesabyar.domain.settlement.models import (
    GatewayPayment,
    SettlementException,
    SettlementExceptionSeverity,
    SettlementExceptionType,
    SettlementPosting,
    SettlementPostingKind,
    SettlementTranche,
    TrancheState,
)
from hesabyar.domain.settlement.ports import (
    SettlementAuditPort,
    SettlementRepositoryPort,
)
from hesabyar.domain.settlement.state_machine import require_transition

__all__ = [
    "BankReceiptResult",
    "ClearingResult",
    "FeeVarianceResult",
    "RefundResult",
    "SaleFeeResult",
    "SettlementPostingService",
    "TieOutFigures",
]

#: Tranche states a clearing may start from (the file confirmed the
#: tranche — US-523 drives the confirmation; this service posts the
#: money movement, it never confirms a tranche itself).
#: Tranche states a clearing transfer may start from. CONFIRMED is the
#: manual path (the gateway file confirms first); RECEIVED is the batch
#: path — the file confirms and the deposit proves receipt in the same
#: step, so by tie-out time the tranche is already RECEIVED and the
#: batch posting clears it late, still BEFORE its bank receipt (the
#: ledger sequence receivable → clearing → bank is preserved).
_CLEARABLE_STATES = frozenset(
    {
        TrancheState.CONFIRMED,
        TrancheState.PARTIALLY_CONFIRMED,
        TrancheState.RECEIVED,
        TrancheState.PARTIALLY_RECEIVED,
    }
)

#: Tranche states a bank receipt may start from.
_RECEIVABLE_STATES = frozenset(
    {TrancheState.RECEIVED, TrancheState.PARTIALLY_RECEIVED}
)

#: Tranche states a fee-variance allocation may touch: no clearing
#: posting exists for them yet (checked against the ledger), so no
#: engine posting has consumed their amounts.
_VARIANCE_BLOCKED_STATES = frozenset(
    {TrancheState.SETTLED, TrancheState.CANCELLED, TrancheState.WRITTEN_OFF}
)

#: Mapping-center source keys for the settlement accounts, under
#: ``GATEWAY_JOURNAL`` scoped to the channel key.
_ACCOUNT_SOURCE_KEYS = (
    ("receivable", "receivable_account"),
    ("clearing", "clearing_account"),
    ("bank", "bank_account"),
    ("fee_expense", "fee_expense_account"),
    ("fee_vat", "fee_vat_account"),
    ("revenue", "revenue_account"),
)


def _parse_engine_id(target_ref: str, kind: str) -> int | None:
    """Parse an engine-id reference: ``"42"`` or ``"tryton:{kind}:42"``."""
    text = target_ref.strip()
    if text.isdigit():
        return int(text)
    prefix = f"tryton:{kind}:"
    if text.startswith(prefix) and text[len(prefix) :].isdigit():
        return int(text[len(prefix) :])
    return None


@dataclass(frozen=True)
class TieOutFigures:
    """The C6 batch tie-out figures behind a bank receipt (§3.6).

    ``difference_irr`` is (confirmed − deductions) − deposits; the
    receipt posts only when ``abs(difference_irr) <= tolerance_irr``.
    """

    confirmed_net_irr: int
    deductions_irr: int
    matched_deposits_irr: int
    difference_irr: int
    tolerance_irr: int

    @property
    def passes(self) -> bool:
        return abs(self.difference_irr) <= self.tolerance_irr

    def as_details(self) -> dict[str, object]:
        return {
            "confirmed_net_irr": self.confirmed_net_irr,
            "deductions_irr": self.deductions_irr,
            "matched_deposits_irr": self.matched_deposits_irr,
            "difference_irr": self.difference_irr,
            "tolerance_irr": self.tolerance_irr,
        }


@dataclass(frozen=True)
class SaleFeeResult:
    """A posted sale fee (policy A) — or a zero-fee no-op."""

    payment: GatewayPayment
    fee_irr: int
    fee_vat_irr: int
    posting: SettlementPosting | None
    """None when the payment's fee + VAT are zero (nothing to recognise)."""
    tryton_move_id: int | None


@dataclass(frozen=True)
class ClearingResult:
    """A posted tranche clearing, with its reconciliation when complete."""

    tranche: SettlementTranche
    amount_irr: int
    cleared_total_irr: int
    payment: PaymentView
    posting: SettlementPosting
    reconciliation: ReconciliationView | None
    """None while the tranche is only partially cleared (reconciled on completion)."""
    fee_posting: SettlementPosting | None
    """The policy-B fee posting for this tranche, when one was made."""


@dataclass(frozen=True)
class BankReceiptResult:
    """A posted bank receipt, with its tie-out figures."""

    tranche: SettlementTranche
    amount_irr: int
    received_total_irr: int
    posting: SettlementPosting
    tie_out: TieOutFigures
    reconciliation: ReconciliationView | None
    """None while the tranche is only partially received."""


@dataclass(frozen=True)
class FeeVarianceResult:
    """A posted fee-variance adjustment — or a zero-variance no-op."""

    payment: GatewayPayment
    fee_variance_irr: int
    vat_variance_irr: int
    posting: SettlementPosting | None
    """None when actual == expected (no variance, no move, no exception)."""
    exception: SettlementException | None
    tryton_move_id: int | None
    allocations: tuple[tuple[str, int, int], ...]
    """(tranche id, fee share, VAT share) per adjusted tranche."""


@dataclass(frozen=True)
class RefundResult:
    """An applied refund: tranche allocations + the excess, if any."""

    payment: GatewayPayment
    refund_amount_irr: int
    allocations: tuple[tuple[str, int], ...]
    """(tranche id, amount deducted) per open tranche, last-to-first."""
    excess_irr: int
    """Refund beyond the open balance — a negative adjustment on a later settlement."""
    posting: SettlementPosting
    exception: SettlementException | None
    """``cancelled_after_settlement`` when an excess remains to be netted."""


class SettlementPostingService:
    """Post §3.5 settlement journals through the engine port (US-522)."""

    def __init__(
        self,
        repository: SettlementRepositoryPort,
        registry: IntegrationsRegistryPort,
        engine: AccountingEnginePort,
        audit: SettlementAuditPort | None = None,
    ) -> None:
        self._repository = repository
        self._registry = registry
        self._engine = engine
        self._audit = audit

    # -- wiring -----------------------------------------------------------

    def resolve_wiring(
        self,
        installation_id: str,
        channel_key: str,
        company: CompanyRef,
    ) -> SettlementPostingWiring:
        """Build a channel's posting wiring from the mapping center.

        Conventions (fail-closed — every missing key is listed, never
        defaulted): the gateway party is ``EXTERNAL_PARTY`` with
        source key = the channel key; the payment journal is
        ``GATEWAY_JOURNAL`` with source key = the channel key (the
        store pipeline's convention); the six accounts are
        ``GATEWAY_JOURNAL`` rows scoped to the channel with source
        keys ``receivable`` / ``clearing`` / ``bank`` /
        ``fee_expense`` / ``fee_vat`` / ``revenue`` (target ``account``,
        by engine id or chart code). The general journal is optional
        (``GATEWAY_JOURNAL`` / ``general_journal``): when unmapped the
        adapter resolves the company's single general journal.
        """
        channel = self._registry.get_channel(installation_id, channel_key)
        if channel is None:
            raise IntegrationChannelNotFoundError(
                f"Payment channel {channel_key!r} is not in this installation's registry.",
                details={"channel_key": channel_key},
            )
        unresolved: list[str] = []
        versions: list[str] = []

        party_mapping = self._registry.resolve_mapping(
            installation_id, MappingKind.EXTERNAL_PARTY, channel_key, channel_key
        )
        gateway_party = self._engine_id_target(
            party_mapping, "party", f"external_party:{channel_key}", unresolved, versions
        )
        journal_mapping = self._registry.resolve_mapping(
            installation_id, MappingKind.GATEWAY_JOURNAL, channel_key, channel_key
        )
        payment_journal = self._engine_id_target(
            journal_mapping, "journal", f"gateway_journal:{channel_key}", unresolved, versions
        )
        chart = {
            view.code: view.ref
            for view in self._engine.get_chart_of_accounts(company)
        }
        accounts: dict[str, AccountRef] = {}
        for source_key, attr in _ACCOUNT_SOURCE_KEYS:
            account = self._resolve_account(
                installation_id, channel_key, source_key, chart, unresolved, versions
            )
            if account is not None:
                accounts[attr] = account
        general_journal: JournalRef | None = None
        general_mapping = self._registry.resolve_mapping(
            installation_id, MappingKind.GATEWAY_JOURNAL, channel_key, "general_journal"
        )
        if general_mapping is not None:
            general_id = self._engine_id_target(
                general_mapping,
                "journal",
                "gateway_journal:general_journal",
                unresolved,
                versions,
            )
            if general_id is not None:
                general_journal = JournalRef(general_id)
        if unresolved:
            raise SettlementWiringUnresolvedError(
                f"Cannot resolve settlement wiring for channel {channel_key!r}: "
                + "; ".join(unresolved)
                + ". Configure the mappings in the mapping center.",
                details={
                    "channel_key": channel_key,
                    "unresolved": list(unresolved),
                    "resolved_versions": versions,
                },
            )
        assert gateway_party is not None and payment_journal is not None
        return SettlementPostingWiring(
            company=company,
            gateway_party=PartyRef(gateway_party),
            payment_journal=PaymentJournalRef(payment_journal),
            receivable_account=accounts["receivable_account"],
            clearing_account=accounts["clearing_account"],
            bank_account=accounts["bank_account"],
            fee_expense_account=accounts["fee_expense_account"],
            fee_vat_account=accounts["fee_vat_account"],
            revenue_account=accounts["revenue_account"],
            general_journal=general_journal,
        )

    def resolve_wiring_for_payment(
        self,
        installation_id: str,
        payment_id: str,
        company: CompanyRef,
    ) -> SettlementPostingWiring:
        """Resolve a payment's posting wiring (REST entry point).

        The channel comes from the payment itself; the company comes
        from the caller (the settlement center is per-company, and a
        payment carries no store/company mapping of its own).
        """
        payment = self._load_payment(installation_id, payment_id)
        channel = self._channel_for(payment)
        return self.resolve_wiring(installation_id, channel.channel_key, company)

    def resolve_wiring_for_tranche(
        self,
        installation_id: str,
        tranche_id: str,
        company: CompanyRef,
    ) -> SettlementPostingWiring:
        """Resolve a tranche's posting wiring via its payment (REST entry point)."""
        _, payment = self._load_tranche(installation_id, tranche_id)
        channel = self._channel_for(payment)
        return self.resolve_wiring(installation_id, channel.channel_key, company)

    def _engine_id_target(
        self,
        mapping: IntegrationMapping | None,
        kind: str,
        label: str,
        unresolved: list[str],
        versions: list[str],
    ) -> int | None:
        """Parse a mapping's target_ref as an engine id (or record the gap)."""
        if mapping is None:
            unresolved.append(label)
            return None
        if mapping.target_type != kind:
            unresolved.append(
                f"{label} (mapping targets {mapping.target_type!r}, not {kind!r})"
            )
            return None
        engine_id = _parse_engine_id(mapping.target_ref, kind)
        if engine_id is None:
            unresolved.append(
                f"{label} (target_ref {mapping.target_ref!r} is not an engine id)"
            )
            return None
        versions.append(f"{label}:v{mapping.version}")
        return engine_id

    def _resolve_account(
        self,
        installation_id: str,
        channel_key: str,
        source_key: str,
        chart: dict[str, AccountRef],
        unresolved: list[str],
        versions: list[str],
    ) -> AccountRef | None:
        label = f"gateway_journal:{source_key}"
        mapping = self._registry.resolve_mapping(
            installation_id, MappingKind.GATEWAY_JOURNAL, channel_key, source_key
        )
        if mapping is None:
            unresolved.append(label)
            return None
        if mapping.target_type != "account":
            unresolved.append(
                f"{label} (mapping targets {mapping.target_type!r}, not 'account')"
            )
            return None
        engine_id = _parse_engine_id(mapping.target_ref, "account")
        if engine_id is not None:
            versions.append(f"{label}:v{mapping.version}")
            return AccountRef(engine_id)
        account = chart.get(mapping.target_ref.strip())
        if account is None:
            unresolved.append(
                f"{label} (mapped account code {mapping.target_ref!r} "
                "is not in the company chart)"
            )
            return None
        versions.append(f"{label}:v{mapping.version}")
        return account

    # -- helpers ----------------------------------------------------------

    def _load_payment(self, installation_id: str, payment_id: str) -> GatewayPayment:
        payment = self._repository.get_payment(installation_id, payment_id)
        if payment is None:
            raise SettlementPaymentNotFoundError(
                f"Gateway payment {payment_id!r} does not exist.",
                details={"payment_id": payment_id},
            )
        return payment

    def _load_tranche(
        self, installation_id: str, tranche_id: str
    ) -> tuple[SettlementTranche, GatewayPayment]:
        tranche = self._repository.get_tranche(installation_id, tranche_id)
        if tranche is None:
            raise SettlementTrancheNotFoundError(
                f"Settlement tranche {tranche_id!r} does not exist.",
                details={"tranche_id": tranche_id},
            )
        return tranche, self._load_payment(installation_id, tranche.gateway_payment_id)

    def _contract_for(self, payment: GatewayPayment) -> GatewayContract:
        contract = self._registry.get_contract_by_id(payment.contract_version_id)
        if contract is None:
            raise SettlementContractMissingError(
                f"Payment {payment.id} was built from contract version "
                f"{payment.contract_version_id!r}, which no longer exists.",
                details={
                    "payment_id": payment.id,
                    "contract_version_id": payment.contract_version_id,
                },
            )
        return contract

    def _channel_for(self, payment: GatewayPayment) -> PaymentChannel:
        channel = self._registry.get_channel_by_id(payment.payment_channel_id)
        if channel is None:
            raise IntegrationChannelNotFoundError(
                f"Payment {payment.id} belongs to channel {payment.payment_channel_id!r}, "
                "which is not in the registry.",
                details={
                    "payment_id": payment.id,
                    "channel_id": payment.payment_channel_id,
                },
            )
        return channel

    def _post_move(
        self,
        wiring: SettlementPostingWiring,
        date: datetime.date,
        lines: Sequence[JournalDraftLine],
        description: str,
    ) -> int:
        """Create a draft journal and post it immediately; return the move id."""
        draft = self._engine.create_journal_draft(
            CreateJournalDraft(
                company=wiring.company,
                date=FiscalDate(date),
                lines=tuple(lines),
                description=description,
                journal=wiring.general_journal,
            )
        )
        posted = self._engine.post_journal(PostJournal(draft=draft))
        return posted.engine_id

    def _posted_total(
        self, installation_id: str, tranche_id: str, kind: SettlementPostingKind
    ) -> int:
        """Sum the amounts of a tranche's postings of one kind (cumulative math)."""
        return sum(
            posting.amount_irr
            for posting in self._repository.list_postings_for_tranche(
                installation_id, tranche_id
            )
            if posting.kind == kind
        )

    def _record_audit(
        self,
        *,
        tenant_id: str | None,
        installation_id: str,
        api_client_id: str | None,
        action: str,
        resource_type: str,
        resource_id: str | None,
        actor: str | None,
        metadata: dict[str, object] | None,
    ) -> None:
        if self._audit is None or tenant_id is None:
            return
        self._audit.record_settlement_audit(
            tenant_id=tenant_id,
            installation_id=installation_id,
            api_client_id=api_client_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            actor=actor,
            metadata=metadata,
        )

    # -- sale fee (policy A) ----------------------------------------------

    def post_sale_fee(
        self, installation_id: str, command: PostSaleFee
    ) -> SaleFeeResult:
        """Recognise a payment's fee + fee VAT at sale (policy A, §3.5).

        One posted journal: Dr fee expense + Dr fee VAT / Cr gateway
        receivable — the receivable leg is split per tranche (one line
        per tranche, party=gateway, maturity_date=tranche due) so each
        tranche reconciles independently when its clearing leg posts.
        Under policy B the service refuses: the fee is recognised per
        tranche at confirmation instead.
        """
        payment = self._load_payment(installation_id, command.payment_id)
        contract = self._contract_for(payment)
        if contract.fee_accrual_policy != "at_sale":
            raise SettlementPostingPreconditionError(
                f"Payment {payment.id} uses fee policy "
                f"{contract.fee_accrual_policy!r}: the fee is recognised per "
                "tranche at confirmation, not at sale.",
                details={
                    "payment_id": payment.id,
                    "fee_accrual_policy": contract.fee_accrual_policy,
                },
            )
        existing = self._repository.get_posting_by_kind(
            installation_id,
            SettlementPostingKind.FEE_AT_SALE.value,
            gateway_payment_id=payment.id,
        )
        if existing is not None:
            raise SettlementPostingExistsError(
                f"The sale fee for payment {payment.id} was already posted "
                f"(posting {existing.id}, Tryton move {existing.tryton_move_id}).",
                details={"payment_id": payment.id, "posting_id": existing.id},
            )
        tranches = self._repository.list_tranches_for_payment(
            installation_id, payment.id
        )
        legs = [
            (tranche, tranche.fee_irr, tranche.fee_vat_irr) for tranche in tranches
        ]
        total_fee = sum(fee for _, fee, _ in legs)
        total_vat = sum(vat for _, _, vat in legs)
        if total_fee + total_vat == 0:
            # A zero-fee channel (e.g. a credit-line contract) has
            # nothing to recognise — a no-op, not an error, and no
            # posting row: there is no journal to be idempotent about.
            return SaleFeeResult(
                payment=payment,
                fee_irr=0,
                fee_vat_irr=0,
                posting=None,
                tryton_move_id=None,
            )
        channel = self._channel_for(payment)
        lines = self._fee_lines(command.wiring, legs, channel.channel_key, payment)
        move_id = self._post_move(
            command.wiring,
            command.date,
            lines,
            f"Gateway fee — {channel.channel_key} {payment.gateway_transaction_id}",
        )
        posting = self._repository.save_posting(
            installation_id=installation_id,
            gateway_payment_id=payment.id,
            kind=SettlementPostingKind.FEE_AT_SALE.value,
            amount_irr=total_fee + total_vat,
            tryton_move_id=move_id,
            details={
                "fee_irr": total_fee,
                "fee_vat_irr": total_vat,
                "legs": [
                    {
                        "tranche_id": tranche.id,
                        "fee_irr": fee,
                        "fee_vat_irr": vat,
                    }
                    for tranche, fee, vat in legs
                ],
            },
        )
        for record in self._repository.list_fee_records(
            installation_id, gateway_payment_id=payment.id
        ):
            if record.tranche_id is None:
                self._repository.update_fee_record(
                    record.id, tryton_fee_journal_id=move_id
                )
        return SaleFeeResult(
            payment=payment,
            fee_irr=total_fee,
            fee_vat_irr=total_vat,
            posting=posting,
            tryton_move_id=move_id,
        )

    def _fee_lines(
        self,
        wiring: SettlementPostingWiring,
        legs: Sequence[tuple[SettlementTranche, int, int]],
        channel_key: str,
        payment: GatewayPayment,
    ) -> list[JournalDraftLine]:
        """Build a fee journal's lines: expense debits + per-tranche credits."""
        total_fee = sum(fee for _, fee, _ in legs)
        total_vat = sum(vat for _, _, vat in legs)
        lines: list[JournalDraftLine] = []
        if total_fee > 0:
            lines.append(
                JournalDraftLine(
                    account=wiring.fee_expense_account,
                    debit=MoneyIRR(total_fee),
                    description=f"Gateway fee — {channel_key} {payment.gateway_transaction_id}",
                )
            )
        if total_vat > 0:
            lines.append(
                JournalDraftLine(
                    account=wiring.fee_vat_account,
                    debit=MoneyIRR(total_vat),
                    description=f"Gateway fee VAT — {channel_key} {payment.gateway_transaction_id}",
                )
            )
        for tranche, fee, vat in legs:
            amount = fee + vat
            if amount <= 0:
                continue
            lines.append(
                JournalDraftLine(
                    account=wiring.receivable_account,
                    credit=MoneyIRR(amount),
                    party=wiring.gateway_party,
                    maturity_date=FiscalDate(tranche.expected_date),
                    description=(
                        f"Gateway fee tranche {tranche.tranche_index} — "
                        f"{channel_key} {payment.gateway_transaction_id}"
                    ),
                )
            )
        return lines

    # -- tranche clearing (Path 1b) ----------------------------------------

    def post_tranche_clearing(
        self, installation_id: str, command: PostTrancheClearing
    ) -> ClearingResult:
        """Post a confirmed tranche's clearing transfer (C3, Path 1b).

        Creates the tranche's ``account.payment`` ONLY now (a Draft
        payment already reduces ``amount_to_pay``), drives it
        ``submit → process_rest → succeed`` with the payout date as
        the clearing date, posts the clearing move, and reconciles the
        tranche's receivable lines once the tranche is fully cleared
        (a partial confirmation leaves the lines open for the
        completing clearing). Under policy B the tranche's fee share
        is recognised first. The tranche is ``confirmed`` on the manual
        path; on the batch path it is already ``received`` (the file
        confirms and the deposit proves receipt in the same step) and
        the tie-out clears it late, before its bank receipt.
        """
        tranche, payment = self._load_tranche(installation_id, command.tranche_id)
        contract = self._contract_for(payment)
        if tranche.state not in _CLEARABLE_STATES:
            raise SettlementPostingPreconditionError(
                f"Tranche {tranche.id} is {tranche.state.value}: only a confirmed "
                "or received tranche can be cleared (the gateway file confirms "
                "first; the batch flow clears a received tranche late, at tie-out).",
                details={"tranche_id": tranche.id, "state": tranche.state.value},
            )
        confirmed = tranche.confirmed_net_irr
        if confirmed is None:
            confirmed = tranche.net_irr
        already_cleared = self._posted_total(
            installation_id, tranche.id, SettlementPostingKind.CLEARING
        )
        amount = confirmed - already_cleared
        if amount <= 0:
            raise SettlementPostingPreconditionError(
                f"Tranche {tranche.id} is already fully cleared "
                f"(confirmed {confirmed} IRR, cleared {already_cleared} IRR).",
                details={
                    "tranche_id": tranche.id,
                    "confirmed_net_irr": confirmed,
                    "already_cleared_irr": already_cleared,
                },
            )
        if confirmed > tranche.net_irr:
            raise SettlementPostingPreconditionError(
                f"Tranche {tranche.id} confirmed {confirmed} IRR, more than its "
                f"net {tranche.net_irr} IRR — resolve the over-confirmation first.",
                details={
                    "tranche_id": tranche.id,
                    "confirmed_net_irr": confirmed,
                    "net_irr": tranche.net_irr,
                },
            )
        journal_view = self._engine.get_payment_journal(command.wiring.payment_journal)
        if journal_view is None or not journal_view.is_clearing_configured:
            raise SettlementPostingPreconditionError(
                "The payment journal has no clearing account + journal configured; "
                "`succeed` would silently create no clearing move.",
                details={
                    "payment_journal_id": command.wiring.payment_journal.engine_id,
                },
            )
        channel = self._channel_for(payment)
        fee_posting: SettlementPosting | None = None
        if contract.fee_accrual_policy == "at_settlement":
            fee_posting = self._post_tranche_fee(
                installation_id, tranche, payment, channel.channel_key, command
            )
        # Pre-check (before ANY engine write): when this clearing
        # completes the tranche, the receivable lines must sum to zero
        # with the new clearing leg — otherwise the fee leg (or the
        # invoice line) is missing and the clearing must not be
        # created at all (a failed reconcile after the payment exists
        # would leave a posted clearing move a retry cannot undo).
        if already_cleared + amount == tranche.net_irr:
            self._check_receivable_reconciles(
                installation_id, command.wiring, tranche, amount
            )
        payment_ref = self._engine.create_payment(
            CreatePayment(
                company=command.wiring.company,
                party=command.wiring.gateway_party,
                kind="receivable",
                amount=MoneyIRR(amount),
                date=FiscalDate(command.payout_date),
                journal=command.wiring.payment_journal,
                line=(
                    MoveLineRef(tranche.tryton_receivable_line_id)
                    if tranche.tryton_receivable_line_id is not None
                    else None
                ),
                account=(
                    command.wiring.receivable_account
                    if tranche.tryton_receivable_line_id is None
                    else None
                ),
                reference=f"settlement:{tranche.id}",
            )
        )
        self._engine.submit_payment(payment_ref)
        # One batch call even for a single payment: the batch groups
        # the payment into ONE account.payment.group per settlement
        # batch (per-payment calls would each open their own group).
        self._engine.process_payments([payment_ref])
        payment_view = self._engine.succeed_payment(
            SucceedPayment(
                payment=payment_ref, clearing_date=FiscalDate(command.payout_date)
            )
        )
        clearing_move = payment_view.clearing_move
        if clearing_move is None:
            raise SettlementPostingPreconditionError(
                f"Payment {payment_ref.engine_id} succeeded but created no "
                "clearing move — the journal lost its clearing configuration.",
                details={
                    "tranche_id": tranche.id,
                    "payment_id": payment_ref.engine_id,
                },
            )
        self._engine.post_journal(PostJournal(draft=clearing_move))
        cleared_total = already_cleared + amount
        reconciliation: ReconciliationView | None = None
        reconciled = False
        if cleared_total == tranche.net_irr:
            reconciliation = self._reconcile_tranche_receivable(
                installation_id, command.wiring, tranche, clearing_move
            )
            reconciled = True
        posting = self._repository.save_posting(
            installation_id=installation_id,
            gateway_payment_id=payment.id,
            kind=SettlementPostingKind.CLEARING.value,
            amount_irr=amount,
            tranche_id=tranche.id,
            tryton_move_id=clearing_move.engine_id,
            tryton_payment_id=payment_ref.engine_id,
            tryton_reconciliation_id=(
                reconciliation.ref.engine_id if reconciliation is not None else None
            ),
            details={
                "confirmed_net_irr": confirmed,
                "cleared_total_irr": cleared_total,
                "tranche_net_irr": tranche.net_irr,
                "reconciled": reconciled,
            },
        )
        return ClearingResult(
            tranche=tranche,
            amount_irr=amount,
            cleared_total_irr=cleared_total,
            payment=payment_view,
            posting=posting,
            reconciliation=reconciliation,
            fee_posting=fee_posting,
        )

    def _post_tranche_fee(
        self,
        installation_id: str,
        tranche: SettlementTranche,
        payment: GatewayPayment,
        channel_key: str,
        command: PostTrancheClearing,
    ) -> SettlementPosting | None:
        """Recognise one tranche's fee share at confirmation (policy B)."""
        existing = self._repository.get_posting_by_kind(
            installation_id,
            SettlementPostingKind.FEE_AT_SETTLEMENT.value,
            tranche_id=tranche.id,
        )
        if existing is not None:
            return existing
        if tranche.fee_irr + tranche.fee_vat_irr == 0:
            return None
        lines = self._fee_lines(
            command.wiring,
            [(tranche, tranche.fee_irr, tranche.fee_vat_irr)],
            channel_key,
            payment,
        )
        move_id = self._post_move(
            command.wiring,
            command.payout_date,
            lines,
            f"Gateway fee tranche {tranche.tranche_index} — "
            f"{channel_key} {payment.gateway_transaction_id}",
        )
        return self._repository.save_posting(
            installation_id=installation_id,
            gateway_payment_id=payment.id,
            kind=SettlementPostingKind.FEE_AT_SETTLEMENT.value,
            amount_irr=tranche.fee_irr + tranche.fee_vat_irr,
            tranche_id=tranche.id,
            tryton_move_id=move_id,
            details={
                "fee_irr": tranche.fee_irr,
                "fee_vat_irr": tranche.fee_vat_irr,
            },
        )

    def _payment_receivable_move_ids(
        self,
        installation_id: str,
        payment: GatewayPayment,
    ) -> set[int]:
        """Move ids carrying one payment's receivable legs.

        The sale invoice's move (via the payment's linked invoice,
        when recorded with one) + this payment's settlement posting
        moves (sale fee, per-tranche fee, fee variance, refund).
        Clearing moves are NOT included here — their legs carry no
        maturity date and are gathered off their move separately.
        """
        move_ids: set[int] = set()
        if payment.tryton_invoice_id is not None:
            invoice = self._engine.get_invoice(
                InvoiceRef(payment.tryton_invoice_id)
            )
            if invoice is not None and invoice.move is not None:
                move_ids.add(invoice.move.engine_id)
        for posting in self._repository.list_postings_for_payment(
            installation_id, payment.id
        ):
            if (
                posting.kind
                in (
                    SettlementPostingKind.FEE_AT_SALE,
                    SettlementPostingKind.FEE_AT_SETTLEMENT,
                    SettlementPostingKind.FEE_VARIANCE,
                    SettlementPostingKind.REFUND,
                )
                and posting.tryton_move_id is not None
            ):
                move_ids.add(posting.tryton_move_id)
        return move_ids

    def _gather_tranche_receivable_lines(
        self,
        installation_id: str,
        wiring: SettlementPostingWiring,
        tranche: SettlementTranche,
        clearing_moves: Sequence[JournalDraftRef],
    ) -> list[MoveLineView]:
        """Gather a tranche's unreconciled receivable lines, scoped to its payment.

        The invoice line (gross debit) + the fee leg (credit) +
        variance/refund legs (credits), all by (account, party,
        maturity_date = tranche due) — but ONLY from this payment's
        own moves (its sale invoice + its fee/variance/refund
        postings). Two same-day sales to one gateway share (account,
        party, maturity_date), so an unscoped query mixes their
        lines; scoping by move keeps each tranche's lines
        attributable to its own payment. Plus the receivable leg of
        each given clearing move (a clearing leg carries no maturity
        date, so it is read off its move, not by the maturity query).
        """
        payment = self._load_payment(installation_id, tranche.gateway_payment_id)
        own_moves = self._payment_receivable_move_ids(installation_id, payment)
        due = FiscalDate(tranche.expected_date)
        candidates = list(
            self._engine.list_move_lines(
                wiring.company,
                account=wiring.receivable_account,
                party=wiring.gateway_party,
                maturity_date=due,
                unreconciled_only=True,
            )
        )
        # The tranche's own receivable line (when linked) lives on
        # the payment's invoice move — take that move into the set
        # even when the payment carries no invoice link.
        if tranche.tryton_receivable_line_id is not None:
            for line in candidates:
                if line.ref.engine_id == tranche.tryton_receivable_line_id:
                    own_moves.add(line.move.engine_id)
                    break
        lines = [line for line in candidates if line.move.engine_id in own_moves]
        for move in clearing_moves:
            for line in self._engine.list_move_lines(wiring.company, move=move):
                if (
                    line.account == wiring.receivable_account
                    and line.reconciliation is None
                ):
                    lines.append(line)
        return lines

    def _prior_clearing_moves(
        self, installation_id: str, tranche: SettlementTranche
    ) -> list[JournalDraftRef]:
        """The clearing moves of a tranche's earlier clearing postings."""
        return [
            JournalDraftRef(posting.tryton_move_id)
            for posting in self._repository.list_postings_for_tranche(
                installation_id, tranche.id
            )
            if posting.kind == SettlementPostingKind.CLEARING
            and posting.tryton_move_id is not None
        ]

    def _check_receivable_reconciles(
        self,
        installation_id: str,
        wiring: SettlementPostingWiring,
        tranche: SettlementTranche,
        clearing_amount: int,
    ) -> None:
        """Pre-check (no engine writes): the tranche's receivable lines
        plus the about-to-be-created clearing leg must sum to zero.

        The new clearing leg contributes ``-clearing_amount``; the
        existing lines must total ``+clearing_amount``. Anything else
        is a missing leg (the sale fee was never posted, or the
        invoice line was never linked to this tranche) and the
        clearing must not be created.
        """
        lines = self._gather_tranche_receivable_lines(
            installation_id,
            wiring,
            tranche,
            self._prior_clearing_moves(installation_id, tranche),
        )
        total = sum(line.balance.amount for line in lines) - clearing_amount
        if len(lines) < 1 or total != 0:
            raise SettlementReconciliationFailedError(
                f"Tranche {tranche.id} receivable lines do not reconcile: "
                f"{len(lines)} existing lines plus the clearing leg of "
                f"{clearing_amount} IRR leave a net balance of {total} IRR "
                "(expected 0). A leg is missing — the sale fee may never "
                "have been posted, or the invoice line was never linked "
                "to this tranche.",
                details={
                    "tranche_id": tranche.id,
                    "line_ids": [line.ref.engine_id for line in lines],
                    "clearing_amount_irr": clearing_amount,
                    "net_balance_irr": total,
                },
            )

    def _reconcile_tranche_receivable(
        self,
        installation_id: str,
        wiring: SettlementPostingWiring,
        tranche: SettlementTranche,
        clearing_move: JournalDraftRef,
    ) -> ReconciliationView:
        """Reconcile a fully-cleared tranche's receivable lines (§5, option (a)).

        The pre-check already proved the set sums to zero; this
        re-reads the lines (fresh state, now including the new
        clearing leg) and reconciles them. A failure here can only be
        an engine-level race — the missing-leg case never reaches it.
        """
        lines = self._gather_tranche_receivable_lines(
            installation_id,
            wiring,
            tranche,
            [*self._prior_clearing_moves(installation_id, tranche), clearing_move],
        )
        refs = [line.ref for line in lines]
        total = sum(line.balance.amount for line in lines)
        if len(refs) < 2 or total != 0:
            raise SettlementReconciliationFailedError(
                f"Tranche {tranche.id} receivable lines do not reconcile: "
                f"{len(refs)} lines, net balance {total} IRR (expected 0).",
                details={
                    "tranche_id": tranche.id,
                    "line_ids": [ref.engine_id for ref in refs],
                    "net_balance_irr": total,
                },
            )
        return self._engine.reconcile_move_lines(
            ReconcileMoveLines(company=wiring.company, lines=tuple(refs))
        )

    # -- bank receipt (after tie-out) ---------------------------------------

    def compute_tie_out(
        self,
        installation_id: str,
        settlement_event_id: str,
        payment: GatewayPayment,
    ) -> TieOutFigures:
        """Compute a settlement event's batch tie-out (§3.6, C6).

        Σ confirmed tranche nets − Σ documented deductions must equal
        Σ matched bank deposits (allocated amounts), within the
        contract's ``tieout_tolerance_irr``. An undocumented
        difference is not a deduction — the whole batch stays in
        exception and NO automatic posting happens. The tolerance is
        the loosest among the contracts of the event's matched
        tranches (versions differ only across contract changes).
        """
        event = self._repository.get_settlement_event(
            installation_id, settlement_event_id
        )
        if event is None:
            raise SettlementPostingPreconditionError(
                f"Settlement event {settlement_event_id!r} does not exist.",
                details={"settlement_event_id": settlement_event_id},
            )
        matches = self._repository.list_matches_for_event(
            installation_id, settlement_event_id
        )
        confirmed_total = 0
        deposits_total = 0
        tolerance = 0
        seen_tranches: set[str] = set()
        for match in matches:
            if match.bank_deposit_id is not None:
                deposits_total += match.allocated_amount_irr
            if match.tranche_id in seen_tranches:
                continue
            seen_tranches.add(match.tranche_id)
            tranche = self._repository.get_tranche(installation_id, match.tranche_id)
            if tranche is None:
                continue
            confirmed_total += (
                tranche.confirmed_net_irr
                if tranche.confirmed_net_irr is not None
                else tranche.net_irr
            )
            tranche_payment = self._repository.get_payment(
                installation_id, tranche.gateway_payment_id
            )
            if tranche_payment is not None:
                contract = self._registry.get_contract_by_id(
                    tranche_payment.contract_version_id
                )
                if contract is not None:
                    tolerance = max(tolerance, contract.tieout_tolerance_irr)
        if not seen_tranches:
            # No matched tranches: fall back to the tranche's own
            # payment contract for the tolerance (the event may be
            # matched by deposit only).
            contract = self._contract_for(payment)
            tolerance = contract.tieout_tolerance_irr
        difference = confirmed_total - event.deductions_irr - deposits_total
        return TieOutFigures(
            confirmed_net_irr=confirmed_total,
            deductions_irr=event.deductions_irr,
            matched_deposits_irr=deposits_total,
            difference_irr=difference,
            tolerance_irr=tolerance,
        )

    def post_bank_receipt(
        self, installation_id: str, command: PostBankReceipt
    ) -> BankReceiptResult:
        """Post a received tranche's bank receipt (C6, after tie-out).

        Dr bank / Cr clearing for the newly received amount, then the
        clearing legs are reconciled once the tranche is fully
        received. Refuses unless the tranche is received, its matched
        deposits cover the amount, and the settlement event's batch
        tie-out passes. A fully received tranche moves to ``settled``.
        """
        tranche, payment = self._load_tranche(installation_id, command.tranche_id)
        if tranche.state not in _RECEIVABLE_STATES:
            raise SettlementPostingPreconditionError(
                f"Tranche {tranche.id} is {tranche.state.value}: only a received "
                "tranche has a bank receipt to post.",
                details={"tranche_id": tranche.id, "state": tranche.state.value},
            )
        received = tranche.received_net_irr
        if received is None:
            raise SettlementPostingPreconditionError(
                f"Tranche {tranche.id} has no received amount recorded — "
                "match its bank deposits first (the matching flow records "
                "received_net_irr).",
                details={"tranche_id": tranche.id},
            )
        already_received = self._posted_total(
            installation_id, tranche.id, SettlementPostingKind.BANK_RECEIPT
        )
        amount = received - already_received
        if amount <= 0:
            raise SettlementPostingPreconditionError(
                f"Tranche {tranche.id} has no newly received amount to post "
                f"(received {received} IRR, already posted {already_received} IRR).",
                details={
                    "tranche_id": tranche.id,
                    "received_net_irr": received,
                    "already_posted_irr": already_received,
                },
            )
        matches = self._repository.list_matches_for_tranche(
            installation_id, tranche.id
        )
        matched_deposits = sum(
            match.allocated_amount_irr
            for match in matches
            if match.bank_deposit_id is not None
        )
        if matched_deposits < received:
            raise SettlementPostingPreconditionError(
                f"Tranche {tranche.id} received {received} IRR but only "
                f"{matched_deposits} IRR is matched to bank deposits — "
                "match the deposits first.",
                details={
                    "tranche_id": tranche.id,
                    "received_net_irr": received,
                    "matched_deposits_irr": matched_deposits,
                },
            )
        tie_out = self.compute_tie_out(
            installation_id, command.settlement_event_id, payment
        )
        if not tie_out.passes:
            raise SettlementTieOutFailedError(
                f"Batch tie-out failed for settlement event "
                f"{command.settlement_event_id}: confirmed "
                f"{tie_out.confirmed_net_irr} − deductions "
                f"{tie_out.deductions_irr} − deposits "
                f"{tie_out.matched_deposits_irr} = difference "
                f"{tie_out.difference_irr} IRR (tolerance "
                f"{tie_out.tolerance_irr} IRR). No receipt is posted.",
                details={
                    "settlement_event_id": command.settlement_event_id,
                    "tranche_id": tranche.id,
                    **tie_out.as_details(),
                },
            )
        clearing_postings = [
            posting
            for posting in self._repository.list_postings_for_tranche(
                installation_id, tranche.id
            )
            if posting.kind == SettlementPostingKind.CLEARING
        ]
        if not clearing_postings:
            raise SettlementPostingPreconditionError(
                f"Tranche {tranche.id} was never cleared — post its clearing "
                "transfer before the bank receipt.",
                details={"tranche_id": tranche.id},
            )
        cleared_total = sum(posting.amount_irr for posting in clearing_postings)
        if received > cleared_total:
            raise SettlementPostingPreconditionError(
                f"Tranche {tranche.id} received {received} IRR, more than its "
                f"cleared {cleared_total} IRR — resolve the overpayment "
                "exception first.",
                details={
                    "tranche_id": tranche.id,
                    "received_net_irr": received,
                    "cleared_total_irr": cleared_total,
                },
            )
        channel = self._channel_for(payment)
        description = (
            f"Bank receipt tranche {tranche.tranche_index} — "
            f"{channel.channel_key} {payment.gateway_transaction_id}"
        )
        # Pre-check (before ANY engine write): when this receipt
        # completes the tranche, the clearing legs must sum to zero
        # with the new bank leg — otherwise the bank move must not be
        # created (a failed reconcile after posting would leave a
        # posted move a retry cannot undo).
        if already_received + amount == cleared_total:
            self._check_clearing_reconciles(
                installation_id, command.wiring, tranche, clearing_postings, amount
            )
        draft = self._engine.create_journal_draft(
            CreateJournalDraft(
                company=command.wiring.company,
                date=FiscalDate(command.deposit_date),
                lines=(
                    JournalDraftLine(
                        account=command.wiring.bank_account,
                        debit=MoneyIRR(amount),
                        description=description,
                    ),
                    JournalDraftLine(
                        account=command.wiring.clearing_account,
                        credit=MoneyIRR(amount),
                        description=description,
                    ),
                ),
                description=description,
                journal=command.wiring.general_journal,
            )
        )
        bank_lines = list(self._engine.list_move_lines(command.wiring.company, move=draft))
        posted = self._engine.post_journal(PostJournal(draft=draft))
        received_total = already_received + amount
        reconciliation: ReconciliationView | None = None
        reconciled = False
        if received_total == cleared_total:
            reconciliation = self._reconcile_clearing_legs(
                installation_id,
                command.wiring,
                tranche,
                clearing_postings,
                bank_lines,
                posted.engine_id,
            )
            reconciled = True
        posting = self._repository.save_posting(
            installation_id=installation_id,
            gateway_payment_id=payment.id,
            kind=SettlementPostingKind.BANK_RECEIPT.value,
            amount_irr=amount,
            tranche_id=tranche.id,
            tryton_move_id=posted.engine_id,
            tryton_reconciliation_id=(
                reconciliation.ref.engine_id if reconciliation is not None else None
            ),
            details={
                "received_total_irr": received_total,
                "cleared_total_irr": cleared_total,
                "tranche_net_irr": tranche.net_irr,
                "reconciled": reconciled,
                "tie_out": tie_out.as_details(),
            },
        )
        updated_tranche = tranche
        if received_total == tranche.net_irr:
            require_transition(tranche.state, TrancheState.SETTLED)
            updated = self._repository.update_tranche(
                tranche.id, state=TrancheState.SETTLED
            )
            assert updated is not None
            updated_tranche = updated
        return BankReceiptResult(
            tranche=updated_tranche,
            amount_irr=amount,
            received_total_irr=received_total,
            posting=posting,
            tie_out=tie_out,
            reconciliation=reconciliation,
        )

    def _gather_clearing_legs(
        self,
        installation_id: str,
        wiring: SettlementPostingWiring,
        tranche: SettlementTranche,
        clearing_postings: Sequence[SettlementPosting],
        bank_lines: Sequence[MoveLineView] = (),
        exclude_bank_move_id: int | None = None,
    ) -> list[MoveLineView]:
        """Gather a tranche's unreconciled clearing legs.

        The payment's Dr clearing leg(s) (read off each clearing
        move) + every bank receipt's Cr clearing leg (prior receipts
        from the posting ledger, this receipt's passed in as
        ``bank_lines``).
        """
        legs: list[MoveLineView] = []
        for clearing in clearing_postings:
            if clearing.tryton_payment_id is None:
                continue
            payment_view = self._engine.get_payment(
                PaymentRef(clearing.tryton_payment_id)
            )
            if payment_view is None or payment_view.clearing_move is None:
                continue
            for line in self._engine.list_move_lines(
                wiring.company, move=payment_view.clearing_move
            ):
                if (
                    line.account == wiring.clearing_account
                    and line.reconciliation is None
                ):
                    legs.append(line)
        for posting in self._repository.list_postings_for_tranche(
            installation_id, tranche.id
        ):
            if (
                posting.kind == SettlementPostingKind.BANK_RECEIPT
                and posting.tryton_move_id is not None
                and posting.tryton_move_id != exclude_bank_move_id
            ):
                for line in self._engine.list_move_lines(
                    wiring.company, move=JournalDraftRef(posting.tryton_move_id)
                ):
                    if (
                        line.account == wiring.clearing_account
                        and line.reconciliation is None
                    ):
                        legs.append(line)
        for line in bank_lines:
            if line.account == wiring.clearing_account:
                legs.append(line)
        return legs

    def _check_clearing_reconciles(
        self,
        installation_id: str,
        wiring: SettlementPostingWiring,
        tranche: SettlementTranche,
        clearing_postings: Sequence[SettlementPosting],
        bank_amount: int,
    ) -> None:
        """Pre-check (no engine writes): the tranche's clearing legs
        plus the about-to-be-created bank leg must sum to zero."""
        legs = self._gather_clearing_legs(
            installation_id, wiring, tranche, clearing_postings
        )
        total = sum(leg.balance.amount for leg in legs) - bank_amount
        if len(legs) < 1 or total != 0:
            raise SettlementReconciliationFailedError(
                f"Tranche {tranche.id} clearing legs do not reconcile: "
                f"{len(legs)} existing legs plus the bank leg of "
                f"{bank_amount} IRR leave a net balance of {total} IRR "
                "(expected 0).",
                details={
                    "tranche_id": tranche.id,
                    "line_ids": [leg.ref.engine_id for leg in legs],
                    "bank_amount_irr": bank_amount,
                    "net_balance_irr": total,
                },
            )

    def _reconcile_clearing_legs(
        self,
        installation_id: str,
        wiring: SettlementPostingWiring,
        tranche: SettlementTranche,
        clearing_postings: Sequence[SettlementPosting],
        bank_lines: Sequence[MoveLineView],
        bank_move_id: int,
    ) -> ReconciliationView:
        """Reconcile a fully-received tranche's clearing legs.

        The pre-check already proved the set sums to zero; this
        re-reads the legs (fresh state, now including the new bank
        leg) and reconciles them.
        """
        legs = self._gather_clearing_legs(
            installation_id,
            wiring,
            tranche,
            clearing_postings,
            bank_lines=bank_lines,
            exclude_bank_move_id=bank_move_id,
        )
        refs = [leg.ref for leg in legs]
        total = sum(leg.balance.amount for leg in legs)
        if len(refs) < 2 or total != 0:
            raise SettlementReconciliationFailedError(
                f"Tranche {tranche.id} clearing legs do not reconcile: "
                f"{len(refs)} legs, net balance {total} IRR (expected 0).",
                details={
                    "tranche_id": tranche.id,
                    "line_ids": [ref.engine_id for ref in refs],
                    "net_balance_irr": total,
                },
            )
        return self._engine.reconcile_move_lines(
            ReconcileMoveLines(company=wiring.company, lines=tuple(refs))
        )

    # -- fee variance -------------------------------------------------------

    def post_fee_variance(
        self,
        installation_id: str,
        command: PostFeeVariance,
        *,
        tenant_id: str | None = None,
        api_client_id: str | None = None,
    ) -> FeeVarianceResult:
        """Post a fee-variance adjustment move (§3.5).

        The gateway file showed a real fee (and VAT) different from
        the expected figures: the difference is posted as an
        adjustment move (Dr/Cr fee expense + corresponding VAT
        against the gateway receivable) — NEVER silently absorbed —
        and a ``fee_mismatch`` exception is always opened with the
        reason. The variance is allocated across the payment's
        still-adjustable tranches (no clearing posted yet), so each
        tranche's net stays the live schedule; when no tranche is
        adjustable the receivable leg carries no maturity and stands
        as a claim against the gateway.

        The explicit call IS the approval: this method posts
        immediately. The §3.7 draft-first variant (an automatic
        import that drafts the adjustment for review) is a US-523
        follow-up, not this method.
        """
        payment = self._load_payment(installation_id, command.payment_id)
        if not command.reason.strip():
            raise SettlementPostingPreconditionError(
                "A fee variance needs a reason — it is never posted silently.",
                details={"payment_id": payment.id},
            )
        tranches = self._repository.list_tranches_for_payment(
            installation_id, payment.id
        )
        if command.tranche_id is not None:
            scoped = next(
                (t for t in tranches if t.id == command.tranche_id), None
            )
            if scoped is None:
                raise SettlementTrancheNotFoundError(
                    f"Settlement tranche {command.tranche_id!r} does not exist "
                    f"for payment {payment.id}.",
                    details={
                        "tranche_id": command.tranche_id,
                        "payment_id": payment.id,
                    },
                )
            expected_fee, expected_vat = scoped.fee_irr, scoped.fee_vat_irr
            scope: Sequence[SettlementTranche] = (scoped,)
        else:
            expected_fee, expected_vat = payment.fee_irr, payment.fee_vat_irr
            scope = tranches
        fee_diff = command.actual_fee_irr - expected_fee
        vat_diff = command.actual_vat_irr - expected_vat
        if fee_diff == 0 and vat_diff == 0:
            return FeeVarianceResult(
                payment=payment,
                fee_variance_irr=0,
                vat_variance_irr=0,
                posting=None,
                exception=None,
                tryton_move_id=None,
                allocations=(),
            )
        allocations = self._allocate_variance(
            installation_id, scope, fee_diff, vat_diff, command.tranche_id is not None
        )
        channel = self._channel_for(payment)
        description = (
            f"Fee variance — {channel.channel_key} "
            f"{payment.gateway_transaction_id}: {command.reason}"
        )
        lines: list[JournalDraftLine] = []
        if fee_diff > 0:
            lines.append(
                JournalDraftLine(
                    account=command.wiring.fee_expense_account,
                    debit=MoneyIRR(fee_diff),
                    description=description,
                )
            )
        elif fee_diff < 0:
            lines.append(
                JournalDraftLine(
                    account=command.wiring.fee_expense_account,
                    credit=MoneyIRR(-fee_diff),
                    description=description,
                )
            )
        if vat_diff > 0:
            lines.append(
                JournalDraftLine(
                    account=command.wiring.fee_vat_account,
                    debit=MoneyIRR(vat_diff),
                    description=description,
                )
            )
        elif vat_diff < 0:
            lines.append(
                JournalDraftLine(
                    account=command.wiring.fee_vat_account,
                    credit=MoneyIRR(-vat_diff),
                    description=description,
                )
            )
        total_diff = fee_diff + vat_diff
        if allocations:
            for tranche, fee_share, vat_share in allocations:
                leg = fee_share + vat_share
                if leg == 0:
                    continue
                if leg > 0:
                    lines.append(
                        JournalDraftLine(
                            account=command.wiring.receivable_account,
                            credit=MoneyIRR(leg),
                            party=command.wiring.gateway_party,
                            maturity_date=FiscalDate(tranche.expected_date),
                            description=description,
                        )
                    )
                else:
                    lines.append(
                        JournalDraftLine(
                            account=command.wiring.receivable_account,
                            debit=MoneyIRR(-leg),
                            party=command.wiring.gateway_party,
                            maturity_date=FiscalDate(tranche.expected_date),
                            description=description,
                        )
                    )
        else:
            # No adjustable tranche: the leg stands unmatured as a
            # claim against the gateway (netted on a later settlement).
            if total_diff > 0:
                lines.append(
                    JournalDraftLine(
                        account=command.wiring.receivable_account,
                        credit=MoneyIRR(total_diff),
                        party=command.wiring.gateway_party,
                        description=description,
                    )
                )
            else:
                lines.append(
                    JournalDraftLine(
                        account=command.wiring.receivable_account,
                        debit=MoneyIRR(-total_diff),
                        party=command.wiring.gateway_party,
                        description=description,
                    )
                )
        move_id = self._post_move(command.wiring, command.date, lines, description)
        for tranche, fee_share, vat_share in allocations:
            new_net = tranche.net_irr - (fee_share + vat_share)
            self._repository.update_tranche(
                tranche.id,
                fee_irr=tranche.fee_irr + fee_share,
                fee_vat_irr=tranche.fee_vat_irr + vat_share,
                net_irr=new_net,
            )
        exception = self._repository.save_exception(
            CreateSettlementException(
                installation_id=installation_id,
                exception_type=SettlementExceptionType.FEE_MISMATCH.value,
                severity=SettlementExceptionSeverity.WARNING.value,
                tranche_id=command.tranche_id,
                gateway_payment_id=payment.id,
                expected_amount_irr=expected_fee + expected_vat,
                actual_amount_irr=command.actual_fee_irr + command.actual_vat_irr,
                note=(
                    f"Gateway fee differs from contract: expected "
                    f"{expected_fee + expected_vat} IRR, actual "
                    f"{command.actual_fee_irr + command.actual_vat_irr} IRR. "
                    f"Reason: {command.reason}"
                ),
            )
        )
        posting = self._repository.save_posting(
            installation_id=installation_id,
            gateway_payment_id=payment.id,
            kind=SettlementPostingKind.FEE_VARIANCE.value,
            amount_irr=total_diff,
            tranche_id=command.tranche_id,
            tryton_move_id=move_id,
            reason=command.reason,
            event_ref=command.event_ref,
            details={
                "expected_fee_irr": expected_fee,
                "expected_vat_irr": expected_vat,
                "actual_fee_irr": command.actual_fee_irr,
                "actual_vat_irr": command.actual_vat_irr,
                "fee_variance_irr": fee_diff,
                "vat_variance_irr": vat_diff,
                "allocations": [
                    {
                        "tranche_id": tranche.id,
                        "fee_share_irr": fee_share,
                        "vat_share_irr": vat_share,
                    }
                    for tranche, fee_share, vat_share in allocations
                ],
            },
        )
        if command.tranche_id is None:
            for record in self._repository.list_fee_records(
                installation_id, gateway_payment_id=payment.id
            ):
                if record.tranche_id is None:
                    self._repository.update_fee_record(
                        record.id,
                        actual_fee_irr=command.actual_fee_irr,
                        actual_vat_irr=command.actual_vat_irr,
                        fee_variance_irr=fee_diff,
                        vat_variance_irr=vat_diff,
                        source="file",
                    )
        self._record_audit(
            tenant_id=tenant_id,
            installation_id=installation_id,
            api_client_id=api_client_id,
            action="settlement.fee_variance",
            resource_type="gateway_payment",
            resource_id=payment.id,
            actor=command.actor,
            metadata={
                "fee_variance_irr": fee_diff,
                "vat_variance_irr": vat_diff,
                "reason": command.reason,
                "tryton_move_id": move_id,
            },
        )
        return FeeVarianceResult(
            payment=payment,
            fee_variance_irr=fee_diff,
            vat_variance_irr=vat_diff,
            posting=posting,
            exception=exception,
            tryton_move_id=move_id,
            allocations=tuple(
                (tranche.id, fee_share, vat_share)
                for tranche, fee_share, vat_share in allocations
            ),
        )

    def _allocate_variance(
        self,
        installation_id: str,
        scope: Sequence[SettlementTranche],
        fee_diff: int,
        vat_diff: int,
        scoped: bool,
    ) -> list[tuple[SettlementTranche, int, int]]:
        """Split a variance across a payment's adjustable tranches.

        A tranche is adjustable while no clearing posting exists for
        it and it is not settled/cancelled/written-off. A scoped
        (single-tranche) variance takes the whole difference on that
        tranche when adjustable; a payment-level variance splits by
        fee share (gross share when the expected fee is zero), the
        last tranche taking the rounding remainder so the shares sum
        exactly. An empty list means no tranche is adjustable.
        """
        adjustable: list[SettlementTranche] = []
        for tranche in scope:
            if tranche.state in _VARIANCE_BLOCKED_STATES:
                continue
            cleared = self._posted_total(
                installation_id, tranche.id, SettlementPostingKind.CLEARING
            )
            if cleared > 0:
                continue
            adjustable.append(tranche)
        if not adjustable:
            return []
        if scoped or len(adjustable) == 1:
            tranche = adjustable[0]
            self._check_variance_fits(tranche, fee_diff + vat_diff)
            return [(tranche, fee_diff, vat_diff)]
        weights = [tranche.fee_irr for tranche in adjustable]
        if sum(weights) <= 0:
            weights = [tranche.gross_irr for tranche in adjustable]
        total_weight = sum(weights)
        if total_weight <= 0:
            # No fee and no gross anywhere — put it all on the last
            # adjustable tranche (the amounts are zero, the variance
            # is not, so any split is arbitrary; keep it in one place).
            tranche = adjustable[-1]
            self._check_variance_fits(tranche, fee_diff + vat_diff)
            return [(tranche, fee_diff, vat_diff)]
        fee_shares = _split_by_weight(fee_diff, weights)
        vat_shares = _split_by_weight(vat_diff, weights)
        allocations: list[tuple[SettlementTranche, int, int]] = []
        for tranche, fee_share, vat_share in zip(
            adjustable, fee_shares, vat_shares, strict=True
        ):
            self._check_variance_fits(tranche, fee_share + vat_share)
            allocations.append((tranche, fee_share, vat_share))
        return allocations

    @staticmethod
    def _check_variance_fits(tranche: SettlementTranche, total_diff: int) -> None:
        """Fail closed when a variance would drive a tranche net negative."""
        if tranche.net_irr - total_diff < 0:
            raise SettlementPostingPreconditionError(
                f"Fee variance of {total_diff} IRR exceeds tranche "
                f"{tranche.id} net {tranche.net_irr} IRR — resolve it manually.",
                details={
                    "tranche_id": tranche.id,
                    "net_irr": tranche.net_irr,
                    "variance_irr": total_diff,
                },
            )

    # -- refunds ------------------------------------------------------------

    def apply_refund(
        self,
        installation_id: str,
        command: ApplyRefund,
        *,
        tenant_id: str | None = None,
        api_client_id: str | None = None,
    ) -> RefundResult:
        """Apply a (partial) post-settlement refund (§3.5).

        The refunded amount is deducted FIRST from the payment's OPEN
        UNREACHED tranches (last tranche backwards); any excess
        becomes a negative adjustment (``refunds_deducted_irr``) on
        the next settlement of the same gateway, with a
        ``cancelled_after_settlement`` exception. Confirmed/received
        tranches are untouched. The sales-return journal (Dr revenue
        / Cr gateway receivable) is posted for the refunded amount,
        and the posting row records the reason + refund event
        reference.

        NOTE on the journal sides: the design text (§3.5, part B)
        lists this entry as "Cr revenue / Dr gateway receivable",
        which is the reverse of the standard sales-return reversal
        (the sale was Dr receivable / Cr revenue; a return reverses
        it). This service posts the accounting-correct reversal; the
        discrepancy is recorded in docs/settlement.md.
        """
        payment = self._load_payment(installation_id, command.payment_id)
        if command.refund_amount_irr <= 0:
            raise SettlementPostingPreconditionError(
                f"A refund amount must be positive, got {command.refund_amount_irr}.",
                details={
                    "payment_id": payment.id,
                    "refund_amount_irr": command.refund_amount_irr,
                },
            )
        if not command.reason.strip():
            raise SettlementPostingPreconditionError(
                "A refund needs a reason — it is never posted silently.",
                details={"payment_id": payment.id},
            )
        tranches = self._repository.list_tranches_for_payment(
            installation_id, payment.id
        )
        remaining = command.refund_amount_irr
        allocations: list[tuple[SettlementTranche, int]] = []
        for tranche in sorted(
            (t for t in tranches if t.is_open),
            key=lambda t: t.tranche_index,
            reverse=True,
        ):
            if remaining <= 0:
                break
            take = min(remaining, tranche.net_irr)
            if take <= 0:
                continue
            allocations.append((tranche, take))
            remaining -= take
        excess = remaining
        channel = self._channel_for(payment)
        description = (
            f"Sales return — {channel.channel_key} "
            f"{payment.gateway_transaction_id}: {command.reason}"
        )
        lines: list[JournalDraftLine] = [
            JournalDraftLine(
                account=command.wiring.revenue_account,
                debit=MoneyIRR(command.refund_amount_irr),
                description=description,
            )
        ]
        for tranche, take in allocations:
            lines.append(
                JournalDraftLine(
                    account=command.wiring.receivable_account,
                    credit=MoneyIRR(take),
                    party=command.wiring.gateway_party,
                    maturity_date=FiscalDate(tranche.expected_date),
                    description=description,
                )
            )
        if excess > 0:
            # The excess is a claim against the gateway's NEXT
            # settlement — no maturity, it belongs to no tranche here.
            lines.append(
                JournalDraftLine(
                    account=command.wiring.receivable_account,
                    credit=MoneyIRR(excess),
                    party=command.wiring.gateway_party,
                    description=description,
                )
            )
        move_id = self._post_move(command.wiring, command.date, lines, description)
        for tranche, take in allocations:
            new_gross = tranche.gross_irr - take
            new_net = tranche.net_irr - take
            if new_net == 0:
                require_transition(tranche.state, TrancheState.CANCELLED)
                self._repository.update_tranche(
                    tranche.id,
                    state=TrancheState.CANCELLED,
                    gross_irr=new_gross,
                    net_irr=new_net,
                )
            else:
                self._repository.update_tranche(
                    tranche.id, gross_irr=new_gross, net_irr=new_net
                )
        updated_payment = payment
        exception: SettlementException | None = None
        if excess > 0:
            exception = self._repository.save_exception(
                CreateSettlementException(
                    installation_id=installation_id,
                    exception_type=SettlementExceptionType.CANCELLED_AFTER_SETTLEMENT.value,
                    severity=SettlementExceptionSeverity.WARNING.value,
                    gateway_payment_id=payment.id,
                    expected_amount_irr=command.refund_amount_irr,
                    actual_amount_irr=excess,
                    note=(
                        f"Refund of {command.refund_amount_irr} IRR exceeds the "
                        f"payment's open balance by {excess} IRR; the gateway "
                        f"deducts it from a later settlement. Reason: {command.reason}"
                    ),
                )
            )
            updated = self._repository.update_payment_status(
                payment.id,
                payment.status.value,
                refunds_deducted_irr=payment.refunds_deducted_irr + excess,
            )
            assert updated is not None
            updated_payment = updated
        posting = self._repository.save_posting(
            installation_id=installation_id,
            gateway_payment_id=payment.id,
            kind=SettlementPostingKind.REFUND.value,
            amount_irr=command.refund_amount_irr,
            tryton_move_id=move_id,
            reason=command.reason,
            event_ref=command.event_ref,
            details={
                "allocations": [
                    {"tranche_id": tranche.id, "amount_irr": take}
                    for tranche, take in allocations
                ],
                "excess_irr": excess,
            },
        )
        self._record_audit(
            tenant_id=tenant_id,
            installation_id=installation_id,
            api_client_id=api_client_id,
            action="settlement.refund",
            resource_type="gateway_payment",
            resource_id=payment.id,
            actor=command.actor,
            metadata={
                "refund_amount_irr": command.refund_amount_irr,
                "excess_irr": excess,
                "reason": command.reason,
                "tryton_move_id": move_id,
            },
        )
        return RefundResult(
            payment=updated_payment,
            refund_amount_irr=command.refund_amount_irr,
            allocations=tuple(
                (tranche.id, take) for tranche, take in allocations
            ),
            excess_irr=excess,
            posting=posting,
            exception=exception,
        )


def _split_by_weight(amount: int, weights: Sequence[int]) -> list[int]:
    """Split ``amount`` across ``weights``; the last share takes the remainder.

    Integer-exact: the shares always sum to ``amount`` (negative
    amounts split the same way — floor division's direction does not
    matter, the remainder rule guarantees the total).
    """
    total = sum(weights)
    shares: list[int] = []
    allocated = 0
    for weight in weights[:-1]:
        share = (amount * weight) // total if total else 0
        shares.append(share)
        allocated += share
    shares.append(amount - allocated)
    return shares
