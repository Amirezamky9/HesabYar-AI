"""Unit tests for the reminder service (US-564, Phase 5-7 ۵خ W3).

The daily job + preferences over in-memory fakes. Reuses the SMS
world (``tests.unit.test_sms_services.World``: fake SMS registry,
fake installment repository, fake SMS clients, review-queue
fakes), the fake cheque repository, and the in-memory cash-box
port. "Today" is pinned explicitly (2026-11-20 = 1405/08/29);
"now" is pinned to 08:30 Tehran unless a test says otherwise.
No live SMS call is ever made from a test.
"""

from __future__ import annotations

import dataclasses
import datetime
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from hesabyar.application.commands.reminders import ReminderService
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.queries.reminders import ReminderQueryService
from hesabyar.domain.cashbox.models import (
    CashBox,
    CashBoxKind,
    CountSession,
    CountSessionStatus,
)
from hesabyar.domain.cheques.models import (
    Cheque,
    ChequeKind,
    ChequePurpose,
    ChequeState,
)
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.installments.models import (
    InstallmentReceipt,
    LegAllocation,
    PaymentMethod,
    PlanKind,
)
from hesabyar.domain.reminders.errors import (
    ReminderAlreadySentError,
    ReminderValidationError,
)
from hesabyar.domain.reminders.models import (
    SENT_REMINDER_LOG_STATUSES,
    ReminderChannel,
    ReminderLogEntry,
    ReminderLogStatus,
    ReminderPreferences,
    ReminderType,
)
from hesabyar.domain.reminders.ports import EmailSendResult
from hesabyar.domain.review_queue.models import ReviewItemType, ReviewSeverity
from hesabyar.domain.sms.commands import UpdateSmsProvider
from hesabyar.domain.sms.errors import SmsProviderRejectedError
from hesabyar.domain.sms.models import (
    SmsDeliveryStatus,
    SmsLogStatus,
)
from hesabyar.domain.sms.ports import SmsSendResult
from tests.unit.test_cashbox_services import InMemoryCashBox
from tests.unit.test_cheque_services import FakeChequeRepository
from tests.unit.test_installment_services import (
    COMPANY_ENGINE_ID,
    INSTALLATION_ID,
    TENANT_ID,
)
from tests.unit.test_review_queue_services import (
    InMemoryReviewAudit,
    InMemoryReviewQueue,
)
from tests.unit.test_sms_services import (
    INVOICE_ID,
    PHONE,
    TODAY,
    FakeSmsClient,
    World,
    _fiscal,
)

#: 08:30 Tehran — the scheduled run time, just after quiet hours end.
NOW = datetime.datetime(2026, 11, 20, 8, 30, tzinfo=datetime.UTC)

#: 23:00 — inside the default quiet hours (22:00–08:00).
QUIET_NOW = datetime.datetime(2026, 11, 20, 23, 0, tzinfo=datetime.UTC)

REVIEW_NOW = datetime.datetime(2026, 11, 20, 8, 30, tzinfo=datetime.UTC)


@dataclass
class FakeReminderRepository:
    """In-memory reminder repository (dedup mirrors the SQL one)."""

    preferences: dict[str, ReminderPreferences] = field(default_factory=dict)
    log: list[ReminderLogEntry] = field(default_factory=list)

    def get_preferences(
        self, *, installation_id: str
    ) -> ReminderPreferences | None:
        return self.preferences.get(installation_id)

    def save_preferences(
        self, preferences: ReminderPreferences
    ) -> ReminderPreferences:
        stamped = dataclasses.replace(
            preferences,
            created_at=REVIEW_NOW,
            updated_at=REVIEW_NOW,
        )
        self.preferences[preferences.installation_id] = stamped
        return stamped

    def list_preferences_enabled(self) -> list[ReminderPreferences]:
        return [p for p in self.preferences.values() if p.enabled]

    def insert_log(self, entry: ReminderLogEntry) -> ReminderLogEntry:
        if entry.status in SENT_REMINDER_LOG_STATUSES and self.find_sent_log(
            installation_id=entry.installation_id,
            idempotency_key=entry.idempotency_key,
        ) is not None:
            raise ReminderAlreadySentError(idempotency_key=entry.idempotency_key)
        stamped = dataclasses.replace(entry, created_at=REVIEW_NOW)
        self.log.append(stamped)
        return stamped

    def find_sent_log(
        self, *, installation_id: str, idempotency_key: str
    ) -> ReminderLogEntry | None:
        for entry in self.log:
            if (
                entry.installation_id == installation_id
                and entry.idempotency_key == idempotency_key
                and entry.status in SENT_REMINDER_LOG_STATUSES
            ):
                return entry
        return None

    def list_log(
        self,
        *,
        installation_id: str,
        reminder_type: ReminderType | None = None,
        subject_id: str | None = None,
        channel: ReminderChannel | None = None,
        status: ReminderLogStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ReminderLogEntry]:
        found = [e for e in self.log if e.installation_id == installation_id]
        if reminder_type is not None:
            found = [e for e in found if e.reminder_type == reminder_type]
        if subject_id is not None:
            found = [e for e in found if e.subject_id == subject_id]
        if channel is not None:
            found = [e for e in found if e.channel == channel]
        if status is not None:
            found = [e for e in found if e.status == status]
        return found[offset : offset + limit]


