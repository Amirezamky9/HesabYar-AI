"""Contract tests for US-544 (Phase 5-5 / ۵ج): SMS provider registry + reminders.

The REAL stack (SmsRegistryService + SmsQueryService +
SmsReminderService + SqlAlchemySmsRepository) against the LIVE
HesabYar PostgreSQL database. No live SMS call is ever made —
the simulator client (and the fake client factory) covers every
send. These tests need only the HesabYar DB (migration 0031's
tables, created by ``create_all`` in the fixture when missing).

Each test uses a FRESH tenant + installation id and UNIQUE
provider keys / invoice ids (the registry unique index is
per-installation, but unique ids keep runs independent on the
shared dev DB). Teardown deletes the test's rows (reminder log →
contract settings → settings → providers → installment rows →
review items).

Configuration (all optional):
  HESABYAR_DATABASE_URL (the HesabYar DB; CI sets it, local runs
                         source hesabyar-reference/.env.local-db)

When the DB is not reachable, every test SKIPS cleanly with a clear
message.
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import Generator
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.application.commands.installments import InstallmentCommandService
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.commands.sms import SmsRegistryService, SmsReminderService
from hesabyar.application.queries.sms import SmsQueryService
from hesabyar.domain.review_queue.models import (
    ReviewItemType,
    ReviewSeverity,
)
from hesabyar.domain.sms.errors import SmsReminderAlreadySentError
from hesabyar.domain.sms.models import (
    SmsLogStatus,
    SmsReminderLogEntry,
    SmsReminderTrigger,
    new_log_id,
)
from hesabyar.infrastructure.persistence.postgres.installments_repository import (
    SqlAlchemyInstallmentRepository,
)
from hesabyar.infrastructure.persistence.postgres.models import (
    Base,
    InstallmentLegModel,
    InstallmentPlanModel,
    InstallmentReceiptAllocationModel,
    InstallmentReceiptModel,
    InstallmentSaleContractModel,
    ReviewItemAuditModel,
    ReviewItemModel,
    SmsContractReminderSettingsModel,
    SmsProviderModel,
    SmsReminderLogModel,
    SmsReminderSettingsModel,
)
from hesabyar.infrastructure.persistence.postgres.review_queue_repository import (
    SqlAlchemyReviewQueueRepository,
)
from hesabyar.infrastructure.persistence.postgres.sms_repository import (
    SqlAlchemySmsRepository,
)
from tests.unit.test_sms_services import FakeSmsClient, FakeSmsClientFactory, FakeVault

HESABYAR_DB_URL = os.environ.get("HESABYAR_DATABASE_URL", "")

COMPANY_ENGINE_ID = 544
PHONE = "09123456789"

#: Far-past Jalali first due date (1400-01-01 = 2021-03-21): every
#: leg is overdue on any run date, so the manual trigger's offset
#: is deterministically positive.
PAST_JALALI = "1400-01-01"


def _unique_suffix() -> str:
    return uuid.uuid4().hex[:12]


def _tenant_id() -> str:
    return str(uuid.uuid4())


def _installation_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture(scope="module")
def hesabyar_engine() -> Generator[Engine, None, None]:
    if not HESABYAR_DB_URL:
        pytest.skip("HESABYAR_DATABASE_URL is not set.")
    eng = create_engine(HESABYAR_DB_URL, echo=False)
    try:
        with eng.connect():
            pass
    except Exception:
        eng.dispose()
        pytest.skip("HesabYar DB is not reachable.")
    # Creates missing tables only — never alters existing ones.
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(hesabyar_engine: Engine) -> Generator[Session, None, None]:
    factory = sessionmaker(bind=hesabyar_engine)
    sess = factory()
    try:
        yield sess
    finally:
        sess.rollback()
        sess.close()


@pytest.fixture()
def tenant() -> str:
    return _tenant_id()


@pytest.fixture()
def installation() -> str:
    return _installation_id()


@pytest.fixture()
def services(
    session: Session,
) -> tuple[SmsRegistryService, SmsQueryService, SmsReminderService, InstallmentCommandService]:
    sms_repo = SqlAlchemySmsRepository(session)
    installment_repo = SqlAlchemyInstallmentRepository(session)
    review_repo = SqlAlchemyReviewQueueRepository(session)
    review = ReviewQueueCommandService(review_repo, review_repo)
    clients = FakeSmsClientFactory(
        {"simulator": FakeSmsClient(provider_kind="simulator")}
    )
    return (
        SmsRegistryService(sms_repo, installment_repo, vault=FakeVault()),
        SmsQueryService(sms_repo),
        SmsReminderService(
            sms_repo, installment_repo, client_factory=clients, vault=FakeVault(), review=review
        ),
        InstallmentCommandService(installment_repo),
    )


@pytest.fixture()
def _cleanup(
    hesabyar_engine: Engine, installation: str, tenant: str
) -> Generator[None, None, None]:
    yield
    # A FRESH session for cleanup (the US-533 pattern): the test's
    # own session may already be closed by teardown time, and the
    # deletes must not depend on fixture teardown order. Reverse FK
    # order; scoped to this test's installation / tenant.
    cleanup = sessionmaker(bind=hesabyar_engine)()
    try:
        cleanup.execute(
            delete(SmsReminderLogModel).where(
                SmsReminderLogModel.installation_id == installation
            )
        )
        cleanup.execute(
            delete(SmsContractReminderSettingsModel).where(
                SmsContractReminderSettingsModel.installation_id == installation
            )
        )
        cleanup.execute(
            delete(SmsReminderSettingsModel).where(
                SmsReminderSettingsModel.installation_id == installation
            )
        )
        cleanup.execute(
            delete(SmsProviderModel).where(
                SmsProviderModel.installation_id == installation
            )
        )
        cleanup.execute(
            delete(InstallmentReceiptAllocationModel).where(
                InstallmentReceiptAllocationModel.receipt_id.in_(
                    select(InstallmentReceiptModel.id).where(
                        InstallmentReceiptModel.installation_id == installation
                    )
                )
            )
        )
        cleanup.execute(
            delete(InstallmentReceiptModel).where(
                InstallmentReceiptModel.installation_id == installation
            )
        )
        cleanup.execute(
            delete(InstallmentLegModel).where(
                InstallmentLegModel.plan_id.in_(
                    select(InstallmentPlanModel.id).where(
                        InstallmentPlanModel.installation_id == installation
                    )
                )
            )
        )
        cleanup.execute(
            delete(InstallmentPlanModel).where(
                InstallmentPlanModel.installation_id == installation
            )
        )
        cleanup.execute(
            delete(InstallmentSaleContractModel).where(
                InstallmentSaleContractModel.installation_id == installation
            )
        )
        cleanup.execute(
            delete(ReviewItemAuditModel).where(ReviewItemAuditModel.tenant_id == tenant)
        )
        cleanup.execute(
            delete(ReviewItemModel).where(ReviewItemModel.tenant_id == tenant)
        )
        cleanup.commit()
    finally:
        cleanup.close()


def _provider_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "provider_key": f"custom-{_unique_suffix()}",
        "display_name_fa": "ارائه‌دهندهٔ آزمون",
        "provider_kind": "custom",
    }
    payload.update(overrides)
    return payload


def _plan_payload(invoice_id: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "invoice_id": invoice_id,
        "total_principal_toman": 3_000_000,
        "leg_count": 2,
        "interval_days": 30,
        "first_due_date": PAST_JALALI,
        "customer_ref": PHONE,
    }
    payload.update(overrides)
    return payload


def _contract_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "code": f"C-{_unique_suffix()}",
        "name": "قرارداد آزمون",
        "version": 1,
        "effective_from": "1405-08-01",
    }
    payload.update(overrides)
    return payload


# -- seed + provider CRUD -------------------------------------------------------


@pytest.mark.usefixtures("_cleanup")
def test_seed_is_idempotent_through_real_db(
    session: Session,
    services: tuple[SmsRegistryService, SmsQueryService, SmsReminderService, InstallmentCommandService],
    installation: str,
) -> None:
    registry, queries, _reminders, _installments = services
    first = registry.seed_builtin_providers(installation)
    session.commit()
    assert [p.provider_key for p in first] == ["kavenegar", "sms_ir", "simulator"]
    second = registry.seed_builtin_providers(installation)
    session.commit()
    assert [p.id for p in first] == [p.id for p in second]
    assert len(queries.list_providers(installation_id=installation)) == 3
    default = queries.get_default_provider(installation_id=installation)
    assert default is not None
    assert default.provider_key == "simulator"


@pytest.mark.usefixtures("_cleanup")
def test_provider_crud_round_trip_through_real_db(
    session: Session,
    services: tuple[SmsRegistryService, SmsQueryService, SmsReminderService, InstallmentCommandService],
    installation: str,
) -> None:
    registry, queries, _reminders, _installments = services
    registry.seed_builtin_providers(installation)
    session.commit()
    created = registry.create_provider(
        installation_id=installation,
        payload=_provider_payload(
            price_per_sms_toman="125.5",
            rate_limit_per_minute=60,
            line_type="service",
            contract_required=True,
        ),
    )
    session.commit()
    assert created.price_per_sms_toman == Decimal("125.5")
    assert created.rate_limit_per_minute == 60

    fetched = queries.get_provider(created.id, installation_id=installation)
    assert fetched.display_name_fa == "ارائه‌دهندهٔ آزمون"

    updated = registry.update_provider(
        installation_id=installation,
        provider_id=created.id,
        payload={"enabled": True, "is_default": True, "notes": "یادداشت"},
    )
    session.commit()
    assert updated.enabled is True
    assert updated.is_default is True
    # The previous default (simulator) was cleared.
    simulator = queries.get_default_provider(installation_id=installation)
    assert simulator is not None
    assert simulator.id == created.id

    registry.delete_provider(installation_id=installation, provider_id=created.id)
    session.commit()
    assert queries.get_provider(created.id, installation_id=installation) is None


@pytest.mark.usefixtures("_cleanup")
def test_credentials_round_trip_through_real_db(
    session: Session,
    services: tuple[SmsRegistryService, SmsQueryService, SmsReminderService, InstallmentCommandService],
    installation: str,
) -> None:
    registry, _queries, _reminders, _installments = services
    created = registry.create_provider(
        installation_id=installation,
        payload=_provider_payload(
            provider_kind="kavenegar",
            credentials={"api_key": "live-key-544"},
        ),
    )
    session.commit()
    assert created.has_credentials is True
    # The ciphertext in the DB is not the plaintext.
    row = session.execute(
        select(SmsProviderModel).where(SmsProviderModel.id == created.id)
    ).scalar_one()
    assert row.credentials_ciphertext is not None
    assert "live-key-544" not in row.credentials_ciphertext
    assert FakeVault().decrypt(row.credentials_ciphertext) == '{"api_key": "live-key-544"}'


# -- settings + opt-ins ---------------------------------------------------------


@pytest.mark.usefixtures("_cleanup")
def test_settings_and_contract_opt_in_round_trip(
    session: Session,
    services: tuple[SmsRegistryService, SmsQueryService, SmsReminderService, InstallmentCommandService],
    tenant: str,
    installation: str,
) -> None:
    registry, queries, _reminders, installments = services
    contract = installments.create_contract(
        tenant_id=tenant,
        installation_id=installation,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_contract_payload(),
    )
    session.commit()

    settings = registry.save_reminder_settings(
        tenant_id=tenant,
        installation_id=installation,
        payload={
            "enabled": True,
            "offsets_days_before": [3, 0],
            "offsets_days_overdue": [1],
            "template_before_due": "مانده {amount_toman} تومان",
            "template_overdue": "معوق {amount_toman} تومان",
        },
    )
    session.commit()
    assert settings.enabled is True
    assert queries.get_reminder_settings(
        tenant_id=tenant, installation_id=installation
    ) == settings

    opt_in = registry.save_contract_settings(
        tenant_id=tenant,
        installation_id=installation,
        contract_id=contract.id,
        payload={"reminders_enabled": True},
    )
    session.commit()
    assert opt_in.reminders_enabled is True
    assert queries.get_contract_settings(
        installation_id=installation, contract_id=contract.id
    ) == opt_in


# -- reminder log: dedup + filters ----------------------------------------------


@pytest.mark.usefixtures("_cleanup")
def test_log_dedup_index_rejects_second_sent_row(
    session: Session,
    services: tuple[SmsRegistryService, SmsQueryService, SmsReminderService, InstallmentCommandService],
    tenant: str,
    installation: str,
) -> None:
    registry, queries, _reminders, installments = services
    registry.seed_builtin_providers(installation)
    plan = installments.create_plan(
        tenant_id=tenant,
        installation_id=installation,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_plan_payload(f"INV-US544-{_unique_suffix()}"),
    )
    session.commit()
    legs = SqlAlchemyInstallmentRepository(session).list_legs(
        plan.id, tenant_id=tenant, installation_id=installation
    )
    assert len(legs) == 2
    simulator = queries.get_default_provider(installation_id=installation)
    assert simulator is not None

    repo = SqlAlchemySmsRepository(session)
    first = SmsReminderLogEntry(
        id=new_log_id(),
        tenant_id=tenant,
        installation_id=installation,
        plan_id=plan.id,
        leg_id=legs[0].id,
        provider_id=simulator.id,
        provider_key="simulator",
        recipient_phone=PHONE,
        message_text="یادآوری",
        offset_days=0,
        trigger=SmsReminderTrigger.MANUAL,
        status=SmsLogStatus.SENT,
    )
    repo.insert_log(first)
    session.commit()
    assert repo.find_sent_log(
        installation_id=installation, leg_id=legs[0].id, offset_days=0
    ) is not None

    # A second SENT row for the same leg + offset violates the dedup index.
    duplicate = SmsReminderLogEntry(
        id=new_log_id(),
        tenant_id=tenant,
        installation_id=installation,
        plan_id=plan.id,
        leg_id=legs[0].id,
        provider_id=simulator.id,
        provider_key="simulator",
        recipient_phone=PHONE,
        message_text="یادآوری دوباره",
        offset_days=0,
        trigger=SmsReminderTrigger.SCHEDULED,
        status=SmsLogStatus.SENT,
    )
    with pytest.raises(SmsReminderAlreadySentError):
        repo.insert_log(duplicate)
    session.rollback()

    # ...but a FAILED row for the same leg + offset is fine (retryable),
    # and so is a SENT row for a DIFFERENT offset.
    failed = SmsReminderLogEntry(
        id=new_log_id(),
        tenant_id=tenant,
        installation_id=installation,
        plan_id=plan.id,
        leg_id=legs[0].id,
        provider_id=simulator.id,
        provider_key="simulator",
        recipient_phone=PHONE,
        message_text="یادآوری ناموفق",
        offset_days=0,
        trigger=SmsReminderTrigger.MANUAL,
        status=SmsLogStatus.FAILED,
        error_code="SMS_PROVIDER_REJECTED",
    )
    repo.insert_log(failed)
    other_offset = SmsReminderLogEntry(
        id=new_log_id(),
        tenant_id=tenant,
        installation_id=installation,
        plan_id=plan.id,
        leg_id=legs[0].id,
        provider_id=simulator.id,
        provider_key="simulator",
        recipient_phone=PHONE,
        message_text="یادآوری",
        offset_days=1,
        trigger=SmsReminderTrigger.SCHEDULED,
        status=SmsLogStatus.SENT,
    )
    repo.insert_log(other_offset)
    session.commit()

    assert len(repo.list_log(installation_id=installation)) == 3
    assert len(repo.list_log(installation_id=installation, status=SmsLogStatus.FAILED)) == 1
    assert len(
        repo.list_log(
            installation_id=installation, trigger=SmsReminderTrigger.SCHEDULED
        )
    ) == 1
    assert len(repo.list_log(installation_id=installation, plan_id=plan.id)) == 3
    assert len(repo.list_log(installation_id=installation, provider_key="kavenegar")) == 0


# -- manual trigger end-to-end ---------------------------------------------------


@pytest.mark.usefixtures("_cleanup")
def test_manual_trigger_end_to_end_through_real_db(
    session: Session,
    services: tuple[SmsRegistryService, SmsQueryService, SmsReminderService, InstallmentCommandService],
    tenant: str,
    installation: str,
) -> None:
    registry, queries, reminders, installments = services
    registry.seed_builtin_providers(installation)
    plan = installments.create_plan(
        tenant_id=tenant,
        installation_id=installation,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_plan_payload(f"INV-US544-{_unique_suffix()}"),
    )
    session.commit()
    entry = reminders.send_manual_reminder(
        tenant_id=tenant,
        installation_id=installation,
        plan_id=plan.id,
        today=datetime.date(2026, 11, 20),
    )
    session.commit()
    assert entry.status == SmsLogStatus.SENT
    assert entry.provider_key == "simulator"
    assert entry.offset_days > 0  # far-past due date
    assert "۱۴۰۰/۰۱/۰۱" in entry.message_text
    assert "معوق" in entry.message_text
    rows = queries.list_log(installation_id=installation, plan_id=plan.id)
    assert len(rows) == 1
    assert rows[0].id == entry.id


# -- tenth review-queue type ------------------------------------------------------


@pytest.mark.usefixtures("_cleanup")
def test_sms_delivery_failed_review_item_persists(
    session: Session,
    services: tuple[SmsRegistryService, SmsQueryService, SmsReminderService, InstallmentCommandService],
    tenant: str,
) -> None:
    _registry, _queries, _reminders, _installments = services
    review_repo = SqlAlchemyReviewQueueRepository(session)
    review = ReviewQueueCommandService(review_repo, review_repo)
    item = review.file_item(
        tenant_id=tenant,
        item_type=ReviewItemType.SMS_DELIVERY_FAILED,
        severity=ReviewSeverity.WARNING,
        reason="SMS reminder failed for installment leg L (plan P).",
        suggested_action="Check the provider and re-send.",
        company_id=None,
        subtype="sms_reminder_failed",
        channel="simulator",
        source_event_ref="installment_leg:L",
        amount_irr=10_000_000,
        actor="test",
    )
    session.commit()
    row = session.execute(
        select(ReviewItemModel).where(ReviewItemModel.id == item.id)
    ).scalar_one()
    assert row.item_type == "sms_delivery_failed"
    assert row.severity == "warning"
