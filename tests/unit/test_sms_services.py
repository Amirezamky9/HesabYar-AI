"""Unit tests for the SMS application services (US-544).

Registry service + reminder service (manual trigger + daily job)
over in-memory fakes. Reuses the installment fake repository and
the review-queue fakes from the sibling test modules. "Today" is
pinned explicitly (2026-11-20 = 1405/08/29). No live SMS call is
ever made from a test — the fake clients return canned results.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.commands.sms import SmsRegistryService, SmsReminderService
from hesabyar.application.queries.sms import SmsQueryService
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.installments.errors import (
    InstallmentContractNotFoundError,
    InstallmentPlanNotFoundError,
)
from hesabyar.domain.installments.models import (
    InstallmentLeg,
    InstallmentPlan,
    InstallmentReceipt,
    InstallmentSaleContract,
    LegAllocation,
    PaymentMethod,
)
from hesabyar.domain.review_queue.models import ReviewItemType, ReviewSeverity
from hesabyar.domain.sms.commands import (
    UNSET,
    CreateSmsProvider,
    RotateSmsProviderCredentials,
    UpdateSmsProvider,
)
from hesabyar.domain.sms.errors import (
    SmsLegNotFoundError,
    SmsLegNotRemindableError,
    SmsNoProviderAvailableError,
    SmsNoUnpaidLegError,
    SmsProviderClientUnavailableError,
    SmsProviderInUseError,
    SmsProviderKeyConflictError,
    SmsProviderNotConfiguredError,
    SmsProviderNotEnabledError,
    SmsProviderNotFoundError,
    SmsProviderRejectedError,
    SmsRecipientPhoneMissingError,
    SmsReminderAlreadySentError,
    SmsValidationError,
    SmsVaultUnavailableError,
)
from hesabyar.domain.sms.models import (
    SENT_LOG_STATUSES,
    SmsContractReminderSettings,
    SmsDeliveryStatus,
    SmsLogStatus,
    SmsProvider,
    SmsProviderStatus,
    SmsReminderLogEntry,
    SmsReminderSettings,
    SmsReminderTrigger,
    new_contract_settings_id,
    new_provider_id,
)
from hesabyar.domain.sms.ports import SmsDeliveryUpdate, SmsSendResult
from tests.unit.test_installment_services import (
    COMPANY_ENGINE_ID,
    INSTALLATION_ID,
    TENANT_ID,
    FakeInstallmentRepository,
)
from tests.unit.test_review_queue_services import (
    NOW as REVIEW_NOW,
)
from tests.unit.test_review_queue_services import (
    InMemoryReviewAudit,
    InMemoryReviewQueue,
)

TODAY = datetime.date(2026, 11, 20)  # 1405/08/29
PHONE = "09123456789"
INVOICE_ID = "INV-544-001"


def _fiscal(day: datetime.date) -> FiscalDate:
    return FiscalDate.from_gregorian(day)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSmsRepository:
    """In-memory SMS repository (dedup semantics mirror the SQL one)."""

    def __init__(self) -> None:
        self.providers: dict[str, SmsProvider] = {}
        self.ciphertexts: dict[str, str] = {}
        self.settings: dict[str, SmsReminderSettings] = {}
        self.contract_settings: dict[tuple[str, str], SmsContractReminderSettings] = {}
        self.log: list[SmsReminderLogEntry] = []
        self._clock = datetime.datetime(2026, 11, 20, 12, 0, tzinfo=datetime.UTC)

    def _next_timestamp(self) -> datetime.datetime:
        self._clock += datetime.timedelta(seconds=1)
        return self._clock

    # -- providers -----------------------------------------------------

    def create_provider(self, command: CreateSmsProvider) -> SmsProvider:
        provider = SmsProvider(
            id=new_provider_id(),
            installation_id=command.installation_id,
            provider_key=command.provider_key,
            display_name_fa=command.display_name_fa,
            provider_kind=command.provider_kind,
            is_builtin=command.is_builtin,
            enabled=command.enabled,
            is_default=command.is_default,
            status=command.status,
            line_number=command.line_number,
            price_per_sms_toman=command.price_per_sms_toman,
            rate_limit_per_minute=command.rate_limit_per_minute,
            line_type=command.line_type,
            contract_required=command.contract_required,
            enmad_required=command.enmad_required,
            notes=command.notes,
            has_credentials=False,
            created_at=self._next_timestamp(),
            updated_at=self._next_timestamp(),
        )
        self.providers[provider.id] = provider
        return provider

    def get_provider(self, provider_id: str, *, installation_id: str) -> SmsProvider | None:
        provider = self.providers.get(provider_id)
        if provider is None or provider.installation_id != installation_id:
            return None
        return provider

    def get_provider_by_key(self, provider_key: str, *, installation_id: str) -> SmsProvider | None:
        for provider in self.providers.values():
            if provider.installation_id == installation_id and provider.provider_key == provider_key:
                return provider
        return None

    def get_default_provider(self, *, installation_id: str) -> SmsProvider | None:
        for provider in self.providers.values():
            if provider.installation_id == installation_id and provider.is_default:
                return provider
        return None

    def list_providers(self, *, installation_id: str) -> list[SmsProvider]:
        found = [p for p in self.providers.values() if p.installation_id == installation_id]
        found.sort(key=lambda p: (not p.is_builtin, p.provider_key))
        return found

    def update_provider(self, command: UpdateSmsProvider) -> SmsProvider | None:
        provider = self.get_provider(command.provider_id, installation_id=command.installation_id)
        if provider is None:
            return None
        changes: dict[str, object] = {}
        if command.display_name_fa is not None:
            changes["display_name_fa"] = command.display_name_fa
        if command.enabled is not None:
            changes["enabled"] = command.enabled
        if command.is_default is not None:
            changes["is_default"] = command.is_default
        if command.status is not None:
            changes["status"] = command.status
        if command.line_number is not UNSET:
            changes["line_number"] = command.line_number
        if command.price_per_sms_toman is not UNSET:
            changes["price_per_sms_toman"] = command.price_per_sms_toman
        if command.rate_limit_per_minute is not UNSET:
            changes["rate_limit_per_minute"] = command.rate_limit_per_minute
        if command.line_type is not UNSET:
            changes["line_type"] = command.line_type
        if command.contract_required is not None:
            changes["contract_required"] = command.contract_required
        if command.enmad_required is not None:
            changes["enmad_required"] = command.enmad_required
        if command.notes is not UNSET:
            changes["notes"] = command.notes
        updated = dataclasses.replace(provider, **changes, updated_at=self._next_timestamp())
        self.providers[updated.id] = updated
        return updated

    def delete_provider(self, provider_id: str, *, installation_id: str) -> bool:
        if self.get_provider(provider_id, installation_id=installation_id) is None:
            return False
        del self.providers[provider_id]
        self.ciphertexts.pop(provider_id, None)
        return True

    def rotate_provider_credentials(
        self, command: RotateSmsProviderCredentials
    ) -> SmsProvider | None:
        provider = self.get_provider(command.provider_id, installation_id=command.installation_id)
        if provider is None:
            return None
        self.ciphertexts[provider.id] = command.credentials_ciphertext
        updated = dataclasses.replace(
            provider,
            has_credentials=True,
            credentials_key_id=command.credentials_key_id,
            updated_at=self._next_timestamp(),
        )
        self.providers[updated.id] = updated
        return updated

    def load_provider_credentials(self, provider_id: str, *, installation_id: str) -> str | None:
        if self.get_provider(provider_id, installation_id=installation_id) is None:
            return None
        return self.ciphertexts.get(provider_id)

    def seed_builtin_providers(self, installation_id: str) -> list[SmsProvider]:
        out: list[SmsProvider] = []
        for key, kind, enabled, is_default, status in (
            ("kavenegar", "kavenegar", False, False, SmsProviderStatus.SETUP),
            ("sms_ir", "sms_ir", False, False, SmsProviderStatus.SETUP),
            ("simulator", "simulator", True, True, SmsProviderStatus.ACTIVE),
        ):
            existing = self.get_provider_by_key(key, installation_id=installation_id)
            if existing is not None:
                out.append(existing)
                continue
            if is_default and self.get_default_provider(installation_id=installation_id) is not None:
                is_default = False
            out.append(
                self.create_provider(
                    CreateSmsProvider(
                        installation_id=installation_id,
                        provider_key=key,
                        display_name_fa=key,
                        provider_kind=kind,
                        is_builtin=True,
                        enabled=enabled,
                        is_default=is_default,
                        status=status,
                    )
                )
            )
        return out

    # -- settings -------------------------------------------------------

    def get_reminder_settings(self, *, installation_id: str) -> SmsReminderSettings | None:
        return self.settings.get(installation_id)

    def save_reminder_settings(self, settings: SmsReminderSettings) -> SmsReminderSettings:
        self.settings[settings.installation_id] = settings
        return settings

    def list_reminder_settings_enabled(self) -> list[SmsReminderSettings]:
        return [s for s in self.settings.values() if s.enabled]

    # -- contract opt-ins -----------------------------------------------

    def get_contract_settings(
        self, *, installation_id: str, contract_id: str
    ) -> SmsContractReminderSettings | None:
        return self.contract_settings.get((installation_id, contract_id))

    def save_contract_settings(
        self, settings: SmsContractReminderSettings
    ) -> SmsContractReminderSettings:
        self.contract_settings[(settings.installation_id, settings.contract_id)] = settings
        return settings

    def list_contract_settings(self, *, installation_id: str) -> list[SmsContractReminderSettings]:
        return [
            row
            for (inst, _), row in self.contract_settings.items()
            if inst == installation_id
        ]

    # -- log -------------------------------------------------------------

    def insert_log(self, entry: SmsReminderLogEntry) -> SmsReminderLogEntry:
        if entry.status in SENT_LOG_STATUSES and self.find_sent_log(
            installation_id=entry.installation_id,
            leg_id=entry.leg_id,
            offset_days=entry.offset_days,
        ) is not None:
            raise SmsReminderAlreadySentError(leg_id=entry.leg_id, offset_days=entry.offset_days)
        stamped = dataclasses.replace(entry, created_at=self._next_timestamp())
        self.log.append(stamped)
        return stamped

    def get_log_entry(self, log_id: str, *, installation_id: str) -> SmsReminderLogEntry | None:
        for entry in self.log:
            if entry.id == log_id and entry.installation_id == installation_id:
                return entry
        return None

    def find_sent_log(
        self, *, installation_id: str, leg_id: str, offset_days: int
    ) -> SmsReminderLogEntry | None:
        for entry in self.log:
            if (
                entry.installation_id == installation_id
                and entry.leg_id == leg_id
                and entry.offset_days == offset_days
                and entry.status in SENT_LOG_STATUSES
            ):
                return entry
        return None

    def list_log(
        self,
        *,
        installation_id: str,
        plan_id: str | None = None,
        leg_id: str | None = None,
        provider_key: str | None = None,
        status: SmsLogStatus | None = None,
        trigger: SmsReminderTrigger | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SmsReminderLogEntry]:
        found = [e for e in self.log if e.installation_id == installation_id]
        if plan_id is not None:
            found = [e for e in found if e.plan_id == plan_id]
        if leg_id is not None:
            found = [e for e in found if e.leg_id == leg_id]
        if provider_key is not None:
            found = [e for e in found if e.provider_key == provider_key]
        if status is not None:
            found = [e for e in found if e.status == status]
        if trigger is not None:
            found = [e for e in found if e.trigger == trigger]
        found.sort(key=lambda e: e.created_at or datetime.datetime.min.replace(tzinfo=datetime.UTC), reverse=True)
        return found[offset : offset + limit]


@dataclass
class FakeSmsClient:
    """A canned provider client: records sends, returns a canned result or raises."""

    provider_kind: str = "simulator"
    result: SmsSendResult | None = None
    error: Exception | None = None
    sends: list[dict[str, object]] = field(default_factory=list)
    closed: int = 0

    def send(self, *, to: str, message: str, sender_line: str | None) -> SmsSendResult:
        self.sends.append({"to": to, "message": message, "sender_line": sender_line})
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return self.result
        return SmsSendResult(
            provider_message_id="sim-fake-1",
            status=SmsDeliveryStatus.SENT,
            provider_status_raw="simulated",
        )

    def get_status(self, provider_message_id: str) -> SmsDeliveryUpdate:
        return SmsDeliveryUpdate(
            provider_message_id=provider_message_id,
            status=SmsDeliveryStatus.SENT,
            provider_status_raw="simulated",
        )

    def close(self) -> None:
        self.closed += 1


class FakeSmsClientFactory:
    """Maps provider kinds to fake clients (default: a simulator fake).

    Mirrors the real factory's fail-closed behaviour for kinds
    without a configured fake: a live kind without its ``api_key``
    raises ``SmsProviderNotConfiguredError``; ``custom`` raises
    ``SmsProviderClientUnavailableError``.
    """

    def __init__(self, clients: dict[str, FakeSmsClient] | None = None) -> None:
        self.clients: dict[str, FakeSmsClient] = clients or {}
        self.calls: list[dict[str, object]] = []

    def client_for(
        self,
        *,
        provider_kind: str,
        provider_key: str,
        credentials: dict[str, str],
        line_number: str | None,
    ) -> FakeSmsClient:
        self.calls.append(
            {
                "provider_kind": provider_kind,
                "provider_key": provider_key,
                "credentials": credentials,
                "line_number": line_number,
            }
        )
        if provider_kind in self.clients:
            return self.clients[provider_kind]
        if provider_kind == "simulator":
            client = FakeSmsClient(provider_kind="simulator")
            self.clients["simulator"] = client
            return client
        if provider_kind in ("kavenegar", "sms_ir"):
            if not credentials.get("api_key"):
                raise SmsProviderNotConfiguredError(
                    provider_key,
                    reason="missing required credential field(s): api_key.",
                )
            client = FakeSmsClient(provider_kind=provider_kind)
            self.clients[provider_kind] = client
            return client
        if provider_kind == "custom":
            raise SmsProviderClientUnavailableError(provider_kind)
        raise AssertionError(f"no fake client configured for kind {provider_kind!r}")


class FakeVault:
    """A reversible test vault (prefix scheme — no real crypto)."""

    key_id = "test-key-1"

    def encrypt(self, plaintext: str) -> str:
        return "enc:" + plaintext

    def decrypt(self, ciphertext: str) -> str:
        if not ciphertext.startswith("enc:"):
            raise ValueError("not a fake-vault ciphertext")
        return ciphertext[4:]


@dataclass
class World:
    """Fresh fakes + services for one test."""

    sms: FakeSmsRepository = field(default_factory=FakeSmsRepository)
    installments: FakeInstallmentRepository = field(default_factory=FakeInstallmentRepository)
    review_queue: InMemoryReviewQueue = field(default_factory=InMemoryReviewQueue)
    review_audit: InMemoryReviewAudit = field(default_factory=InMemoryReviewAudit)
    clients: FakeSmsClientFactory = field(default_factory=FakeSmsClientFactory)
    vault: FakeVault = field(default_factory=FakeVault)

    @property
    def review(self) -> ReviewQueueCommandService:
        return ReviewQueueCommandService(self.review_queue, self.review_audit, now=REVIEW_NOW)

    def registry(self, *, no_vault: bool = False) -> SmsRegistryService:
        return SmsRegistryService(
            self.sms, self.installments, vault=None if no_vault else self.vault
        )

    def query_service(self) -> SmsQueryService:
        return SmsQueryService(self.sms)

    def reminders(self, *, review: bool = True) -> SmsReminderService:
        return SmsReminderService(
            self.sms,
            self.installments,
            client_factory=self.clients,
            vault=self.vault,
            review=self.review if review else None,
        )

    def seed(self) -> list[SmsProvider]:
        return self.registry().seed_builtin_providers(INSTALLATION_ID)

    def simulator(self) -> SmsProvider:
        provider = self.sms.get_provider_by_key("simulator", installation_id=INSTALLATION_ID)
        assert provider is not None
        return provider

    def make_contract(self, contract_id: str = "contract-544") -> InstallmentSaleContract:
        contract = InstallmentSaleContract(
            id=contract_id,
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            company_engine_id=COMPANY_ENGINE_ID,
            code="C-544",
            name="قرارداد فروش قسطی",
            version=1,
            effective_from=_fiscal(TODAY),
        )
        return self.installments.create_contract(contract)

    def make_plan(
        self,
        plan_id: str = "plan-544",
        *,
        contract_id: str | None = "contract-544",
        customer_ref: str | None = PHONE,
        legs: list[tuple[str, int, datetime.date, int]] | None = None,
    ) -> InstallmentPlan:
        plan = InstallmentPlan(
            id=plan_id,
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            company_engine_id=COMPANY_ENGINE_ID,
            invoice_id=INVOICE_ID,
            total_principal=MoneyIRR(10_000_000),
            leg_count=len(legs or [(("leg-1", 1, TODAY, 10_000_000))]),
            contract_id=contract_id,
            customer_ref=customer_ref,
        )
        plan_legs = [
            InstallmentLeg(
                id=leg_id,
                plan_id=plan_id,
                sequence=sequence,
                due_date=_fiscal(due),
                principal=MoneyIRR(principal),
            )
            for leg_id, sequence, due, principal in (
                legs or [("leg-1", 1, TODAY, 10_000_000)]
            )
        ]
        return self.installments.create_plan(plan, plan_legs)

    def enable_settings(self) -> SmsReminderSettings:
        settings = SmsReminderSettings(
            installation_id=INSTALLATION_ID,
            tenant_id=TENANT_ID,
            enabled=True,
        )
        return self.sms.save_reminder_settings(settings)

    def opt_in(self, contract_id: str = "contract-544") -> SmsContractReminderSettings:
        return self.sms.save_contract_settings(
            SmsContractReminderSettings(
                id=new_contract_settings_id(),
                installation_id=INSTALLATION_ID,
                contract_id=contract_id,
                reminders_enabled=True,
            )
        )


@pytest.fixture()
def world() -> World:
    return World()


# ---------------------------------------------------------------------------
# Registry: seed + provider CRUD
# ---------------------------------------------------------------------------


def test_seed_creates_three_builtins_with_simulator_default(world: World) -> None:
    providers = world.seed()
    assert [p.provider_key for p in providers] == ["kavenegar", "sms_ir", "simulator"]
    by_key = {p.provider_key: p for p in providers}
    assert by_key["kavenegar"].enabled is False
    assert by_key["kavenegar"].status == SmsProviderStatus.SETUP
    assert by_key["sms_ir"].enabled is False
    assert by_key["simulator"].enabled is True
    assert by_key["simulator"].is_default is True
    assert by_key["simulator"].status == SmsProviderStatus.ACTIVE
    assert all(p.is_builtin for p in providers)


def test_seed_is_idempotent(world: World) -> None:
    first = world.seed()
    second = world.seed()
    assert [p.id for p in first] == [p.id for p in second]
    assert len(world.sms.list_providers(installation_id=INSTALLATION_ID)) == 3


def test_create_provider_born_disabled_setup(world: World) -> None:
    provider = world.registry().create_provider(
        installation_id=INSTALLATION_ID,
        payload={
            "provider_key": "my-sms",
            "display_name_fa": "پیامک من",
            "provider_kind": "custom",
            "price_per_sms_toman": "125.5",
            "line_type": "service",
        },
    )
    assert provider.enabled is False
    assert provider.status == SmsProviderStatus.SETUP
    assert provider.is_default is False
    assert provider.price_per_sms_toman == Decimal("125.5")
    assert provider.has_credentials is False


def test_create_provider_duplicate_key_is_409(world: World) -> None:
    world.seed()
    with pytest.raises(SmsProviderKeyConflictError):
        world.registry().create_provider(
            installation_id=INSTALLATION_ID,
            payload={
                "provider_key": "kavenegar",
                "display_name_fa": "دوباره",
                "provider_kind": "kavenegar",
            },
        )


def test_create_provider_with_credentials_encrypts_and_never_returns_them(world: World) -> None:
    provider = world.registry().create_provider(
        installation_id=INSTALLATION_ID,
        payload={
            "provider_key": "kavenegar",
            "display_name_fa": "کاوه‌نگار",
            "provider_kind": "kavenegar",
            "credentials": {"api_key": "s3cr3t-key"},
        },
    )
    assert provider.has_credentials is True
    assert provider.credentials_key_id == "test-key-1"
    ciphertext = world.sms.ciphertexts[provider.id]
    assert "s3cr3t-key" not in ciphertext  # the stored form is encrypted
    assert json.loads(world.vault.decrypt(ciphertext)) == {"api_key": "s3cr3t-key"}


def test_create_provider_credentials_without_vault_fails_closed(world: World) -> None:
    with pytest.raises(SmsVaultUnavailableError):
        world.registry(no_vault=True).create_provider(
            installation_id=INSTALLATION_ID,
            payload={
                "provider_key": "kavenegar",
                "display_name_fa": "کاوه‌نگار",
                "provider_kind": "kavenegar",
                "credentials": {"api_key": "s3cr3t-key"},
            },
        )


def test_create_provider_default_clears_previous_default(world: World) -> None:
    world.seed()
    provider = world.registry().create_provider(
        installation_id=INSTALLATION_ID,
        payload={
            "provider_key": "other",
            "display_name_fa": "دیگر",
            "provider_kind": "custom",
            "is_default": True,
        },
    )
    assert provider.is_default is True
    assert world.simulator().is_default is False
    assert world.sms.get_default_provider(installation_id=INSTALLATION_ID) == provider


def test_update_provider_patches_only_supplied_fields(world: World) -> None:
    world.seed()
    simulator = world.simulator()
    updated = world.registry().update_provider(
        installation_id=INSTALLATION_ID,
        provider_id=simulator.id,
        payload={"display_name_fa": "شبیه‌ساز جدید", "price_per_sms_toman": "10"},
    )
    assert updated.display_name_fa == "شبیه‌ساز جدید"
    assert updated.price_per_sms_toman == Decimal("10")
    assert updated.enabled is True  # untouched
    assert updated.is_default is True  # untouched


def test_update_provider_null_clears_a_field(world: World) -> None:
    world.seed()
    simulator = world.simulator()
    world.registry().update_provider(
        installation_id=INSTALLATION_ID,
        provider_id=simulator.id,
        payload={"notes": "یادداشت"},
    )
    updated = world.registry().update_provider(
        installation_id=INSTALLATION_ID,
        provider_id=simulator.id,
        payload={"notes": None},
    )
    assert updated.notes is None


def test_update_provider_missing_is_404(world: World) -> None:
    with pytest.raises(SmsProviderNotFoundError):
        world.registry().update_provider(
            installation_id=INSTALLATION_ID,
            provider_id="no-such-provider",
            payload={"display_name_fa": "x"},
        )


def test_update_provider_rotates_credentials(world: World) -> None:
    world.seed()
    kavenegar = world.sms.get_provider_by_key("kavenegar", installation_id=INSTALLATION_ID)
    assert kavenegar is not None
    updated = world.registry().update_provider(
        installation_id=INSTALLATION_ID,
        provider_id=kavenegar.id,
        payload={"credentials": {"api_key": "new-key"}},
    )
    assert updated.has_credentials is True
    ciphertext = world.sms.ciphertexts[kavenegar.id]
    assert json.loads(world.vault.decrypt(ciphertext)) == {"api_key": "new-key"}


def test_delete_provider_refuses_default_and_missing(world: World) -> None:
    world.seed()
    with pytest.raises(SmsProviderInUseError):
        world.registry().delete_provider(
            installation_id=INSTALLATION_ID, provider_id=world.simulator().id
        )
    with pytest.raises(SmsProviderNotFoundError):
        world.registry().delete_provider(
            installation_id=INSTALLATION_ID, provider_id="no-such-provider"
        )


def test_delete_provider_removes_non_default(world: World) -> None:
    world.seed()
    kavenegar = world.sms.get_provider_by_key("kavenegar", installation_id=INSTALLATION_ID)
    assert kavenegar is not None
    world.registry().delete_provider(installation_id=INSTALLATION_ID, provider_id=kavenegar.id)
    assert world.sms.get_provider(kavenegar.id, installation_id=INSTALLATION_ID) is None


# ---------------------------------------------------------------------------
# Registry: settings + contract opt-ins
# ---------------------------------------------------------------------------


def test_save_reminder_settings_round_trip(world: World) -> None:
    settings = world.registry().save_reminder_settings(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        payload={
            "enabled": True,
            "offsets_days_before": [5, 0],
            "offsets_days_overdue": [2],
            "template_before_due": "مانده {amount_toman} تومان",
            "template_overdue": "معوق {amount_toman} تومان",
        },
    )
    assert settings.enabled is True
    assert settings.offsets_days_before == (0, 5)
    assert settings.offsets_days_overdue == (2,)
    assert world.sms.get_reminder_settings(installation_id=INSTALLATION_ID) == settings


def test_save_reminder_settings_invalid_is_422(world: World) -> None:
    with pytest.raises(SmsValidationError):
        world.registry().save_reminder_settings(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            payload={
                "enabled": True,
                "offsets_days_before": [],
                "offsets_days_overdue": [],
                "template_before_due": "x",
                "template_overdue": "y",
            },
        )


def test_save_contract_settings_requires_existing_contract(world: World) -> None:
    with pytest.raises(InstallmentContractNotFoundError):
        world.registry().save_contract_settings(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            contract_id="no-such-contract",
            payload={"reminders_enabled": True},
        )


def test_save_contract_settings_round_trip(world: World) -> None:
    world.make_contract()
    settings = world.registry().save_contract_settings(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        contract_id="contract-544",
        payload={"reminders_enabled": True},
    )
    assert settings.reminders_enabled is True
    updated = world.registry().save_contract_settings(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        contract_id="contract-544",
        payload={"reminders_enabled": False},
    )
    assert updated.id == settings.id  # same row, updated
    assert updated.reminders_enabled is False


# ---------------------------------------------------------------------------
# Manual trigger
# ---------------------------------------------------------------------------


def test_manual_reminder_sends_via_simulator_and_logs(world: World) -> None:
    world.seed()
    world.make_plan(
        legs=[("leg-1", 1, TODAY + datetime.timedelta(days=3), 12_500_000)]
    )
    entry = world.reminders().send_manual_reminder(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id="plan-544",
        today=TODAY,
    )
    assert entry.status == SmsLogStatus.SENT
    assert entry.trigger == SmsReminderTrigger.MANUAL
    assert entry.provider_key == "simulator"
    assert entry.provider_message_id == "sim-fake-1"
    assert entry.offset_days == -3
    assert entry.recipient_phone == PHONE
    assert entry.sent_at is not None
    assert entry.message_text == (
        "یادآوری سررسید قسط: مبلغ ۱۲۵۰۰۰۰ تومان، سررسید ۱۴۰۵/۰۹/۰۲ "
        "(۳ روز مانده). فاکتور INV-544-001."
    )
    client = world.clients.clients["simulator"]
    assert len(client.sends) == 1
    assert client.sends[0]["to"] == PHONE
    assert client.closed == 1


def test_manual_reminder_overdue_uses_overdue_template(world: World) -> None:
    world.seed()
    world.make_plan(legs=[("leg-1", 1, TODAY - datetime.timedelta(days=1), 5_000_000)])
    entry = world.reminders().send_manual_reminder(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id="plan-544",
        today=TODAY,
    )
    assert entry.offset_days == 1
    assert entry.message_text == (
        "قسط شما به مبلغ ۵۰۰۰۰۰ تومان با سررسید ۱۴۰۵/۰۸/۲۸، "
        "۱ روز معوق است. فاکتور INV-544-001. لطفاً پرداخت فرمایید."
    )


def test_manual_reminder_amount_is_remaining_balance(world: World) -> None:
    world.seed()
    world.make_plan(legs=[("leg-1", 1, TODAY, 10_000_000)])
    # A receipt paying 4,000,000 Rial of the leg (see _add_receipt below).
    _add_receipt(world, "plan-544", "leg-1", 4_000_000)
    entry = world.reminders().send_manual_reminder(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id="plan-544",
        today=TODAY,
    )
    # Remaining balance 6,000,000 Rial = 600,000 Toman.
    assert "۶۰۰۰۰۰" in entry.message_text


def test_manual_reminder_dry_run_logs_and_sends_nothing(world: World) -> None:
    world.seed()
    world.make_plan(legs=[("leg-1", 1, TODAY, 10_000_000)])
    entry = world.reminders().send_manual_reminder(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id="plan-544",
        dry_run=True,
        today=TODAY,
    )
    assert entry.status == SmsLogStatus.DRY_RUN
    assert entry.provider_message_id is None
    assert entry.sent_at is None
    assert "۱۰۰۰۰۰۰" in entry.message_text
    assert world.clients.calls == []  # no client was even built


def test_manual_reminder_defaults_to_earliest_unpaid_leg(world: World) -> None:
    world.seed()
    world.make_plan(
        legs=[
            ("leg-1", 1, TODAY, 10_000_000),
            ("leg-2", 2, TODAY + datetime.timedelta(days=30), 10_000_000),
        ]
    )
    _add_receipt(world, "plan-544", "leg-1", 10_000_000)  # leg-1 paid in full
    entry = world.reminders().send_manual_reminder(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id="plan-544",
        today=TODAY,
    )
    assert entry.leg_id == "leg-2"


def test_manual_reminder_explicit_leg_and_phone_override(world: World) -> None:
    world.seed()
    world.make_plan(
        legs=[
            ("leg-1", 1, TODAY, 10_000_000),
            ("leg-2", 2, TODAY + datetime.timedelta(days=30), 10_000_000),
        ]
    )
    entry = world.reminders().send_manual_reminder(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id="plan-544",
        leg_id="leg-2",
        recipient_phone="+989121111111",
        today=TODAY,
    )
    assert entry.leg_id == "leg-2"
    assert entry.recipient_phone == "09121111111"


def test_manual_reminder_errors(world: World) -> None:
    world.seed()
    world.make_plan(legs=[("leg-1", 1, TODAY, 10_000_000)])
    service = world.reminders()
    with pytest.raises(SmsLegNotFoundError):
        service.send_manual_reminder(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id="plan-544",
            leg_id="no-such-leg",
            today=TODAY,
        )
    with pytest.raises(SmsValidationError):
        service.send_manual_reminder(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id="plan-544",
            recipient_phone="not-a-phone",
            today=TODAY,
        )
    with pytest.raises(SmsProviderNotFoundError):
        service.send_manual_reminder(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id="plan-544",
            provider_id="no-such-provider",
            today=TODAY,
        )
    with pytest.raises(SmsLegNotRemindableError):
        service.send_manual_reminder(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id="plan-544",
            pending_clearance_leg_ids=frozenset({"leg-1"}),
            today=TODAY,
        )
    with pytest.raises(InstallmentPlanNotFoundError):
        service.send_manual_reminder(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id="no-such-plan",
            today=TODAY,
        )


def test_manual_reminder_no_phone_and_no_unpaid_leg(world: World) -> None:
    world.seed()
    world.make_plan(plan_id="plan-no-phone", customer_ref=None)
    with pytest.raises(SmsRecipientPhoneMissingError):
        world.reminders().send_manual_reminder(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id="plan-no-phone",
            today=TODAY,
        )
    world.make_plan(plan_id="plan-paid", legs=[("leg-p", 1, TODAY, 10_000_000)])
    _add_receipt(world, "plan-paid", "leg-p", 10_000_000)
    with pytest.raises(SmsNoUnpaidLegError):
        world.reminders().send_manual_reminder(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id="plan-paid",
            today=TODAY,
        )
    with pytest.raises(SmsLegNotRemindableError):
        world.reminders().send_manual_reminder(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id="plan-paid",
            leg_id="leg-p",
            today=TODAY,
        )


def test_manual_reminder_disabled_provider_and_no_default(world: World) -> None:
    world.seed()
    world.make_plan()
    world.registry().update_provider(
        installation_id=INSTALLATION_ID,
        provider_id=world.simulator().id,
        payload={"enabled": False},
    )
    with pytest.raises(SmsProviderNotEnabledError):
        world.reminders().send_manual_reminder(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id="plan-544",
            today=TODAY,
        )
    # No default at all: delete the (disabled, non-default) kavenegar row and
    # clear the simulator's default flag via a second default, then delete it.
    world.registry().update_provider(
        installation_id=INSTALLATION_ID,
        provider_id=world.simulator().id,
        payload={"is_default": False},
    )
    with pytest.raises(SmsNoProviderAvailableError):
        world.reminders().send_manual_reminder(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id="plan-544",
            today=TODAY,
        )


def test_manual_reminder_already_sent_is_409(world: World) -> None:
    world.seed()
    world.make_plan(legs=[("leg-1", 1, TODAY, 10_000_000)])
    service = world.reminders()
    service.send_manual_reminder(
        tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, plan_id="plan-544", today=TODAY
    )
    with pytest.raises(SmsReminderAlreadySentError):
        service.send_manual_reminder(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id="plan-544",
            today=TODAY,
        )
    # A dry run never blocks a real send, and a failed send never blocks a retry.
    assert len(world.sms.log) == 1


def test_manual_reminder_failed_send_logs_failed_row_and_files_review(world: World) -> None:
    world.seed()
    world.make_plan(legs=[("leg-1", 1, TODAY, 10_000_000)])
    world.clients.clients["simulator"] = FakeSmsClient(
        provider_kind="simulator",
        error=SmsProviderRejectedError("simulator", reason="the provider said no."),
    )
    with pytest.raises(SmsProviderRejectedError):
        world.reminders().send_manual_reminder(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id="plan-544",
            today=TODAY,
        )
    assert len(world.sms.log) == 1
    entry = world.sms.log[0]
    assert entry.status == SmsLogStatus.FAILED
    assert entry.error_code == "SMS_PROVIDER_REJECTED"
    items = list(world.review_queue.items.values())
    assert len(items) == 1
    assert items[0].item_type == ReviewItemType.SMS_DELIVERY_FAILED
    assert items[0].severity == ReviewSeverity.WARNING
    assert items[0].amount_irr == 10_000_000
    # The failed row does NOT block a retry.
    world.clients.clients["simulator"] = FakeSmsClient(provider_kind="simulator")
    retry = world.reminders().send_manual_reminder(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id="plan-544",
        today=TODAY,
    )
    assert retry.status == SmsLogStatus.SENT


def test_manual_reminder_provider_reported_failed_is_a_failure(world: World) -> None:
    world.seed()
    world.make_plan(legs=[("leg-1", 1, TODAY, 10_000_000)])
    world.clients.clients["simulator"] = FakeSmsClient(
        provider_kind="simulator",
        result=SmsSendResult(
            provider_message_id="x",
            status=SmsDeliveryStatus.FAILED,
            provider_status_raw="6:failed",
        ),
    )
    with pytest.raises(SmsProviderRejectedError):
        world.reminders().send_manual_reminder(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id="plan-544",
            today=TODAY,
        )
    assert world.sms.log[0].status == SmsLogStatus.FAILED
    assert len(world.review_queue.items) == 1


def test_manual_reminder_unconfigured_live_provider_fails_closed(world: World) -> None:
    world.seed()
    world.make_plan(legs=[("leg-1", 1, TODAY, 10_000_000)])
    # Make kavenegar the default (born without credentials).
    kavenegar = world.sms.get_provider_by_key("kavenegar", installation_id=INSTALLATION_ID)
    assert kavenegar is not None
    world.registry().update_provider(
        installation_id=INSTALLATION_ID,
        provider_id=kavenegar.id,
        payload={"enabled": True, "is_default": True},
    )
    with pytest.raises(SmsProviderNotConfiguredError):
        world.reminders().send_manual_reminder(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id="plan-544",
            today=TODAY,
        )
    # Never silent: a failed row + a review item.
    assert world.sms.log[0].status == SmsLogStatus.FAILED
    assert world.sms.log[0].error_code == "SMS_PROVIDER_NOT_CONFIGURED"
    assert len(world.review_queue.items) == 1
    # And no send was attempted (the factory refused before any client existed).
    assert "kavenegar" not in world.clients.clients


def test_manual_reminder_custom_provider_has_no_client(world: World) -> None:
    world.seed()
    world.make_plan(legs=[("leg-1", 1, TODAY, 10_000_000)])
    custom = world.registry().create_provider(
        installation_id=INSTALLATION_ID,
        payload={
            "provider_key": "my-gateway",
            "display_name_fa": "درگاه من",
            "provider_kind": "custom",
            "enabled": True,
            "is_default": True,
        },
    )
    with pytest.raises(SmsProviderClientUnavailableError):
        world.reminders().send_manual_reminder(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id="plan-544",
            provider_id=custom.id,
            today=TODAY,
        )
    assert world.sms.log[0].status == SmsLogStatus.FAILED
    assert world.sms.log[0].error_code == "SMS_PROVIDER_CLIENT_UNAVAILABLE"
    assert len(world.review_queue.items) == 1


def test_manual_reminder_with_credentials_decrypts_and_sends(world: World) -> None:
    world.seed()
    world.make_plan(legs=[("leg-1", 1, TODAY, 10_000_000)])
    kavenegar = world.sms.get_provider_by_key("kavenegar", installation_id=INSTALLATION_ID)
    assert kavenegar is not None
    world.registry().update_provider(
        installation_id=INSTALLATION_ID,
        provider_id=kavenegar.id,
        payload={
            "enabled": True,
            "is_default": True,
            "credentials": {"api_key": "live-key"},
        },
    )
    fake = FakeSmsClient(
        provider_kind="kavenegar",
        result=SmsSendResult(
            provider_message_id="kav-1",
            status=SmsDeliveryStatus.QUEUED,
            provider_status_raw="1:queued",
            cost_toman=Decimal("150"),
            cost_raw="1500",
            cost_unit="IRR",
        ),
    )
    world.clients.clients["kavenegar"] = fake
    entry = world.reminders().send_manual_reminder(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id="plan-544",
        today=TODAY,
    )
    assert entry.status == SmsLogStatus.QUEUED
    assert entry.provider_key == "kavenegar"
    assert entry.cost_toman == Decimal("150")
    assert entry.provider_cost_raw == "1500"
    assert entry.provider_cost_unit == "IRR"
    call = world.clients.calls[0]
    assert call["provider_kind"] == "kavenegar"
    assert call["credentials"] == {"api_key": "live-key"}


# ---------------------------------------------------------------------------
# Daily job
# ---------------------------------------------------------------------------


def test_daily_job_sends_for_opted_in_plans_at_offsets(world: World) -> None:
    world.seed()
    world.make_contract()
    world.enable_settings()
    world.opt_in()
    world.make_plan(
        plan_id="plan-a",
        legs=[
            ("leg-a1", 1, TODAY + datetime.timedelta(days=3), 10_000_000),  # fires (-3)
            ("leg-a2", 2, TODAY + datetime.timedelta(days=2), 10_000_000),  # no offset
        ],
    )
    world.make_plan(
        plan_id="plan-b",
        legs=[("leg-b1", 1, TODAY - datetime.timedelta(days=1), 10_000_000)],  # fires (+1)
    )
    report = world.reminders().run_daily_reminder_job(today=TODAY)
    assert report.installations_scanned == 1
    assert report.plans_scanned == 2
    assert report.reminders_due == 2
    assert report.sent == 2
    assert report.failed == 0
    assert report.errors == ()
    sent = [e for e in world.sms.log if e.status == SmsLogStatus.SENT]
    assert {(e.leg_id, e.offset_days) for e in sent} == {("leg-a1", -3), ("leg-b1", 1)}
    assert all(e.trigger == SmsReminderTrigger.SCHEDULED for e in sent)


def test_daily_job_skips_disabled_installations_and_non_opted_in_plans(world: World) -> None:
    world.seed()
    world.make_contract()
    # Settings exist but are disabled: the installation is not scanned at all.
    world.sms.save_reminder_settings(
        SmsReminderSettings(
            installation_id=INSTALLATION_ID, tenant_id=TENANT_ID, enabled=False
        )
    )
    world.make_plan()
    report = world.reminders().run_daily_reminder_job(today=TODAY)
    assert report.installations_scanned == 0
    assert report.sent == 0

    # Enabled settings, but the plan's contract never opted in.
    world.enable_settings()
    report = world.reminders().run_daily_reminder_job(today=TODAY)
    assert report.installations_scanned == 1
    assert report.plans_scanned == 1
    assert report.skipped_not_opted_in == 1
    assert report.sent == 0
    assert world.sms.log == []


def test_daily_job_skips_no_phone_with_a_skipped_row_and_no_review(world: World) -> None:
    world.seed()
    world.make_contract()
    world.enable_settings()
    world.opt_in()
    world.make_plan(plan_id="plan-nophone", customer_ref=None)
    report = world.reminders().run_daily_reminder_job(today=TODAY)
    assert report.reminders_due == 1
    assert report.sent == 0
    assert report.skipped_no_phone == 1
    assert len(world.sms.log) == 1
    assert world.sms.log[0].status == SmsLogStatus.SKIPPED
    assert world.sms.log[0].error_code == "SMS_RECIPIENT_PHONE_MISSING"
    assert world.review_queue.items == {}


def test_daily_job_counts_pending_clearance_and_skips_already_sent(world: World) -> None:
    world.seed()
    world.make_contract()
    world.enable_settings()
    world.opt_in()
    world.make_plan(
        legs=[
            ("leg-pending", 1, TODAY, 10_000_000),
            ("leg-sent", 2, TODAY, 10_000_000),
            ("leg-new", 3, TODAY, 10_000_000),
        ]
    )
    # leg-sent already got its reminder for offset 0 (a previous run).
    world.sms.insert_log(
        SmsReminderLogEntry(
            id="already-sent",
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id="plan-544",
            leg_id="leg-sent",
            provider_id=world.simulator().id,
            provider_key="simulator",
            recipient_phone=PHONE,
            message_text="earlier",
            offset_days=0,
            trigger=SmsReminderTrigger.SCHEDULED,
            status=SmsLogStatus.SENT,
        )
    )
    report = world.reminders().run_daily_reminder_job(
        today=TODAY, pending_clearance_leg_ids=frozenset({"leg-pending"})
    )
    assert report.skipped_pending_clearance == 1
    assert report.skipped_already_sent == 1
    assert report.sent == 1
    assert [e.leg_id for e in world.sms.log if e.status == SmsLogStatus.SENT][-1] == "leg-new"


def test_daily_job_provider_not_sendable_files_one_review_item(world: World) -> None:
    world.seed()
    world.make_contract()
    world.enable_settings()
    world.opt_in()
    world.make_plan(
        legs=[
            ("leg-1", 1, TODAY, 10_000_000),
            ("leg-2", 2, TODAY, 10_000_000),
        ]
    )
    # Default = kavenegar, enabled, but no credentials stored.
    kavenegar = world.sms.get_provider_by_key("kavenegar", installation_id=INSTALLATION_ID)
    assert kavenegar is not None
    world.registry().update_provider(
        installation_id=INSTALLATION_ID,
        provider_id=kavenegar.id,
        payload={"enabled": True, "is_default": True},
    )
    report = world.reminders().run_daily_reminder_job(today=TODAY)
    assert report.reminders_due == 2
    assert report.sent == 0
    assert report.failed == 0  # skipped, not failed
    assert len(report.errors) == 1
    assert all(e.status == SmsLogStatus.SKIPPED for e in world.sms.log)
    assert all(e.error_code == "SMS_PROVIDER_NOT_CONFIGURED" for e in world.sms.log)
    items = list(world.review_queue.items.values())
    assert len(items) == 1  # ONE review item per installation, not per leg
    assert items[0].item_type == ReviewItemType.SMS_DELIVERY_FAILED
    assert "2 due reminder(s)" in items[0].reason


def test_daily_job_no_enabled_default_provider_skips_installation(world: World) -> None:
    world.seed()
    world.make_contract()
    world.enable_settings()
    world.opt_in()
    world.make_plan()
    world.registry().update_provider(
        installation_id=INSTALLATION_ID,
        provider_id=world.simulator().id,
        payload={"enabled": False},
    )
    report = world.reminders().run_daily_reminder_job(today=TODAY)
    assert report.skipped_no_provider == 1
    assert report.plans_scanned == 0
    assert report.sent == 0
    assert len(report.errors) == 1
    assert world.sms.log == []


def test_daily_job_per_leg_failure_continues(world: World) -> None:
    world.seed()
    world.make_contract()
    world.enable_settings()
    world.opt_in()
    world.make_plan(
        legs=[
            ("leg-fail", 1, TODAY, 10_000_000),
            ("leg-ok", 2, TODAY, 10_000_000),
        ]
    )

    class FlakyClient(FakeSmsClient):
        def send(self, *, to: str, message: str, sender_line: str | None) -> SmsSendResult:
            self.sends.append({"to": to, "message": message, "sender_line": sender_line})
            if len(self.sends) == 1:
                raise SmsProviderRejectedError("simulator", reason="flaky.")
            return SmsSendResult(
                provider_message_id="sim-ok",
                status=SmsDeliveryStatus.SENT,
                provider_status_raw="simulated",
            )

    world.clients.clients["simulator"] = FlakyClient(provider_kind="simulator")
    report = world.reminders().run_daily_reminder_job(today=TODAY)
    assert report.sent == 1
    assert report.failed == 1
    assert len(report.errors) == 1
    assert len(world.review_queue.items) == 1
    statuses = {e.leg_id: e.status for e in world.sms.log}
    assert statuses["leg-fail"] == SmsLogStatus.FAILED
    assert statuses["leg-ok"] == SmsLogStatus.SENT


def test_daily_job_without_review_service_still_sends(world: World) -> None:
    world.seed()
    world.make_contract()
    world.enable_settings()
    world.opt_in()
    world.make_plan()
    report = world.reminders(review=False).run_daily_reminder_job(today=TODAY)
    assert report.sent == 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _add_receipt(
    world: World, plan_id: str, leg_id: str, amount_irr: int
) -> None:
    """Record a receipt allocated fully to one leg (for balance tests)."""
    receipt = InstallmentReceipt(
        id=f"receipt-{leg_id}",
        plan_id=plan_id,
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        invoice_id=INVOICE_ID,
        journal_move_id=1,
        amount=MoneyIRR(amount_irr),
        received_date=_fiscal(TODAY),
        payment_method=PaymentMethod.CASH,
        allocations=(LegAllocation(leg_id=leg_id, amount=MoneyIRR(amount_irr)),),
    )
    world.installments.create_receipt(receipt)