@dataclass
class FakeEmailSender:
    """A canned email sender: records sends, returns a canned result or raises."""

    result: EmailSendResult | None = None
    error: Exception | None = None
    sends: list[dict[str, object]] = field(default_factory=list)

    def send(self, *, to: str, subject: str, body: str) -> EmailSendResult:
        self.sends.append({"to": to, "subject": subject, "body": body})
        if self.error is not None:
            raise self.error
        return self.result or EmailSendResult(provider_message_id="mail-fake-1")


def _make_cheque(
    cheque_id: str,
    *,
    due: datetime.date,
    state: ChequeState = ChequeState.HELD,
    kind: ChequeKind = ChequeKind.RECEIVED,
    purpose: ChequePurpose = ChequePurpose.PAYMENT,
    customer_ref: str | None = PHONE,
    installment_leg_id: str | None = None,
    amount_irr: int = 10_000_000,
) -> Cheque:
    return Cheque(
        id=cheque_id,
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        company_engine_id=COMPANY_ENGINE_ID,
        kind=kind,
        purpose=purpose,
        sayad_id="1234567890123456",
        amount_irr=amount_irr,
        issue_date=_fiscal(TODAY - datetime.timedelta(days=30)),
        due_date=_fiscal(due),
        issuer_name="علی رضایی",
        issuer_national_id="0012345678",
        issuer_bank="ملت",
        payee_name="فروشگاه حیات",
        payee_national_id="0012345679",
        order_id="order-1",
        customer_ref=customer_ref or "",
        sayad_registered=True,
        state=state,
        installment_leg_id=installment_leg_id,
    )


def _make_box(box_id: str, *, created: datetime.date, active: bool = True) -> CashBox:
    return CashBox(
        id=box_id,
        tenant_id=TENANT_ID,
        company_id="company-1",
        name_fa=f"صندوق {box_id}",
        kind=CashBoxKind.STORE,
        statement_journal_id=1,
        cash_account_id=1,
        cash_account_code="101",
        engine_company_id=COMPANY_ENGINE_ID,
        active=active,
        created_at=datetime.datetime.combine(created, datetime.time(9, 0), tzinfo=datetime.UTC),
    )


def _make_session(
    session_id: str,
    box_id: str,
    *,
    created: datetime.date,
    status: CountSessionStatus = CountSessionStatus.ARCHIVED,
) -> CountSession:
    return CountSession(
        id=session_id,
        tenant_id=TENANT_ID,
        company_id="company-1",
        cash_box_id=box_id,
        cashier="cashier-1",
        supervisor="supervisor-1",
        status=status,
        book_balance_irr=0,
        created_at=datetime.datetime.combine(created, datetime.time(9, 0), tzinfo=datetime.UTC),
        engine_company_id=COMPANY_ENGINE_ID,
    )


@dataclass
class ReminderWorld:
    """Fresh fakes + the reminder service for one test."""

    sms_world: World = field(default_factory=World)
    reminders_repo: FakeReminderRepository = field(default_factory=FakeReminderRepository)
    cheques: FakeChequeRepository = field(default_factory=FakeChequeRepository)
    cashbox: InMemoryCashBox = field(default_factory=InMemoryCashBox)
    review_queue: InMemoryReviewQueue = field(default_factory=InMemoryReviewQueue)
    review_audit: InMemoryReviewAudit = field(default_factory=InMemoryReviewAudit)
    email: FakeEmailSender | None = None

    @property
    def review(self) -> ReviewQueueCommandService:
        return ReviewQueueCommandService(self.review_queue, self.review_audit, now=REVIEW_NOW)

    def service(self, *, review: bool = True) -> ReminderService:
        return ReminderService(
            self.reminders_repo,
            self.cheques,
            self.sms_world.installments,
            self.cashbox,
            self.sms_world.sms,
            client_factory=self.sms_world.clients,
            vault=self.sms_world.vault,
            review=self.review if review else None,
            queue=self.review_queue,
            email=self.email,
        )

    def query_service(self) -> ReminderQueryService:
        return ReminderQueryService(self.reminders_repo)

    def enable_preferences(self, **overrides: object) -> ReminderPreferences:
        """Save an enabled policy (defaults + overrides)."""
        payload: dict[str, object] = {"enabled": True}
        payload.update(overrides)
        return self.service().save_preferences(
            tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, payload=payload
        )

    def simulator_client(self) -> FakeSmsClient:
        """The simulator fake client, creating it if no send happened yet."""
        existing = self.sms_world.clients.clients.get("simulator")
        if existing is not None:
            return existing
        return self.sms_world.clients.client_for(
            provider_kind="simulator",
            provider_key="simulator",
            credentials={},
            line_number=None,
        )

    def review_items(self, item_type: ReviewItemType | None = None):
        items = list(self.review_queue.items.values())
        if item_type is not None:
            items = [i for i in items if i.item_type == item_type]
        return items


