"""Unit tests for the SMS domain + provider clients (US-544).

Pure-domain tests (validation, templates, scheduling, credentials)
plus provider-client tests against ``httpx.MockTransport`` with
canned provider payloads. No live SMS call is ever made from a
test. "Today" is pinned explicitly (2026-11-20 = 1405/08/29).
"""

from __future__ import annotations

import datetime
import json
from decimal import Decimal

import httpx
import pytest

from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.sms.credentials import (
    parse_sms_credentials,
    require_credential_fields,
)
from hesabyar.domain.sms.errors import (
    SmsProviderAuthError,
    SmsProviderClientUnavailableError,
    SmsProviderInvalidResponseError,
    SmsProviderNotConfiguredError,
    SmsProviderRejectedError,
    SmsProviderUnreachableError,
    SmsValidationError,
)
from hesabyar.domain.sms.models import (
    DEFAULT_OFFSETS_DAYS_BEFORE,
    DEFAULT_OFFSETS_DAYS_OVERDUE,
    DEFAULT_TEMPLATE_BEFORE_DUE,
    DEFAULT_TEMPLATE_OVERDUE,
    SENT_LOG_STATUSES,
    SmsDeliveryStatus,
    SmsLogStatus,
    SmsProviderStatus,
    SmsReminderSettings,
)
from hesabyar.domain.sms.scheduling import LegReminderView, due_reminders
from hesabyar.domain.sms.templates import (
    ReminderTemplateContext,
    render_reminder_template,
    to_persian_digits,
    validate_template,
)
from hesabyar.domain.sms.validation import (
    normalize_iranian_mobile,
    validate_provider_payload,
    validate_reminder_settings_payload,
)
from hesabyar.infrastructure.sms.factory import DefaultSmsClientFactory
from hesabyar.infrastructure.sms.kavenegar import KavenegarSmsClient
from hesabyar.infrastructure.sms.simulator import SimulatorSmsClient
from hesabyar.infrastructure.sms.sms_ir import SmsIrSmsClient

TODAY = datetime.date(2026, 11, 20)  # 1405/08/29


def _fiscal(day: datetime.date) -> FiscalDate:
    return FiscalDate.from_gregorian(day)


# ---------------------------------------------------------------------------
# Phone normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("09123456789", "09123456789"),
        ("+989123456789", "09123456789"),
        ("00989123456789", "09123456789"),
        ("989123456789", "09123456789"),
        (" 09123456789 ", "09123456789"),
        ("0912 345 6789", "09123456789"),
        ("0912-345-6789", "09123456789"),
        ("۰۹۱۲۳۴۵۶۷۸۹", "09123456789"),  # Persian digits
        ("٠٩١٢٣٤٥٦٧٨٩", "09123456789"),  # Arabic-Indic digits
        ("+۹۸۹۱۲۳۴۵۶۷۸۹", "09123456789"),  # Persian digits, international form
    ],
)
def test_normalize_iranian_mobile_accepts_written_forms(raw: str, expected: str) -> None:
    assert normalize_iranian_mobile(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "123",
        "0912345678",  # 10 digits
        "091234567890",  # 12 digits
        "02123456789",  # landline (starts 02)
        "+442071234567",  # foreign
        "0912345678a",
        "0912 345 678",  # too short even after space removal
    ],
)
def test_normalize_iranian_mobile_rejects_non_mobiles(raw: str | None) -> None:
    assert normalize_iranian_mobile(raw) is None


# ---------------------------------------------------------------------------
# Provider payload validation
# ---------------------------------------------------------------------------


def _valid_provider_payload() -> dict[str, object]:
    return {
        "provider_key": "my-provider",
        "display_name_fa": "ارائه‌دهندهٔ من",
        "provider_kind": "custom",
    }


def test_validate_provider_payload_create_minimal() -> None:
    validated = validate_provider_payload(_valid_provider_payload(), partial=False)
    assert validated.provider_key == "my-provider"
    assert validated.display_name_fa == "ارائه‌دهندهٔ من"
    assert validated.provider_kind == "custom"
    assert validated.enabled is None
    assert validated.credentials is None


