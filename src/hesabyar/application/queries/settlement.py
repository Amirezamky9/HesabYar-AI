"""Settlement query service (US-521, Phase 5-3 Wave A).

Read-only views over the settlement repository: payments with their
schedules, tranches (the cash calendar's raw material), exceptions
and fee records — plus the US-523 import/matching reads (file
imports + rows, settlement events, bank deposits, matches). Every
lookup is installation-scoped; a row of another installation answers
as if it did not exist.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass

from hesabyar.application.errors import IntegrationChannelNotFoundError
from hesabyar.domain.integrations.ports import IntegrationsRegistryPort
from hesabyar.domain.settlement.errors import (
    SettlementDepositNotFoundError,
    SettlementEventNotFoundError,
    SettlementExceptionNotFoundError,
    SettlementFileImportNotFoundError,
    SettlementPaymentNotFoundError,
    SettlementTrancheNotFoundError,
)
from hesabyar.domain.settlement.models import (
    BankDeposit,
    GatewayPayment,
    SettlementEvent,
    SettlementException,
    SettlementFeeRecord,
    SettlementFileImport,
    SettlementFileRow,
    SettlementMatch,
    SettlementPosting,
    SettlementTranche,
    TrancheState,
)
from hesabyar.domain.settlement.ports import SettlementRepositoryPort

__all__ = ["FileImportDetail", "PaymentSchedule", "SettlementQueryService"]


@dataclass(frozen=True)
class FileImportDetail:
    """A file import with its rows."""

    file_import: SettlementFileImport
    rows: tuple[SettlementFileRow, ...]


@dataclass(frozen=True)
class PaymentSchedule:
    """A gateway payment with its full tranche schedule."""

    payment: GatewayPayment
    tranches: tuple[SettlementTranche, ...]


class SettlementQueryService:
    """Settlement reads over the repository + registry ports."""

    def __init__(
        self,
        repository: SettlementRepositoryPort,
        registry: IntegrationsRegistryPort,
    ) -> None:
        self._repository = repository
        self._registry = registry

    def _channel_id(self, installation_id: str, channel_key: str | None) -> str | None:
        """Resolve a channel-key filter to a channel id (404 on unknown keys)."""
        if channel_key is None:
            return None
        channel = self._registry.get_channel(installation_id, channel_key)
        if channel is None:
            raise IntegrationChannelNotFoundError(
                f"Payment channel {channel_key!r} is not in this installation's registry.",
                details={"channel_key": channel_key},
            )
        return channel.id

    # -- payments ---------------------------------------------------------

    def get_payment(self, installation_id: str, payment_id: str) -> GatewayPayment:
        """Return one payment, or raise for an unknown id."""
        payment = self._repository.get_payment(installation_id, payment_id)
        if payment is None:
            raise SettlementPaymentNotFoundError(
                f"Gateway payment {payment_id!r} does not exist.",
                details={"payment_id": payment_id},
            )
        return payment

    def get_payment_schedule(
        self, installation_id: str, payment_id: str
    ) -> PaymentSchedule:
        """Return a payment with its full tranche schedule."""
        payment = self.get_payment(installation_id, payment_id)
        tranches = self._repository.list_tranches_for_payment(installation_id, payment.id)
        return PaymentSchedule(payment=payment, tranches=tuple(tranches))

    def list_payments(
        self,
        installation_id: str,
        *,
        channel_key: str | None = None,
        status: str | None = None,
        sales_period_jalali: str | None = None,
    ) -> Sequence[GatewayPayment]:
        """Return the installation's payments (newest confirmation first)."""
        return self._repository.list_payments(
            installation_id,
            channel_id=self._channel_id(installation_id, channel_key),
            status=status,
            sales_period_jalali=sales_period_jalali,
        )

    # -- tranches ---------------------------------------------------------

    def get_tranche(self, installation_id: str, tranche_id: str) -> SettlementTranche:
        """Return one tranche, or raise for an unknown id."""
        tranche = self._repository.get_tranche(installation_id, tranche_id)
        if tranche is None:
            raise SettlementTrancheNotFoundError(
                f"Settlement tranche {tranche_id!r} does not exist.",
                details={"tranche_id": tranche_id},
            )
        return tranche

    def list_tranches(
        self,
        installation_id: str,
        *,
        channel_key: str | None = None,
        state: TrancheState | None = None,
        sales_period_jalali: str | None = None,
        due_from: datetime.date | None = None,
        due_to: datetime.date | None = None,
    ) -> Sequence[SettlementTranche]:
        """Return the installation's tranches (due date, then index)."""
        return self._repository.list_tranches(
            installation_id,
            channel_id=self._channel_id(installation_id, channel_key),
            state=state,
            sales_period_jalali=sales_period_jalali,
            due_from=due_from,
            due_to=due_to,
        )

    def list_tranches_for_payment(
        self, installation_id: str, payment_id: str
    ) -> Sequence[SettlementTranche]:
        """Return a payment's tranches (raises for an unknown payment)."""
        self.get_payment(installation_id, payment_id)
        return self._repository.list_tranches_for_payment(installation_id, payment_id)

    # -- exceptions + fee records ------------------------------------------

    def get_exception(
        self, installation_id: str, exception_id: str
    ) -> SettlementException:
        """Return one exception, or raise for an unknown id."""
        exc = self._repository.get_exception(installation_id, exception_id)
        if exc is None:
            raise SettlementExceptionNotFoundError(
                f"Settlement exception {exception_id!r} does not exist.",
                details={"exception_id": exception_id},
            )
        return exc

    def list_exceptions(
        self,
        installation_id: str,
        *,
        exception_type: str | None = None,
        status: str | None = None,
        tranche_id: str | None = None,
    ) -> Sequence[SettlementException]:
        """Return the installation's exceptions (newest first)."""
        return self._repository.list_exceptions(
            installation_id,
            exception_type=exception_type,
            status=status,
            tranche_id=tranche_id,
        )

    def list_fee_records(
        self,
        installation_id: str,
        *,
        gateway_payment_id: str | None = None,
        channel_key: str | None = None,
    ) -> Sequence[SettlementFeeRecord]:
        """Return the installation's fee records (newest first)."""
        return self._repository.list_fee_records(
            installation_id,
            gateway_payment_id=gateway_payment_id,
            channel_id=self._channel_id(installation_id, channel_key),
        )

    # -- postings (US-522) ------------------------------------------------

    def list_postings_for_payment(
        self, installation_id: str, payment_id: str
    ) -> Sequence[SettlementPosting]:
        """Return a payment's postings, oldest first (raises for an unknown payment)."""
        self.get_payment(installation_id, payment_id)
        return self._repository.list_postings_for_payment(installation_id, payment_id)

    def list_postings_for_tranche(
        self, installation_id: str, tranche_id: str
    ) -> Sequence[SettlementPosting]:
        """Return a tranche's postings, oldest first (raises for an unknown tranche)."""
        self.get_tranche(installation_id, tranche_id)
        return self._repository.list_postings_for_tranche(installation_id, tranche_id)
    # -- file imports (US-523) ----------------------------------------------

    def get_file_import(
        self, installation_id: str, import_id: str
    ) -> FileImportDetail:
        """Return one file import with its rows, or raise for an unknown id."""
        file_import = self._repository.get_file_import(installation_id, import_id)
        if file_import is None:
            raise SettlementFileImportNotFoundError(
                f"Settlement file import {import_id!r} does not exist.",
                details={"import_id": import_id},
            )
        rows = self._repository.list_file_rows(file_import.id)
        return FileImportDetail(file_import=file_import, rows=tuple(rows))

    def list_file_imports(
        self,
        installation_id: str,
        *,
        channel_key: str | None = None,
        status: str | None = None,
    ) -> Sequence[SettlementFileImport]:
        """Return the installation's file imports (newest first)."""
        return self._repository.list_file_imports(
            installation_id,
            channel_id=self._channel_id(installation_id, channel_key),
            status=status,
        )

    # -- settlement events / deposits / matches (US-523) ---------------------

    def get_settlement_event(
        self, installation_id: str, event_id: str
    ) -> SettlementEvent:
        """Return one settlement event, or raise for an unknown id."""
        event = self._repository.get_settlement_event(installation_id, event_id)
        if event is None:
            raise SettlementEventNotFoundError(
                f"Settlement event {event_id!r} does not exist.",
                details={"event_id": event_id},
            )
        return event

    def list_settlement_events(
        self,
        installation_id: str,
        *,
        channel_key: str | None = None,
        import_batch_id: str | None = None,
    ) -> Sequence[SettlementEvent]:
        """Return the installation's settlement events (newest payout first)."""
        return self._repository.list_settlement_events(
            installation_id,
            channel_id=self._channel_id(installation_id, channel_key),
            import_batch_id=import_batch_id,
        )

    def get_bank_deposit(self, installation_id: str, deposit_id: str) -> BankDeposit:
        """Return one bank deposit, or raise for an unknown id."""
        deposit = self._repository.get_bank_deposit(installation_id, deposit_id)
        if deposit is None:
            raise SettlementDepositNotFoundError(
                f"Bank deposit {deposit_id!r} does not exist.",
                details={"deposit_id": deposit_id},
            )
        return deposit

    def list_bank_deposits(
        self, installation_id: str, *, unmatched_only: bool = False
    ) -> Sequence[BankDeposit]:
        """Return the installation's bank deposits (newest first)."""
        return self._repository.list_bank_deposits(
            installation_id, unmatched_only=unmatched_only
        )

    def list_matches_for_tranche(
        self, installation_id: str, tranche_id: str
    ) -> Sequence[SettlementMatch]:
        """Return a tranche's match links (raises for an unknown tranche)."""
        self.get_tranche(installation_id, tranche_id)
        return self._repository.list_matches_for_tranche(installation_id, tranche_id)

    def list_matches_for_deposit(
        self, installation_id: str, deposit_id: str
    ) -> Sequence[SettlementMatch]:
        """Return a deposit's match links (raises for an unknown deposit)."""
        self.get_bank_deposit(installation_id, deposit_id)
        return self._repository.list_matches_for_deposit(installation_id, deposit_id)

    def list_matches_for_event(
        self, installation_id: str, event_id: str
    ) -> Sequence[SettlementMatch]:
        """Return a settlement event's match links (raises for an unknown event)."""
        self.get_settlement_event(installation_id, event_id)
        return self._repository.list_matches_for_event(installation_id, event_id)
