"""Cheque repository port (US-531).

The application service depends only on this protocol; the
SQLAlchemy implementation lives in
``hesabyar.infrastructure.persistence.postgres.cheques_repository``.
Engine calls do NOT go through a cheque-specific port — they go
through :class:`AccountingEnginePort` (``create_engine_cheque`` /
``transition_engine_cheque``), the one seam to ``account_check_ir``.

Tenant/installation scoping: every read is scoped to
(tenant_id, installation_id); a row of another installation answers
as if it did not exist (the service maps that to 404).
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from typing import Protocol

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
from hesabyar.domain.cheques.validation import ChequeRegistration


class ChequeRepositoryPort(Protocol):
    """HesabYar-side cheque registry persistence."""

    def create(self, cheque: Cheque) -> Cheque:
        """Insert a new registry row (state ``registered``, no journal)."""
        ...

    def get(self, cheque_id: str, *, tenant_id: str, installation_id: str) -> Cheque | None:
        """Return one cheque, or None when missing / out of scope."""
        ...

    def get_by_sayad_id(
        self, sayad_id: str, *, tenant_id: str, installation_id: str
    ) -> Cheque | None:
        """Return the LIVE cheque with this sayad_id, or None.

        Live = any state except ``voided`` / ``expired_unreceived`` /
        ``rejected_mismatch`` (those may be re-registered).
        """
        ...

    def find_by_sayad_id(
        self, sayad_id: str, *, tenant_id: str, installation_id: str
    ) -> Cheque | None:
        """Return the NEWEST cheque with this sayad_id, any state, or None.

        Unlike :meth:`get_by_sayad_id` this includes terminal rows —
        callers that must tell "already terminal" apart from "never
        existed" (a replayed site cancellation, US-533) use this.
        """
        ...

    def list(
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
        """List registry rows, newest first.

        The ``kind`` / ``purpose`` / ``endorsee_type`` /
        ``storage_location`` filters are the segregation seams:
        commercial reports list ``purpose=payment`` only (a
        collateral cheque appears in NO commercial report — §2.2),
        report 5 splits endorsements by ``endorsee_type``, report 6
        lists ``purpose=collateral`` with the secured-obligation
        dimension, and report 16 / the §6 cash-box count list by
        ``storage_location``.
        """
        ...

    def save(self, cheque: Cheque) -> Cheque:
        """Persist an in-memory mutated cheque (state + move refs + fields)."""
        ...

    def add_custody_entry(self, entry: CustodyLogEntry) -> CustodyLogEntry:
        """Append one custody-log entry (never updated, never deleted)."""
        ...

    def list_custody_entries(self, cheque_id: str) -> Sequence[CustodyLogEntry]:
        """Return a cheque's custody history, oldest first."""
        ...

    def add_inquiry_receipt(self, receipt: InquiryReceipt) -> InquiryReceipt:
        """Store one structured inquiry receipt (verdict/code/time only)."""
        ...

    def list_inquiry_receipts(self, cheque_id: str) -> Sequence[InquiryReceipt]:
        """Return a cheque's inquiry receipts, oldest first."""
        ...

    def get_settings(self, installation_id: str) -> ChequeSettings | None:
        """Return the installation's cheque settings, or None (defaults apply)."""
        ...

    def save_settings(self, settings: ChequeSettings) -> ChequeSettings:
        """Upsert the installation's cheque settings."""
        ...

    def list_registered_before(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        registered_before: datetime.datetime,
    ) -> Sequence[Cheque]:
        """Return ``registered`` cheques created before the given time.

        The expiry/reminder job uses this: day-3 and day-(N-1)
        reminders, and the ``expired_unreceived`` transition at the
        deadline.
        """
        ...

    def find_registered_cheques(self) -> Sequence[Cheque]:
        """Return every ``registered`` cheque, any tenant/installation.

        The daily cheque job scans this (the intake queue is small);
        per-installation settings decide each cheque's deadline.
        """
        ...

    def find_in_clearing_cheques(self) -> Sequence[Cheque]:
        """Return every ``in_clearing`` cheque, any tenant/installation.

        The daily cheque job scans this for stale in-clearing
        cheques (US-533): a cheque past its due date + the
        installation's clearing window with no bank document is
        filed to the review queue as ``stale_in_clearing`` — it is
        NEVER auto-bounced.
        """
        ...