def test_validate_provider_payload_create_full() -> None:
    payload = _valid_provider_payload()
    payload.update(
        {
            "provider_kind": "kavenegar",
            "enabled": True,
            "is_default": True,
            "status": "active",
            "line_number": "10001234",
            "price_per_sms_toman": "125.5",
            "rate_limit_per_minute": 60,
            "line_type": "service",
            "contract_required": True,
            "enmad_required": False,
            "notes": "قرارداد تا پایان سال",
            "credentials": {"api_key": "secret-key"},
        }
    )
    validated = validate_provider_payload(payload, partial=False)
    assert validated.enabled is True
    assert validated.is_default is True
    assert validated.status == SmsProviderStatus.ACTIVE
    assert validated.price_per_sms_toman == Decimal("125.5")
    assert validated.rate_limit_per_minute == 60
    assert validated.credentials == {"api_key": "secret-key"}


def test_validate_provider_payload_create_requires_key_name_kind() -> None:
    with pytest.raises(SmsValidationError) as exc_info:
        validate_provider_payload({}, partial=False)
    assert set(exc_info.value.field_errors) == {
        "provider_key",
        "display_name_fa",
        "provider_kind",
    }


@pytest.mark.parametrize("bad_key", ["My Provider", "UPPER", "-leading", "a" * 65, "a/b"])
def test_validate_provider_payload_rejects_bad_keys(bad_key: str) -> None:
    payload = _valid_provider_payload()
    payload["provider_key"] = bad_key
    with pytest.raises(SmsValidationError) as exc_info:
        validate_provider_payload(payload, partial=False)
    assert "provider_key" in exc_info.value.field_errors


def test_validate_provider_payload_rejects_unknown_kind_and_status() -> None:
    payload = _valid_provider_payload()
    payload["provider_kind"] = "telegram"
    payload["status"] = "broken"
    with pytest.raises(SmsValidationError) as exc_info:
        validate_provider_payload(payload, partial=False)
    assert set(exc_info.value.field_errors) == {"provider_kind", "status"}


def test_validate_provider_payload_sms_ir_line_must_be_digits() -> None:
    payload = _valid_provider_payload()
    payload.update({"provider_kind": "sms_ir", "line_number": "3000-ABC"})
    with pytest.raises(SmsValidationError) as exc_info:
        validate_provider_payload(payload, partial=False)
    assert "line_number" in exc_info.value.field_errors

    payload["line_number"] = "3000123456"
    validated = validate_provider_payload(payload, partial=False)
    assert validated.line_number == "3000123456"


def test_validate_provider_payload_credentials_must_satisfy_kind() -> None:
    payload = _valid_provider_payload()
    payload.update({"provider_kind": "kavenegar", "credentials": {"other": "x"}})
    with pytest.raises(SmsValidationError) as exc_info:
        validate_provider_payload(payload, partial=False)
    assert "api_key" in exc_info.value.field_errors["credentials"]
    # The credential VALUE never appears in an error.
    assert "x" not in exc_info.value.field_errors["credentials"]


def test_validate_provider_payload_patch_validates_only_present_fields() -> None:
    validated = validate_provider_payload(
        {"display_name_fa": "نام جدید", "price_per_sms_toman": None},
        partial=True,
        provider_kind="kavenegar",
    )
    assert validated.display_name_fa == "نام جدید"
    assert validated.provider_key is None  # immutable — never patched
    assert validated.provider_kind == "kavenegar"  # the stored kind, from the caller
    assert validated.price_per_sms_toman is None


def test_validate_provider_payload_rejects_negative_price_and_zero_rate() -> None:
    payload = _valid_provider_payload()
    payload["price_per_sms_toman"] = "-1"
    payload["rate_limit_per_minute"] = 0
    with pytest.raises(SmsValidationError) as exc_info:
        validate_provider_payload(payload, partial=False)
    assert set(exc_info.value.field_errors) == {
        "price_per_sms_toman",
        "rate_limit_per_minute",
    }