@pytest.fixture()
def world() -> ReminderWorld:
    return ReminderWorld()


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------


def test_save_preferences_round_trip(world: ReminderWorld) -> None:
    prefs = world.service().save_preferences(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        payload={
            "enabled": True,
            "cheque_lead_days": [5],
            "cashbox_uncounted_days": 14,
            "enmad_expiry_date": "2026-12-20",
            "notification_email": "ops@example.com",
        },
    )
    assert prefs.enabled is True
    assert prefs.cheque_lead_days == (5,)
    assert prefs.cashbox_uncounted_days == 14
    assert prefs.enmad_expiry_date == datetime.date(2026, 12, 20)
    assert prefs.notification_email == "ops@example.com"
    assert world.query_service().get_preferences(
        tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    ) == prefs


def test_preferences_validation_error_is_typed(world: ReminderWorld) -> None:
    with pytest.raises(ReminderValidationError):
        world.service().save_preferences(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            payload={"enabled": True, "cheque_lead_days": [99]},
        )
    # Nothing stored on a bad payload (fail-closed).
    assert world.reminders_repo.get_preferences(installation_id=INSTALLATION_ID) is None


def test_query_defaults_when_never_configured(world: ReminderWorld) -> None:
    prefs = world.query_service().get_preferences(
        tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    )
    assert prefs.enabled is False
    assert prefs.cheque_lead_days == (7, 3)


# ---------------------------------------------------------------------------
# Daily job: nothing configured / disabled
# ---------------------------------------------------------------------------


def test_job_with_no_enabled_installations_scans_nothing(world: ReminderWorld) -> None:
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.installations_scanned == 0
    assert report.reminders_due == 0


def test_job_with_disabled_preferences_sends_nothing(world: ReminderWorld) -> None:
    world.service().save_preferences(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        payload={"enabled": False},
    )
    world.cheques.create(_make_cheque("c1", due=TODAY + datetime.timedelta(days=3)))
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.installations_scanned == 0
    assert world.reminders_repo.log == []


# ---------------------------------------------------------------------------
# Cheque due-soon
# ---------------------------------------------------------------------------


def _seed_cheque_world(
    world: ReminderWorld, *, due_days: int = 3, opt_in: bool = True
) -> None:
    world.sms_world.seed()
    world.sms_world.make_contract()
    if opt_in:
        world.sms_world.opt_in()
    world.sms_world.make_plan(legs=[("leg-1", 1, TODAY, 10_000_000)])
    world.cheques.create(
        _make_cheque(
            "cheque-1",
            due=TODAY + datetime.timedelta(days=due_days),
            installment_leg_id="leg-1",
        )
    )
    world.enable_preferences()


def test_cheque_due_soon_sends_in_app_and_sms(world: ReminderWorld) -> None:
    _seed_cheque_world(world)
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.installations_scanned == 1
    assert report.reminders_due == 1
    assert report.sent_in_app == 1
    assert report.sent_sms == 1
    assert report.sent_email == 0
    assert report.skipped_email_not_configured == 1  # no email sender wired yet

    log = world.reminders_repo.log
    assert len(log) == 3
    by_channel = {entry.channel: entry for entry in log}
    assert by_channel[ReminderChannel.IN_APP].status == ReminderLogStatus.SENT
    assert by_channel[ReminderChannel.SMS].status == ReminderLogStatus.SENT
    assert by_channel[ReminderChannel.SMS].recipient == PHONE
    assert by_channel[ReminderChannel.SMS].provider_key == "simulator"
    assert by_channel[ReminderChannel.EMAIL].status == ReminderLogStatus.SKIPPED
    assert by_channel[ReminderChannel.EMAIL].error_code == "EMAIL_NOT_CONFIGURED"

    # In-app = a review item of type reminder, keyed by the idempotency key.
    items = world.review_items(ReviewItemType.REMINDER)
    assert len(items) == 1
    assert items[0].subtype == "cheque_due_soon"
    assert items[0].source_event_ref == by_channel[ReminderChannel.IN_APP].idempotency_key
    assert "۳ روز" in items[0].reason

    # The SMS went through the simulator client with the same message.
    sends = world.simulator_client().sends
    assert len(sends) == 1
    assert sends[0]["to"] == PHONE
    assert sends[0]["message"] == by_channel[ReminderChannel.SMS].message_text


def test_cheque_fires_at_7_days_too(world: ReminderWorld) -> None:
    _seed_cheque_world(world, due_days=7)
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.reminders_due == 1
    assert report.sent_sms == 1


def test_cheque_at_unconfigured_lead_does_not_fire(world: ReminderWorld) -> None:
    _seed_cheque_world(world, due_days=5)
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.reminders_due == 0