class ChequeScanStorePort(Protocol):
    """Where leaf scans downloaded from ``scan_url`` are kept (US-533).

    The store downloads the scan into HesabYar's filestore and
    returns the stored reference (``scan_ref`` on the registry row);
    the signed site URL itself is kept on the row as ``scan_url``
    for traceability. A download failure raises
    :class:`ChequeScanDownloadError` — the intake service treats it
    as best-effort (the intake still succeeds; the scan can be
    uploaded manually later) and never lets it fail the intake.
    """

    def store_scan(self, *, sayad_id: str, scan_url: str) -> str:
        """Download ``scan_url`` into the filestore; return its ref."""
        ...


class ChequeScanDownloadError(Exception):
    """A leaf scan could not be downloaded from its ``scan_url``."""


def registration_to_cheque(
    registration: ChequeRegistration,
    *,
    cheque_id: str,
    tenant_id: str,
    installation_id: str,
    company_engine_id: int,
    source: str = "manual",
    secured_obligation: str | None = None,
    checkbook_engine_id: int | None = None,
    source_event_id: str | None = None,
) -> Cheque:
    """Build the registry row for a validated registration.

    The row starts in ``registered`` with NO journal and NO engine
    move — intake is a claim, not a delivery (§2.1). ``sayad_status``
    starts ``registered`` when the site says the leaf is Sayyad
    registered (still to be verified by inquiry before ``held``);
    otherwise ``unregistered`` with the ``sayad_unconfirmed`` flag
    and the explicit 'do not accept an unregistered cheque' warning
    carried on the record. ``secured_obligation`` (which obligation
    a collateral cheque secures — report 6) and
    ``checkbook_engine_id`` (which checkbook an issued leaf was
    drawn from) are NOT part of the 24-field site payload; they
    travel alongside it from the manual form / the issue call.
    """
    from hesabyar.domain.cheques.models import SayadStatus  # noqa: PLC0415 - local to avoid a cycle

    return Cheque(
        id=cheque_id,
        tenant_id=tenant_id,
        installation_id=installation_id,
        company_engine_id=company_engine_id,
        kind=registration.kind,
        purpose=registration.purpose,
        sayad_id=registration.sayad_id,
        amount_irr=registration.amount_irr,
        issue_date=registration.issue_date,
        due_date=registration.due_date,
        issuer_name=registration.issuer_name,
        issuer_national_id=registration.issuer_national_id,
        issuer_bank=registration.issuer_bank,
        payee_name=registration.payee_name,
        payee_national_id=registration.payee_national_id,
        order_id=registration.order_id,
        customer_ref=registration.customer_ref,
        sayad_registered=registration.sayad_registered,
        source=source,
        state=ChequeState.REGISTERED,
        sayad_status=(
            SayadStatus.REGISTERED if registration.sayad_registered else SayadStatus.UNREGISTERED
        ),
        sayad_unconfirmed=not registration.sayad_registered,
        leaf_number=registration.leaf_number,
        issuer_branch=registration.issuer_branch,
        issuer_account_number=registration.issuer_account_number,
        reason=registration.reason,
        invoice_id=registration.invoice_id,
        installment_leg_id=registration.installment_leg_id,
        scan_url=registration.scan_url,
        scan_ref=registration.scan_ref,
        registered_by=registration.registered_by,
        notes=registration.notes,
        secured_obligation=secured_obligation,
        checkbook_engine_id=checkbook_engine_id,
        source_event_id=source_event_id,
    )