# ---------------------------------------------------------------------------
# Reminder settings validation
# ---------------------------------------------------------------------------


def _valid_settings_payload() -> dict[str, object]:
    return {
        "enabled": True,
        "offsets_days_before": [3, 0],
        "offsets_days_overdue": [1],
        "template_before_due": DEFAULT_TEMPLATE_BEFORE_DUE,
        "template_overdue": DEFAULT_TEMPLATE_OVERDUE,
    }


def test_validate_reminder_settings_payload_ok_and_sorted() -> None:
    payload = _valid_settings_payload()
    payload["offsets_days_before"] = [0, 3, 1]
    validated = validate_reminder_settings_payload(payload)
    assert validated.enabled is True
    assert validated.offsets_days_before == (0, 1, 3)
    assert validated.offsets_days_overdue == (1,)


def test_validate_reminder_settings_requires_at_least_one_offset() -> None:
    payload = _valid_settings_payload()
    payload["offsets_days_before"] = []
    payload["offsets_days_overdue"] = []
    with pytest.raises(SmsValidationError) as exc_info:
        validate_reminder_settings_payload(payload)
    assert "offsets_days_before" in exc_info.value.field_errors


def test_validate_reminder_settings_offset_bounds() -> None:
    payload = _valid_settings_payload()
    payload["offsets_days_before"] = [31]
    payload["offsets_days_overdue"] = [0]  # overdue offsets start at 1
    with pytest.raises(SmsValidationError) as exc_info:
        validate_reminder_settings_payload(payload)
    assert set(exc_info.value.field_errors) == {
        "offsets_days_before",
        "offsets_days_overdue",
    }


def test_validate_reminder_settings_rejects_duplicate_offsets() -> None:
    payload = _valid_settings_payload()
    payload["offsets_days_before"] = [3, 3]
    with pytest.raises(SmsValidationError) as exc_info:
        validate_reminder_settings_payload(payload)
    assert "offsets_days_before" in exc_info.value.field_errors


def test_validate_reminder_settings_rejects_unknown_placeholder() -> None:
    payload = _valid_settings_payload()
    payload["template_before_due"] = "مبلغ {amount} تومان"
    with pytest.raises(SmsValidationError) as exc_info:
        validate_reminder_settings_payload(payload)
    assert "template_before_due" in exc_info.value.field_errors
    assert "{amount}" in exc_info.value.field_errors["template_before_due"]


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def test_to_persian_digits_transliterates_only_digits() -> None:
    assert to_persian_digits("INV-1405-001") == "INV-۱۴۰۵-۰۰۱"
    assert to_persian_digits(1250000) == "۱۲۵۰۰۰۰"
    assert to_persian_digits("1405/09/02") == "۱۴۰۵/۰۹/۰۲"


def test_validate_template_accepts_known_placeholders_only() -> None:
    assert validate_template(DEFAULT_TEMPLATE_BEFORE_DUE) == []
    assert validate_template(DEFAULT_TEMPLATE_OVERDUE) == []
    problems = validate_template("مبلغ {amount_toman} و {foo}")
    assert len(problems) == 1
    assert "{foo}" in problems[0]


def test_validate_template_rejects_unbalanced_braces() -> None:
    problems = validate_template("مبلغ {amount_toman تومان")
    assert len(problems) == 1
    assert "unbalanced" in problems[0]


def test_validate_template_rejects_attribute_access() -> None:
    problems = validate_template("{amount_toman.x}")
    assert len(problems) == 1


def test_render_reminder_template_persian_digits_and_jalali_date() -> None:
    context = ReminderTemplateContext(
        amount_toman=1250000,
        due_date=_fiscal(datetime.date(2026, 11, 23)),  # 1405/09/02
        days_remaining=3,
        days_overdue=0,
        invoice_id="INV-1405-001",
        leg_sequence=2,
    )
    rendered = render_reminder_template(DEFAULT_TEMPLATE_BEFORE_DUE, context)
    assert rendered == (
        "یادآوری سررسید قسط: مبلغ ۱۲۵۰۰۰۰ تومان، سررسید ۱۴۰۵/۰۹/۰۲ "
        "(۳ روز مانده). فاکتور INV-1405-001."
    )