def test_cheque_without_consent_skips_sms_but_sends_in_app(world: ReminderWorld) -> None:
    _seed_cheque_world(world, opt_in=False)
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.sent_in_app == 1
    assert report.sent_sms == 0
    assert report.skipped_no_consent == 1
    sms_rows = [
        e for e in world.reminders_repo.log if e.channel == ReminderChannel.SMS
    ]
    assert len(sms_rows) == 1
    assert sms_rows[0].status == ReminderLogStatus.SKIPPED
    assert sms_rows[0].error_code == "SMS_NOT_OPTED_IN"


def test_unlinked_cheque_has_no_consent_on_file(world: ReminderWorld) -> None:
    world.sms_world.seed()
    world.cheques.create(
        _make_cheque("cheque-1", due=TODAY + datetime.timedelta(days=3))
    )
    world.enable_preferences()
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.sent_in_app == 1
    assert report.skipped_no_consent == 1
    sms_rows = [
        e for e in world.reminders_repo.log if e.channel == ReminderChannel.SMS
    ]
    assert "not linked to an installment plan" in (sms_rows[0].error_detail or "")


def test_cheque_without_phone_skips_sms(world: ReminderWorld) -> None:
    world.sms_world.seed()
    world.sms_world.make_contract()
    world.sms_world.opt_in()
    world.sms_world.make_plan(legs=[("leg-1", 1, TODAY, 10_000_000)])
    world.cheques.create(
        _make_cheque(
            "cheque-1",
            due=TODAY + datetime.timedelta(days=3),
            customer_ref="no-phone-here",
            installment_leg_id="leg-1",
        )
    )
    world.enable_preferences()
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.skipped_no_phone == 1
    sms_rows = [
        e for e in world.reminders_repo.log if e.channel == ReminderChannel.SMS
    ]
    assert sms_rows[0].error_code == "SMS_RECIPIENT_PHONE_MISSING"


def test_persian_digits_mobile_is_normalized(world: ReminderWorld) -> None:
    # Gotcha 7: Persian digits must normalize before validating.
    world.sms_world.seed()
    world.sms_world.make_contract()
    world.sms_world.opt_in()
    world.sms_world.make_plan(legs=[("leg-1", 1, TODAY, 10_000_000)])
    world.cheques.create(
        _make_cheque(
            "cheque-1",
            due=TODAY + datetime.timedelta(days=3),
            customer_ref="۰۹۱۲۳۴۵۶۷۸۹",
            installment_leg_id="leg-1",
        )
    )
    world.enable_preferences()
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.sent_sms == 1
    sms_rows = [
        e for e in world.reminders_repo.log if e.channel == ReminderChannel.SMS
    ]
    assert sms_rows[0].recipient == "09123456789"


def test_issued_cheque_gets_no_sms(world: ReminderWorld) -> None:
    world.sms_world.seed()
    world.cheques.create(
        _make_cheque(
            "cheque-1",
            due=TODAY + datetime.timedelta(days=3),
            kind=ChequeKind.ISSUED,
            state=ChequeState.ISSUED,
        )
    )
    world.enable_preferences()
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.sent_in_app == 1
    assert report.sent_sms == 0
    # No customer to remind for an issued cheque — consent is absent by design.
    assert report.skipped_no_consent == 1


def test_cheque_type_disabled_sends_nothing(world: ReminderWorld) -> None:
    _seed_cheque_world(world)
    world.service().save_preferences(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        payload={"enabled": True, "cheque_due_soon_enabled": False},
    )
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.reminders_due == 0


# ---------------------------------------------------------------------------
# Overdue installments
# ---------------------------------------------------------------------------


def _seed_installment_world(
    world: ReminderWorld, *, overdue_days: int = 1, opt_in: bool = True
) -> None:
    world.sms_world.seed()
    world.sms_world.make_contract()
    if opt_in:
        world.sms_world.opt_in()
    world.sms_world.make_plan(
        legs=[("leg-1", 1, TODAY - datetime.timedelta(days=overdue_days), 10_000_000)]
    )
    world.enable_preferences()


def test_installment_overdue_sends_all_channels(world: ReminderWorld) -> None:
    _seed_installment_world(world, overdue_days=7)
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.reminders_due == 1
    assert report.sent_in_app == 1
    assert report.sent_sms == 1
    sms_rows = [
        e for e in world.reminders_repo.log if e.channel == ReminderChannel.SMS
    ]
    assert sms_rows[0].lead_days == 7
    assert "۷ روز معوق" in sms_rows[0].message_text
    # The send is mirrored into the US-544 log (shared dedup).
    mirror = world.sms_world.sms.find_sent_log(
        installation_id=INSTALLATION_ID, leg_id="leg-1", offset_days=7
    )
    assert mirror is not None
    assert mirror.status == SmsLogStatus.SENT