def test_render_reminder_template_overdue() -> None:
    context = ReminderTemplateContext(
        amount_toman=500000,
        due_date=_fiscal(datetime.date(2026, 11, 19)),  # 1405/08/28
        days_remaining=0,
        days_overdue=1,
        invoice_id="INV-9",
        leg_sequence=1,
    )
    rendered = render_reminder_template(DEFAULT_TEMPLATE_OVERDUE, context)
    assert "۵۰۰۰۰۰" in rendered
    assert "۱۴۰۵/۰۸/۲۸" in rendered
    assert "۱ روز معوق" in rendered
    assert "INV-9" in rendered  # identifiers stay verbatim


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


def _leg(
    leg_id: str,
    *,
    sequence: int = 1,
    due: datetime.date,
    principal: int = 1_000_000,
    received: int = 0,
    pending_clearance: bool = False,
) -> LegReminderView:
    return LegReminderView(
        leg_id=leg_id,
        sequence=sequence,
        due_date=_fiscal(due),
        principal=MoneyIRR(principal),
        received=MoneyIRR(received),
        pending_clearance=pending_clearance,
    )


def test_due_reminders_fires_only_on_the_exact_offset_day() -> None:
    legs = [
        _leg("due-in-3", due=TODAY + datetime.timedelta(days=3)),
        _leg("due-today", due=TODAY),
        _leg("due-yesterday", due=TODAY - datetime.timedelta(days=1)),
        _leg("due-in-2", due=TODAY + datetime.timedelta(days=2)),  # no offset 2
        _leg("due-2-days-ago", due=TODAY - datetime.timedelta(days=2)),  # no offset 2
    ]
    due = due_reminders(legs, TODAY, (3, 0), (1,))
    assert [(item.leg_id, item.offset_days) for item in due] == [
        ("due-yesterday", 1),
        ("due-today", 0),
        ("due-in-3", -3),
    ]


def test_due_reminders_sorts_by_due_date_then_sequence() -> None:
    legs = [
        _leg("b", sequence=2, due=TODAY + datetime.timedelta(days=3)),
        _leg("a", sequence=1, due=TODAY + datetime.timedelta(days=3)),
        _leg("c", sequence=1, due=TODAY),
    ]
    due = due_reminders(legs, TODAY, (3, 0), ())
    assert [item.leg_id for item in due] == ["c", "a", "b"]


def test_due_reminders_skips_paid_and_pending_clearance_legs() -> None:
    legs = [
        _leg("paid", due=TODAY, received=1_000_000),
        _leg("partial", due=TODAY, received=400_000),
        _leg("pending", due=TODAY, pending_clearance=True),
    ]
    due = due_reminders(legs, TODAY, (0,), ())
    assert [item.leg_id for item in due] == ["partial"]
    assert due[0].balance == MoneyIRR(600_000)


def test_leg_balance_never_negative() -> None:
    leg = _leg("over", due=TODAY, principal=100, received=200)
    assert leg.balance == MoneyIRR(0)
    assert due_reminders([leg], TODAY, (0,), ()) == []


def test_due_reminders_empty_when_nothing_configured() -> None:
    assert due_reminders([_leg("x", due=TODAY)], TODAY, (), ()) == []


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def test_parse_sms_credentials_accepts_json_object() -> None:
    assert parse_sms_credentials('{"api_key": "k", "base_url": "https://x"}', provider_key="kavenegar") == {
        "api_key": "k",
        "base_url": "https://x",
    }


def test_parse_sms_credentials_fails_closed_on_bad_payload() -> None:
    with pytest.raises(SmsProviderNotConfiguredError):
        parse_sms_credentials("not json", provider_key="kavenegar")
    with pytest.raises(SmsProviderNotConfiguredError):
        parse_sms_credentials('["a list"]', provider_key="kavenegar")


def test_require_credential_fields_reports_names_never_values() -> None:
    with pytest.raises(SmsProviderNotConfiguredError) as exc_info:
        require_credential_fields({}, provider_kind="kavenegar", provider_key="kavenegar")
    assert "api_key" in str(exc_info.value)
    secrets = require_credential_fields(
        {"api_key": "s3cr3t", "base_url": "https://x"},
        provider_kind="kavenegar",
        provider_key="kavenegar",
    )
    assert secrets == {"api_key": "s3cr3t", "base_url": "https://x"}
    # The simulator needs no credentials.
    assert require_credential_fields({}, provider_kind="simulator", provider_key="simulator") == {}


# ---------------------------------------------------------------------------
# Models / defaults
# ---------------------------------------------------------------------------


def test_reminder_settings_defaults_are_disabled() -> None:
    settings = SmsReminderSettings.defaults(installation_id="inst", tenant_id="ten")
    assert settings.enabled is False
    assert settings.offsets_days_before == DEFAULT_OFFSETS_DAYS_BEFORE == (3, 0)
    assert settings.offsets_days_overdue == DEFAULT_OFFSETS_DAYS_OVERDUE == (1,)
    assert settings.template_before_due == DEFAULT_TEMPLATE_BEFORE_DUE
    assert settings.template_overdue == DEFAULT_TEMPLATE_OVERDUE


def test_sent_log_statuses_are_the_dedup_set() -> None:
    assert SENT_LOG_STATUSES == {
        SmsLogStatus.QUEUED,
        SmsLogStatus.SENT,
        SmsLogStatus.DELIVERED,
    }
    assert SmsLogStatus.FAILED not in SENT_LOG_STATUSES
    assert SmsLogStatus.DRY_RUN not in SENT_LOG_STATUSES


# ---------------------------------------------------------------------------
# Provider clients (MockTransport — no live call)
# ---------------------------------------------------------------------------


def _kavenegar_success_payload() -> dict[str, object]:
    return {
        "return": {"status": 200, "message": "تایید شد"},
        "entries": [
            {
                "messageid": 123456789,
                "message": "یادآوری",
                "status": 5,
                "statustext": "ارسال شده",
                "sender": "10001234",
                "receptor": "09123456789",
                "date": 1700000000,
                "cost": 1500,
            }
        ],
    }


def test_kavenegar_send_success_parses_cost_as_rial_over_ten() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.content.decode()
        return httpx.Response(200, json=_kavenegar_success_payload())

    client = KavenegarSmsClient(api_key="test-key", transport=httpx.MockTransport(handler))
    result = client.send(to="09123456789", message="یادآوری", sender_line="10001234")
    assert result.provider_message_id == "123456789"
    assert result.status == SmsDeliveryStatus.SENT  # Table 2 code 5
    assert result.provider_status_raw == "5:ارسال شده"
    assert result.cost_toman == Decimal("150")  # 1500 Rial / 10
    assert result.cost_raw == "1500"
    assert result.cost_unit == "IRR"
    assert seen["path"] == "/v1/test-key/sms/send.json"
    assert "receptor=09123456789" in str(seen["body"])
    assert "sender=10001234" in str(seen["body"])


def test_kavenegar_send_omits_sender_when_no_line() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        return httpx.Response(200, json=_kavenegar_success_payload())

    client = KavenegarSmsClient(api_key="test-key", transport=httpx.MockTransport(handler))
    client.send(to="09123456789", message="یادآوری", sender_line=None)
    assert "sender=" not in str(seen["body"])