def test_installment_overdue_at_offset_1_skips_sms_when_us544_sent_first(
    world: ReminderWorld,
) -> None:
    _seed_installment_world(world, overdue_days=1)
    # US-544's job already sent for this leg + offset (its log row).
    world.sms_world.enable_settings()
    world.sms_world.reminders().run_daily_reminder_job(today=TODAY)
    assert world.sms_world.sms.find_sent_log(
        installation_id=INSTALLATION_ID, leg_id="leg-1", offset_days=1
    ) is not None
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.reminders_due == 1
    assert report.sent_in_app == 1  # US-564 still adds in-app + email
    assert report.sent_sms == 0
    assert report.skipped_already_sent == 1
    # Exactly one SMS went out (US-544's), not two.
    assert len(world.simulator_client().sends) == 1


def test_installment_not_yet_overdue_does_not_fire(world: ReminderWorld) -> None:
    world.sms_world.seed()
    world.sms_world.make_contract()
    world.sms_world.opt_in()
    world.sms_world.make_plan(
        legs=[("leg-1", 1, TODAY + datetime.timedelta(days=3), 10_000_000)]
    )
    world.enable_preferences()
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.reminders_due == 0


def test_installment_bnpl_never_reminds(world: ReminderWorld) -> None:
    world.sms_world.seed()
    plan = world.sms_world.make_plan(
        legs=[("leg-1", 1, TODAY - datetime.timedelta(days=1), 10_000_000)]
    )
    # Flip the stored plan to BNPL (the World helper builds internal plans).
    stored = world.sms_world.installments.plans[plan.id]
    world.sms_world.installments.plans[plan.id] = dataclasses.replace(
        stored, kind=PlanKind.BNPL, gateway_channel_key="snapp_pay", contract_id=None
    )
    world.enable_preferences()
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.reminders_due == 0


def test_installment_pending_clearance_not_reminded(world: ReminderWorld) -> None:
    _seed_installment_world(world, overdue_days=1)
    # A cheque receipt fully covering the leg, cheque still held:
    # the cheque may still clear — no reminder.
    world.cheques.create(
        _make_cheque(
            "cheque-cover",
            due=TODAY - datetime.timedelta(days=1),
            state=ChequeState.HELD,
        )
    )
    receipt = InstallmentReceipt(
        id="receipt-1",
        plan_id="plan-544",
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        invoice_id=INVOICE_ID,
        journal_move_id=1,
        amount=MoneyIRR(10_000_000),
        received_date=_fiscal(TODAY),
        payment_method=PaymentMethod.CHEQUE,
        cheque_id="cheque-cover",
        allocations=(LegAllocation(leg_id="leg-1", amount=MoneyIRR(10_000_000)),),
    )
    world.sms_world.installments.create_receipt(receipt)
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.reminders_due == 0


def test_installment_paid_leg_not_reminded(world: ReminderWorld) -> None:
    _seed_installment_world(world, overdue_days=1)
    receipt = InstallmentReceipt(
        id="receipt-1",
        plan_id="plan-544",
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        invoice_id=INVOICE_ID,
        journal_move_id=1,
        amount=MoneyIRR(10_000_000),
        received_date=_fiscal(TODAY),
        payment_method=PaymentMethod.CASH,
        allocations=(LegAllocation(leg_id="leg-1", amount=MoneyIRR(10_000_000)),),
    )
    world.sms_world.installments.create_receipt(receipt)
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.reminders_due == 0


# ---------------------------------------------------------------------------
# Cash box
# ---------------------------------------------------------------------------


def test_cashbox_uncounted_sends_in_app_only(world: ReminderWorld) -> None:
    world.enable_preferences()
    world.cashbox.save_cash_box(
        _make_box("box-1", created=TODAY - datetime.timedelta(days=20))
    )
    world.cashbox.save_session(
        _make_session("s1", "box-1", created=TODAY - datetime.timedelta(days=8))
    )
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.reminders_due == 1
    assert report.sent_in_app == 1
    assert report.sent_sms == 0  # no external recipient for a cash-box reminder
    assert report.skipped_email_not_configured == 1
    rows = world.reminders_repo.log
    assert {e.channel for e in rows} == {ReminderChannel.IN_APP, ReminderChannel.EMAIL}
    in_app = next(e for e in rows if e.channel == ReminderChannel.IN_APP)
    assert in_app.subject_type == "cashbox"
    assert in_app.lead_days == 7
    assert "۸ روز" in in_app.message_text


def test_cashbox_recently_counted_does_not_fire(world: ReminderWorld) -> None:
    world.enable_preferences()
    world.cashbox.save_cash_box(
        _make_box("box-1", created=TODAY - datetime.timedelta(days=20))
    )
    world.cashbox.save_session(
        _make_session("s1", "box-1", created=TODAY - datetime.timedelta(days=3))
    )
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.reminders_due == 0


def test_cashbox_open_session_suppresses_reminder(world: ReminderWorld) -> None:
    world.enable_preferences()
    world.cashbox.save_cash_box(
        _make_box("box-1", created=TODAY - datetime.timedelta(days=20))
    )
    world.cashbox.save_session(
        _make_session(
            "s1",
            "box-1",
            created=TODAY - datetime.timedelta(days=30),
            status=CountSessionStatus.IN_PROGRESS,
        )
    )
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.reminders_due == 0