@pytest.mark.parametrize(
    ("provider_status", "expected_error"),
    [
        (401, SmsProviderAuthError),
        (403, SmsProviderAuthError),
        (411, SmsProviderRejectedError),  # invalid receptor
        (418, SmsProviderRejectedError),  # insufficient credit
        (413, SmsProviderRejectedError),  # empty / too long
    ],
)
def test_kavenegar_send_maps_provider_status_codes(
    provider_status: int, expected_error: type[Exception]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"return": {"status": provider_status, "message": "خطا"}, "entries": []},
        )

    client = KavenegarSmsClient(api_key="test-key", transport=httpx.MockTransport(handler))
    with pytest.raises(expected_error):
        client.send(to="09123456789", message="یادآوری", sender_line=None)


def test_kavenegar_send_200_without_entries_is_invalid_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"return": {"status": 200, "message": "ok"}, "entries": []})

    client = KavenegarSmsClient(api_key="test-key", transport=httpx.MockTransport(handler))
    with pytest.raises(SmsProviderInvalidResponseError):
        client.send(to="09123456789", message="یادآوری", sender_line=None)


def test_kavenegar_http_errors_map_to_typed_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = KavenegarSmsClient(api_key="test-key", transport=httpx.MockTransport(handler))
    with pytest.raises(SmsProviderUnreachableError):
        client.send(to="09123456789", message="یادآوری", sender_line=None)


def test_kavenegar_http_401_maps_to_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    client = KavenegarSmsClient(api_key="test-key", transport=httpx.MockTransport(handler))
    with pytest.raises(SmsProviderAuthError):
        client.send(to="09123456789", message="یادآوری", sender_line=None)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (1, SmsDeliveryStatus.QUEUED),
        (2, SmsDeliveryStatus.QUEUED),
        (4, SmsDeliveryStatus.SENT),
        (6, SmsDeliveryStatus.FAILED),
        (10, SmsDeliveryStatus.DELIVERED),
        (11, SmsDeliveryStatus.FAILED),
        (13, SmsDeliveryStatus.FAILED),
        (14, SmsDeliveryStatus.FAILED),
        (100, SmsDeliveryStatus.UNKNOWN),
        (999, SmsDeliveryStatus.UNKNOWN),
    ],
)
def test_kavenegar_status_mapping(code: int, expected: SmsDeliveryStatus) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "return": {"status": 200, "message": "ok"},
                "entries": [{"messageid": 1, "status": code, "statustext": "x"}],
            },
        )

    client = KavenegarSmsClient(api_key="test-key", transport=httpx.MockTransport(handler))
    update = client.get_status("1")
    assert update.status == expected
    assert update.provider_message_id == "1"


def test_kavenegar_requires_api_key() -> None:
    with pytest.raises(SmsProviderNotConfiguredError):
        KavenegarSmsClient(api_key="  ")


def _sms_ir_success_payload() -> dict[str, object]:
    return {
        "status": 1,
        "message": "موفق",
        "data": {
            "packId": "550e8400-e29b-41d4-a716-446655440000",
            "messageIds": [987654],
            "cost": 123.5,
        },
    }


def test_sms_ir_send_success_is_structural_and_cost_unverified() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["header"] = request.headers.get("x-api-key")
        seen["json"] = json.loads(request.content.decode())
        return httpx.Response(200, json=_sms_ir_success_payload())

    client = SmsIrSmsClient(
        api_key="test-key", line_number=3000123456, transport=httpx.MockTransport(handler)
    )
    result = client.send(to="09123456789", message="یادآوری", sender_line="ignored")
    assert result.provider_message_id == "987654"
    assert result.status == SmsDeliveryStatus.QUEUED
    assert result.provider_status_raw == "packId=550e8400-e29b-41d4-a716-446655440000"
    assert result.cost_toman is None  # unit unverified — never converted
    assert result.cost_raw == "123.5"
    assert result.cost_unit == "unverified"
    assert seen["path"] == "/v1/send/bulk"
    assert seen["header"] == "test-key"
    assert seen["json"] == {
        "lineNumber": 3000123456,
        "messageText": "یادآوری",
        "mobiles": ["09123456789"],
    }