def test_cashbox_never_counted_uses_creation_date(world: ReminderWorld) -> None:
    world.enable_preferences()
    world.cashbox.save_cash_box(
        _make_box("box-1", created=TODAY - datetime.timedelta(days=10))
    )
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.reminders_due == 1
    in_app = next(
        e for e in world.reminders_repo.log if e.channel == ReminderChannel.IN_APP
    )
    assert "تاکنون شمارش نشده است" in in_app.message_text


def test_cashbox_inactive_box_skipped(world: ReminderWorld) -> None:
    world.enable_preferences()
    world.cashbox.save_cash_box(
        _make_box("box-1", created=TODAY - datetime.timedelta(days=30), active=False)
    )
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.reminders_due == 0


def test_cashbox_custom_threshold(world: ReminderWorld) -> None:
    world.enable_preferences(cashbox_uncounted_days=14)
    world.cashbox.save_cash_box(
        _make_box("box-1", created=TODAY - datetime.timedelta(days=20))
    )
    world.cashbox.save_session(
        _make_session("s1", "box-1", created=TODAY - datetime.timedelta(days=8))
    )
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.reminders_due == 0


# ---------------------------------------------------------------------------
# eNamad
# ---------------------------------------------------------------------------


def test_enmad_expiry_warning_at_7_days(world: ReminderWorld) -> None:
    world.enable_preferences(enmad_expiry_date="2026-11-27")
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.reminders_due == 1
    assert report.sent_in_app == 1
    assert report.sent_sms == 0  # operator reminder — no SMS recipient
    in_app = next(
        e for e in world.reminders_repo.log if e.channel == ReminderChannel.IN_APP
    )
    assert in_app.reminder_type == ReminderType.ENMAD_EXPIRY
    assert "غیرفعال می‌شود" in in_app.message_text
    assert "درگاه پرداخت" in in_app.message_text
    assert "BNPL" in in_app.message_text
    items = world.review_items(ReviewItemType.REMINDER)
    assert items[0].severity == ReviewSeverity.WARNING


def test_enmad_expiry_day_is_critical(world: ReminderWorld) -> None:
    world.enable_preferences(enmad_expiry_date="2026-11-20")
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.reminders_due == 1
    items = world.review_items(ReviewItemType.REMINDER)
    assert items[0].severity == ReviewSeverity.CRITICAL
    assert "امروز" in items[0].reason


def test_enmad_no_expiry_date_no_reminder(world: ReminderWorld) -> None:
    world.enable_preferences()
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.reminders_due == 0


def test_enmad_expired_no_reminder(world: ReminderWorld) -> None:
    world.enable_preferences(enmad_expiry_date="2026-11-19")
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.reminders_due == 0


# ---------------------------------------------------------------------------
# Quiet hours
# ---------------------------------------------------------------------------


def test_quiet_hours_suppress_sms_and_email_but_not_in_app(world: ReminderWorld) -> None:
    _seed_cheque_world(world)
    report = world.service().run_daily_reminder_job(today=TODAY, now=QUIET_NOW)
    assert report.sent_in_app == 1
    assert report.sent_sms == 0
    assert report.skipped_quiet_hours == 1  # SMS only — email has no sender anyway
    sms_rows = [
        e for e in world.reminders_repo.log if e.channel == ReminderChannel.SMS
    ]
    assert sms_rows[0].status == ReminderLogStatus.SKIPPED
    assert sms_rows[0].error_code == "QUIET_HOURS"
    assert world.simulator_client().sends == []


def test_quiet_hours_disabled_sends_at_night(world: ReminderWorld) -> None:
    _seed_cheque_world(world)
    world.service().save_preferences(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        payload={"enabled": True, "quiet_hours_enabled": False},
    )
    report = world.service().run_daily_reminder_job(today=TODAY, now=QUIET_NOW)
    assert report.sent_sms == 1


def test_quiet_hours_suppress_email_when_sender_wired(world: ReminderWorld) -> None:
    world.email = FakeEmailSender()
    _seed_cheque_world(world)
    world.service().save_preferences(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        payload={"enabled": True, "notification_email": "ops@example.com"},
    )
    report = world.service().run_daily_reminder_job(today=TODAY, now=QUIET_NOW)
    assert report.skipped_quiet_hours == 2  # SMS + email
    assert world.email.sends == []


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_second_run_sends_nothing_new(world: ReminderWorld) -> None:
    _seed_cheque_world(world)
    first = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert first.sent_in_app == 1
    assert first.sent_sms == 1
    second = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert second.sent_in_app == 0
    assert second.sent_sms == 0
    assert second.skipped_already_sent == 2  # in-app + SMS; email re-skips (not sent)
    assert len(world.simulator_client().sends) == 1
    assert len(world.review_items(ReviewItemType.REMINDER)) == 1