def test_sms_ir_send_rejects_empty_message_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": 0, "message": "خطا", "data": {"packId": "x", "messageIds": []}},
        )

    client = SmsIrSmsClient(
        api_key="test-key", line_number=3000123456, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(SmsProviderRejectedError):
        client.send(to="09123456789", message="یادآوری", sender_line=None)


def test_sms_ir_send_rejects_missing_data() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 0, "message": "خطا"})

    client = SmsIrSmsClient(
        api_key="test-key", line_number=3000123456, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(SmsProviderRejectedError):
        client.send(to="09123456789", message="یادآوری", sender_line=None)


def test_sms_ir_get_status_always_unknown_with_raw_state() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": 1,
                "message": "ok",
                "data": {"messageId": 987654, "deliveryState": 3, "cost": 123.5},
            },
        )

    client = SmsIrSmsClient(
        api_key="test-key", line_number=3000123456, transport=httpx.MockTransport(handler)
    )
    update = client.get_status("987654")
    assert update.status == SmsDeliveryStatus.UNKNOWN  # deliveryState values are UNVERIFIED
    assert update.provider_status_raw == "deliveryState=3"
    assert update.provider_message_id == "987654"


def test_simulator_returns_fake_id_and_sends_nothing() -> None:
    client = SimulatorSmsClient()
    result = client.send(to="09123456789", message="یادآوری", sender_line=None)
    assert result.provider_message_id.startswith("sim-")
    assert len(result.provider_message_id) == 16  # sim- + 12 hex
    assert result.status == SmsDeliveryStatus.SENT
    assert result.provider_status_raw == "simulated"
    assert result.cost_toman is None
    assert client.get_status(result.provider_message_id).status == SmsDeliveryStatus.SENT


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------


def test_factory_simulator_needs_no_credentials() -> None:
    factory = DefaultSmsClientFactory()
    client = factory.client_for(
        provider_kind="simulator",
        provider_key="simulator",
        credentials={},
        line_number=None,
    )
    assert isinstance(client, SimulatorSmsClient)


def test_factory_kavenegar_requires_api_key() -> None:
    factory = DefaultSmsClientFactory()
    with pytest.raises(SmsProviderNotConfiguredError):
        factory.client_for(
            provider_kind="kavenegar",
            provider_key="kavenegar",
            credentials={},
            line_number=None,
        )
    client = factory.client_for(
        provider_kind="kavenegar",
        provider_key="kavenegar",
        credentials={"api_key": "k"},
        line_number=None,
    )
    assert isinstance(client, KavenegarSmsClient)


def test_factory_sms_ir_requires_api_key_and_numeric_line() -> None:
    factory = DefaultSmsClientFactory()
    with pytest.raises(SmsProviderNotConfiguredError):
        factory.client_for(
            provider_kind="sms_ir",
            provider_key="sms_ir",
            credentials={"api_key": "k"},
            line_number=None,
        )
    with pytest.raises(SmsProviderNotConfiguredError):
        factory.client_for(
            provider_kind="sms_ir",
            provider_key="sms_ir",
            credentials={"api_key": "k"},
            line_number="3000-ABC",
        )
    client = factory.client_for(
        provider_kind="sms_ir",
        provider_key="sms_ir",
        credentials={"api_key": "k"},
        line_number="3000123456",
    )
    assert isinstance(client, SmsIrSmsClient)


def test_factory_custom_and_unknown_kinds_have_no_client() -> None:
    factory = DefaultSmsClientFactory()
    for kind in ("custom", "telegram", ""):
        with pytest.raises(SmsProviderClientUnavailableError):
            factory.client_for(
                provider_kind=kind,
                provider_key=kind or "x",
                credentials={},
                line_number=None,
            )


def test_factory_rejects_non_http_base_url_override() -> None:
    factory = DefaultSmsClientFactory()
    with pytest.raises(SmsProviderRejectedError):
        factory.client_for(
            provider_kind="kavenegar",
            provider_key="kavenegar",
            credentials={"api_key": "k", "base_url": "ftp://evil.example"},
            line_number=None,
        )