def test_failed_send_retries_next_run(world: ReminderWorld) -> None:
    _seed_cheque_world(world)
    # The fake factory creates the simulator client lazily on the
    # first send, so the raw dict has no "simulator" key yet —
    # simulator_client() creates it (the send path reuses the same
    # instance from the dict).
    client = world.simulator_client()
    client.error = SmsProviderRejectedError("simulator", reason="no credit.")
    first = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert first.failed == 1
    assert first.sent_sms == 0
    client.error = None
    second = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert second.sent_sms == 1
    assert len(world.simulator_client().sends) == 1


def test_cashbox_reminder_fires_once_per_uncounted_period(world: ReminderWorld) -> None:
    world.enable_preferences()
    world.cashbox.save_cash_box(
        _make_box("box-1", created=TODAY - datetime.timedelta(days=20))
    )
    world.cashbox.save_session(
        _make_session("s1", "box-1", created=TODAY - datetime.timedelta(days=8))
    )
    first = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert first.sent_in_app == 1
    # The next day the box is still uncounted — but the same uncounted
    # period must not remind again.
    second = world.service().run_daily_reminder_job(
        today=TODAY + datetime.timedelta(days=1), now=NOW + datetime.timedelta(days=1)
    )
    assert second.sent_in_app == 0
    assert second.skipped_already_sent == 1


def test_cashbox_reminder_fires_again_after_a_new_count(world: ReminderWorld) -> None:
    world.enable_preferences()
    world.cashbox.save_cash_box(
        _make_box("box-1", created=TODAY - datetime.timedelta(days=20))
    )
    world.cashbox.save_session(
        _make_session("s1", "box-1", created=TODAY - datetime.timedelta(days=8))
    )
    first = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert first.sent_in_app == 1
    # The box is counted (a new archived session), then goes uncounted again.
    world.cashbox.save_session(
        _make_session("s2", "box-1", created=TODAY + datetime.timedelta(days=1))
    )
    later = TODAY + datetime.timedelta(days=10)
    second = world.service().run_daily_reminder_job(
        today=later, now=datetime.datetime.combine(later, datetime.time(8, 30))
    )
    assert second.sent_in_app == 1


# ---------------------------------------------------------------------------
# Cost recording + provider failures
# ---------------------------------------------------------------------------


def test_sms_cost_is_recorded_from_the_provider_result(world: ReminderWorld) -> None:
    _seed_cheque_world(world)
    client = world.simulator_client()
    client.result = SmsSendResult(
        provider_message_id="sim-cost-1",
        status=SmsDeliveryStatus.SENT,
        provider_status_raw="simulated",
        cost_toman=Decimal("150"),
        cost_raw="1500",
        cost_unit="IRR",
    )
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.sent_sms == 1
    sms_rows = [
        e for e in world.reminders_repo.log if e.channel == ReminderChannel.SMS
    ]
    assert sms_rows[0].cost_toman == Decimal("150")
    assert sms_rows[0].provider_cost_raw == "1500"
    assert sms_rows[0].provider_cost_unit == "IRR"
    assert sms_rows[0].provider_message_id == "sim-cost-1"


def test_sms_cost_absent_when_provider_reports_none(world: ReminderWorld) -> None:
    _seed_cheque_world(world)
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.sent_sms == 1
    sms_rows = [
        e for e in world.reminders_repo.log if e.channel == ReminderChannel.SMS
    ]
    assert sms_rows[0].cost_toman is None
    assert sms_rows[0].provider_cost_raw is None


def test_sms_provider_failure_logs_failed_and_files_review_item(
    world: ReminderWorld,
) -> None:
    _seed_cheque_world(world)
    world.simulator_client().error = SmsProviderRejectedError(
        "simulator", reason="no credit."
    )
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.failed == 1
    sms_rows = [
        e for e in world.reminders_repo.log if e.channel == ReminderChannel.SMS
    ]
    assert sms_rows[0].status == ReminderLogStatus.FAILED
    assert sms_rows[0].error_code == "SMS_PROVIDER_REJECTED"
    failures = world.review_items(ReviewItemType.SMS_DELIVERY_FAILED)
    assert len(failures) == 1
    assert failures[0].subtype == "reminder_sms_failed"


def test_sms_provider_reported_failed_is_a_failure(world: ReminderWorld) -> None:
    _seed_cheque_world(world)
    client = world.simulator_client()
    client.result = SmsSendResult(
        provider_message_id="sim-fail-1",
        status=SmsDeliveryStatus.FAILED,
        provider_status_raw="rejected",
    )
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.failed == 1
    sms_rows = [
        e for e in world.reminders_repo.log if e.channel == ReminderChannel.SMS
    ]
    assert sms_rows[0].status == ReminderLogStatus.FAILED
    assert sms_rows[0].error_code == "SMS_PROVIDER_REJECTED"


def test_no_default_provider_skips_sms(world: ReminderWorld) -> None:
    # No seed: no providers at all.
    world.sms_world.make_contract()
    world.sms_world.opt_in()
    world.sms_world.make_plan(legs=[("leg-1", 1, TODAY, 10_000_000)])
    world.cheques.create(
        _make_cheque(
            "cheque-1",
            due=TODAY + datetime.timedelta(days=3),
            installment_leg_id="leg-1",
        )
    )
    world.enable_preferences()
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.sent_in_app == 1
    assert report.skipped_no_provider == 1
    sms_rows = [
        e for e in world.reminders_repo.log if e.channel == ReminderChannel.SMS
    ]
    assert sms_rows[0].error_code == "SMS_NO_PROVIDER_AVAILABLE"


def test_live_provider_without_credentials_skips_and_files_one_item(
    world: ReminderWorld,
) -> None:
    world.sms_world.seed()
    # Make Kavenegar the default (born disabled, no credentials) and
    # un-default the simulator, so the default cannot send.
    sms_repo = world.sms_world.sms
    kavenegar = sms_repo.get_provider_by_key("kavenegar", installation_id=INSTALLATION_ID)
    assert kavenegar is not None
    simulator = sms_repo.get_provider_by_key("simulator", installation_id=INSTALLATION_ID)
    assert simulator is not None
    sms_repo.update_provider(
        UpdateSmsProvider(
            provider_id=simulator.id,
            installation_id=INSTALLATION_ID,
            is_default=False,
        )
    )
    sms_repo.update_provider(
        UpdateSmsProvider(
            provider_id=kavenegar.id,
            installation_id=INSTALLATION_ID,
            enabled=True,
            is_default=True,
        )
    )
    _seed_cheque_world(world)
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.skipped_no_provider == 1
    sms_rows = [
        e for e in world.reminders_repo.log if e.channel == ReminderChannel.SMS
    ]
    assert sms_rows[0].error_code == "SMS_PROVIDER_NOT_CONFIGURED"
    # One review item for the installation, not one per reminder.
    items = world.review_items(ReviewItemType.SMS_DELIVERY_FAILED)
    assert len(items) == 1
    assert items[0].subtype == "sms_provider_not_configured"
    # A second run files no second item (deduped against the open one).
    world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert len(world.review_items(ReviewItemType.SMS_DELIVERY_FAILED)) == 1


# ---------------------------------------------------------------------------
# Email channel (sender wired in tests only)
# ---------------------------------------------------------------------------


def test_email_sends_when_sender_and_recipient_configured(world: ReminderWorld) -> None:
    world.email = FakeEmailSender()
    _seed_cheque_world(world)
    world.service().save_preferences(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        payload={"enabled": True, "notification_email": "ops@example.com"},
    )
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.sent_email == 1
    assert len(world.email.sends) == 1
    assert world.email.sends[0]["to"] == "ops@example.com"
    email_rows = [
        e for e in world.reminders_repo.log if e.channel == ReminderChannel.EMAIL
    ]
    assert email_rows[0].status == ReminderLogStatus.SENT
    assert email_rows[0].recipient == "ops@example.com"


def test_email_without_recipient_skips(world: ReminderWorld) -> None:
    world.email = FakeEmailSender()
    _seed_cheque_world(world)
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.sent_email == 0
    email_rows = [
        e for e in world.reminders_repo.log if e.channel == ReminderChannel.EMAIL
    ]
    assert email_rows[0].error_code == "EMAIL_RECIPIENT_MISSING"


def test_email_send_failure_is_logged(world: ReminderWorld) -> None:
    world.email = FakeEmailSender(error=RuntimeError("SMTP down"))
    _seed_cheque_world(world)
    world.service().save_preferences(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        payload={"enabled": True, "notification_email": "ops@example.com"},
    )
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.failed == 1
    email_rows = [
        e for e in world.reminders_repo.log if e.channel == ReminderChannel.EMAIL
    ]
    assert email_rows[0].status == ReminderLogStatus.FAILED
    assert email_rows[0].error_code == "EMAIL_SEND_FAILED"


# ---------------------------------------------------------------------------
# Channels + review wiring
# ---------------------------------------------------------------------------


def test_disabled_channels_are_not_attempted(world: ReminderWorld) -> None:
    _seed_cheque_world(world)
    world.service().save_preferences(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        payload={
            "enabled": True,
            "channel_in_app": True,
            "channel_email": False,
            "channel_sms": False,
        },
    )
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.sent_in_app == 1
    assert report.sent_sms == 0
    assert len(world.reminders_repo.log) == 1


def test_job_without_review_service_still_sends_sms(world: ReminderWorld) -> None:
    _seed_cheque_world(world)
    report = world.service(review=False).run_daily_reminder_job(today=TODAY, now=NOW)
    assert report.sent_sms == 1
    in_app_rows = [
        e for e in world.reminders_repo.log if e.channel == ReminderChannel.IN_APP
    ]
    assert in_app_rows[0].status == ReminderLogStatus.SKIPPED
    assert in_app_rows[0].error_code == "REVIEW_QUEUE_UNAVAILABLE"


def test_report_summary_is_json_serializable(world: ReminderWorld) -> None:
    import json

    _seed_cheque_world(world)
    report = world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    json.dumps(report.summary())
