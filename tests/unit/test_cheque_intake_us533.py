"""US-533: site intake — cheque.received.registered / .cancelled.

Covers the wiring the story adds on top of US-530/US-531:
the canonical names ingestible through the generic store
connector (webhook mapping + poll/sync mapping + payload
validation at the adapter boundary), the three-way mismatch
matrix, the same-claim convergence rule, the scan filestore,
the service-level intake/cancellation/verification behaviour
(no journal at intake), the daily-job review filings and the
intake queue. Live end-to-end coverage is in
tests/contract/test_cheque_intake_us533.py.
"""

from __future__ import annotations

import datetime
import http.server
import threading
from dataclasses import dataclass
from typing import Any, cast

import pytest

from hesabyar.application.commands.cheque_intake import SiteChequeIntakeHandler
from hesabyar.application.commands.cheques import ChequeCommandService
from hesabyar.application.queries.cheques import ChequeQueryService
from hesabyar.domain.accounting.refs import CompanyRef
from hesabyar.domain.accounting.value_objects import CompanyView
from hesabyar.domain.cheques.errors import ChequeDuplicateError
from hesabyar.domain.cheques.mismatch import (
    CHEQUE_REVIEW_SUBTYPES,
    SourceReading,
    evaluate_three_way_mismatch,
)
from hesabyar.domain.cheques.models import (
    Cheque,
    ChequeSettings,
    ChequeState,
)
from hesabyar.domain.cheques.ports import ChequeScanDownloadError
from hesabyar.domain.cheques.validation import same_cheque_claim, validate_registration_payload
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.events.envelope import CanonicalEvent, CanonicalEventType
from hesabyar.domain.events.errors import (
    InvalidEventEnvelopeError,
    MappingUnresolvedError,
)
from hesabyar.domain.events.models import NormalizedEvent, ResolvedMapping, StoreSettings
from hesabyar.domain.events.store_webhooks import (
    map_store_webhook,
    validate_store_payload,
)
from hesabyar.domain.events.sync import (
    StoreChange,
    StoreEntityKind,
    map_store_change,
)
from hesabyar.infrastructure.files.local_scan_store import LocalChequeScanStore
from tests.event_fixtures import raw_envelope
from tests.unit.test_cheque_services import (
    ACTOR,
    COMPANY_ENGINE_ID,
    INSTALLATION_ID,
    TENANT_ID,
    FakeChequeRepository,
    FakeEngine,
    RecordingReview,
    StubGate,
    _held,
    _payload,
    _register,
)

# ---------------------------------------------------------------------------
# Wiring: the canonical names are ingestible through the store connector
# ---------------------------------------------------------------------------


def test_cheque_topics_map_to_canonical_types() -> None:
    registered = map_store_webhook("cheque.registered")
    assert registered.pending_adr is False
    assert registered.canonical_types == (
        CanonicalEventType.CHEQUE_RECEIVED_REGISTERED,
    )
    cancelled = map_store_webhook("cheque.cancelled")
    assert cancelled.pending_adr is False
    assert cancelled.canonical_types == (
        CanonicalEventType.CHEQUE_RECEIVED_CANCELLED,
    )


def test_canonical_entity_for_cheque_events() -> None:
    """Webhook and poll share the store event id, so ingest
    idempotency dedups a redelivery of the same registration."""
    change = StoreChange(
        source_system="hayat",
        source_store_id="store-1",
        entity_type=StoreEntityKind.CHEQUE,
        entity_id="cheque-1",
        event_id="evt-9",
        event_name="cheque.registered",
        updated_at=datetime.datetime.now(datetime.UTC),
        status="completed",
        payload=_payload(),
    )
    mapped = map_store_change(change)
    assert len(mapped) == 1
    assert mapped[0].event.external_entity_type == "cheque"
    assert mapped[0].event.external_entity_id == "evt-9"
    # A cancellation is its own entity — it must NOT be suppressed
    # by the registration's mapping.
    cancelled = StoreChange(
        source_system="hayat",
        source_store_id="store-1",
        entity_type=StoreEntityKind.CHEQUE,
        entity_id="1234567890123456",
        event_id="evt-10",
        event_name="cheque.cancelled",
        updated_at=datetime.datetime.now(datetime.UTC),
        status="completed",
        payload={"sayad_id": "1234567890123456"},
    )
    mapped = map_store_change(cancelled)
    assert mapped[0].event.external_entity_type == "cheque_cancellation"
    assert mapped[0].event.external_entity_id == "1234567890123456"


def test_poll_sync_maps_cheque_cancelled_too() -> None:
    change = StoreChange(
        source_system="hayat",
        source_store_id="store-1",
        entity_type=StoreEntityKind.CHEQUE,
        entity_id="1234567890123456",
        event_id="evt-11",
        event_name="cheque.cancelled",
        updated_at=datetime.datetime.now(datetime.UTC),
        status="completed",
        payload={"sayad_id": "1234567890123456", "reason": "issuer withdrew"},
    )
    mapped = map_store_change(change)
    assert len(mapped) == 1
    assert mapped[0].event_type == CanonicalEventType.CHEQUE_RECEIVED_CANCELLED
    assert mapped[0].event.external_entity_type == "cheque_cancellation"


# ---------------------------------------------------------------------------
# Payload validation at the adapter boundary (fail-closed)
# ---------------------------------------------------------------------------


def test_validate_registration_payload_happy_path() -> None:
    validate_store_payload(
        CanonicalEventType.CHEQUE_RECEIVED_REGISTERED,
        _payload(),
        external_entity_type="cheque",
        external_entity_id="evt-1",
    )
    registration = validate_registration_payload(_payload())
    assert registration.amount_irr == 17_500_000  # Toman -> Rial x10
    assert registration.sayad_id == "1234567890123456"


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"sayad_id": "123456789012345"}, "sayad_id"),  # 15 digits
        ({"sayad_id": "123456789012345a"}, "sayad_id"),  # non-digit
        ({"issuer_national_id": "123456789"}, "issuer_national_id"),  # 9 digits
        ({"amount_toman": 0}, "amount_toman"),
        ({"amount_toman": -5}, "amount_toman"),
        ({"due_date": "1405-08-20"}, "due_date"),  # before issue 1405-08-26
        ({"purpose": "gift"}, "purpose"),
        ({"sayad_registered": "yes"}, "sayad_registered"),
    ],
    ids=[
        "sayad-short",
        "sayad-nondigit",
        "issuer-short",
        "amount-zero",
        "amount-negative",
        "due-before-issue",
        "purpose-other",
        "sayad-not-bool",
    ],
)
def test_validate_registration_payload_rejects(override: dict, field: str) -> None:
    with pytest.raises(InvalidEventEnvelopeError) as exc_info:
        validate_store_payload(
            CanonicalEventType.CHEQUE_RECEIVED_REGISTERED,
            _payload(**override),
            external_entity_type="cheque",
            external_entity_id="evt-1",
        )
    assert field in str(exc_info.value.details)


def test_validate_registration_payload_requires_cheque_entity() -> None:
    with pytest.raises(InvalidEventEnvelopeError, match="cheque"):
        validate_store_payload(
            CanonicalEventType.CHEQUE_RECEIVED_REGISTERED,
            _payload(),
            external_entity_type="order",
            external_entity_id="order-1",
        )


def test_validate_cancellation_payload() -> None:
    validate_store_payload(
        CanonicalEventType.CHEQUE_RECEIVED_CANCELLED,
        {"sayad_id": "1234567890123456", "reason": "issuer withdrew"},
        external_entity_type="cheque_cancellation",
        external_entity_id="1234567890123456",
    )
    with pytest.raises(InvalidEventEnvelopeError, match="sayad_id"):
        validate_store_payload(
            CanonicalEventType.CHEQUE_RECEIVED_CANCELLED,
            {"sayad_id": "123"},
            external_entity_type="cheque_cancellation",
            external_entity_id="123",
        )


# ---------------------------------------------------------------------------
# Three-way mismatch matrix (site vs leaf vs Sayyad)
# ---------------------------------------------------------------------------

AMOUNT = 17_500_000
DUE = datetime.date(2026, 12, 5)


def _reading(amount: int | None = AMOUNT, due: datetime.date | None = DUE) -> SourceReading:
    return SourceReading(amount_irr=amount, due_date=due)


def test_three_way_all_agree_is_no_mismatch() -> None:
    result = evaluate_three_way_mismatch(site=_reading(), leaf=_reading(), sayad=_reading())
    assert not result.has_mismatch
    assert result.mismatched_fields == ()


def test_three_way_unknown_is_not_a_mismatch() -> None:
    # Sayyad has no reading yet (inquiry not run) — unknown is not
    # a contradiction; the leaf still needs its own confirmation.
    result = evaluate_three_way_mismatch(site=_reading(), leaf=_reading(), sayad=SourceReading())
    assert not result.has_mismatch
    result = evaluate_three_way_mismatch(site=_reading(), leaf=SourceReading(), sayad=SourceReading())
    assert not result.has_mismatch


def test_three_way_amount_mismatch() -> None:
    result = evaluate_three_way_mismatch(
        site=_reading(), leaf=_reading(amount=18_000_000), sayad=_reading()
    )
    assert result.has_mismatch
    assert result.mismatched_fields == ("amount",)
    assert "18,000,000" in result.describe() or "18000000" in result.describe()


def test_three_way_due_date_mismatch() -> None:
    result = evaluate_three_way_mismatch(
        site=_reading(),
        leaf=_reading(),
        sayad=_reading(due=datetime.date(2026, 12, 6)),
    )
    assert result.has_mismatch
    assert result.mismatched_fields == ("due_date",)


def test_three_way_both_fields_mismatch() -> None:
    result = evaluate_three_way_mismatch(
        site=_reading(amount=1, due=datetime.date(2026, 1, 1)),
        leaf=_reading(amount=2, due=datetime.date(2026, 1, 2)),
        sayad=_reading(amount=3, due=datetime.date(2026, 1, 3)),
    )
    assert result.mismatched_fields == ("amount", "due_date")


def test_review_subtypes_cover_the_matrix() -> None:
    assert set(CHEQUE_REVIEW_SUBTYPES) == {
        "three_way_mismatch",
        "duplicate_sayad_id",
        "cancellation_after_held",
        "stale_in_clearing",
        "never_arrived_cheque",
        "ambiguous_matching",
    }


# ---------------------------------------------------------------------------
# Same-claim convergence rule
# ---------------------------------------------------------------------------


def test_same_claim_identical_payload() -> None:
    registration = validate_registration_payload(_payload())
    cheque = _register(_service(_world()))
    assert same_cheque_claim(registration, cheque)


def test_same_claim_amount_or_date_differs() -> None:
    cheque = _register(_service(_world()))
    assert not same_cheque_claim(
        validate_registration_payload(_payload(amount_toman=9990000)), cheque
    )
    assert not same_cheque_claim(
        validate_registration_payload(_payload(due_date="1405-11-15")), cheque
    )


def test_same_claim_optional_fields_only_when_both_present() -> None:
    # A leaf number on one side only is not a contradiction.
    cheque = _register(_service(_world()))
    assert same_cheque_claim(
        validate_registration_payload(_payload(leaf_number="L-1")), cheque
    )
    # ... but contradicting leaf numbers are.
    cheque.leaf_number = "L-2"
    assert not same_cheque_claim(
        validate_registration_payload(_payload(leaf_number="L-1")), cheque
    )


# ---------------------------------------------------------------------------
# Scan filestore
# ---------------------------------------------------------------------------

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6300000002000100e50f5d0000000049454e44ae426082"
)


def _serve_png(tmp_path) -> tuple[str, threading.Thread]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib name
            if self.path == "/scan.png":
                body = _PNG
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{server.server_port}/scan.png", thread


def test_scan_store_downloads_into_filestore(tmp_path) -> None:
    url, _ = _serve_png(tmp_path)
    store = LocalChequeScanStore(tmp_path / "filestore")
    ref = store.store_scan(sayad_id="1234567890123456", scan_url=url)
    assert ref.startswith("cheques/1234567890123456-")
    assert ref.endswith(".png")
    assert (tmp_path / "filestore" / ref).is_file()


def test_scan_store_rejects_bad_content_type(tmp_path) -> None:
    url, _ = _serve_png(tmp_path)
    store = LocalChequeScanStore(tmp_path / "filestore")
    with pytest.raises(ChequeScanDownloadError):
        store.store_scan(sayad_id="1234567890123456", scan_url=url.replace("scan.png", "missing.png"))
    with pytest.raises(ChequeScanDownloadError):
        store.store_scan(sayad_id="1234567890123456", scan_url="ftp://example.invalid/scan.png")


# ---------------------------------------------------------------------------
# Service: register_site / cancel / verify / daily job (fakes)
# ---------------------------------------------------------------------------


@dataclass
class PayeeAwareEngine(FakeEngine):
    """FakeEngine that also answers list_companies (payee check)."""

    company_national_id: str | None = "00123456789"

    def list_companies(self) -> list[CompanyView]:
        return [
            CompanyView(
                ref=CompanyRef(engine_id=COMPANY_ENGINE_ID),
                name="کافه ایوان",
                currency_code="IRR",
                national_id=self.company_national_id,
            )
        ]


def _world(engine: FakeEngine | None = None) -> tuple[FakeChequeRepository, FakeEngine, RecordingReview]:
    return FakeChequeRepository(), engine or PayeeAwareEngine(), RecordingReview()


def _service(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
    *,
    scan_store: LocalChequeScanStore | None = None,
    gate: StubGate | None = None,
    today: datetime.date | None = None,
) -> ChequeCommandService:
    repo, engine, review = world
    return ChequeCommandService(
        repo,
        cast(Any, engine),
        gate=cast(Any, gate) if gate is not None else None,
        review=cast(Any, review),
        scan_store=scan_store,
        today=today,
    )


def _register_site(
    service: ChequeCommandService, **overrides: object
) -> Cheque:
    return service.register_site(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_payload(**overrides),
        actor="system:store-intake:hayat",
        source_event_id="evt-1",
    )


def test_register_site_is_a_claim_with_no_journal() -> None:
    service = _service(_world())
    cheque = _register_site(service)
    assert cheque.state is ChequeState.REGISTERED
    assert cheque.source == "site"
    assert cheque.source_event_id == "evt-1"
    assert cheque.move_ids == {}  # intake is a claim — no journal
    assert cheque.engine_id is not None


def test_register_site_unregistered_sayad_flags_warning() -> None:
    service = _service(_world())
    cheque = _register_site(service, sayad_registered=False)
    assert cheque.sayad_unconfirmed is True
    assert cheque.sayad_status.value == "unregistered"


def test_register_site_payee_mismatch_is_rejected() -> None:
    world = _world(PayeeAwareEngine(company_national_id="98765432109"))
    service = _service(world)
    with pytest.raises(MappingUnresolvedError, match="another business"):
        _register_site(service)
    assert world[0].cheques == {}  # nothing was created


def test_register_site_company_without_national_id_fails_closed() -> None:
    world = _world(PayeeAwareEngine(company_national_id=None))
    service = _service(world)
    with pytest.raises(MappingUnresolvedError, match="no national id"):
        _register_site(service)


def test_register_site_idempotent_replay_and_conflict() -> None:
    world = _world()
    service = _service(world)
    first = _register_site(service)
    again = _register_site(service)
    assert again.id == first.id
    assert len(world[0].cheques) == 1
    with pytest.raises(ChequeDuplicateError):
        _register_site(service, amount_toman=9990000)
    assert world[2].items[-1]["subtype"] == "duplicate_sayad_id"
    assert len(world[0].cheques) == 1  # still exactly one record


def test_register_site_downloads_scan_best_effort(tmp_path) -> None:
    url, _ = _serve_png(tmp_path)
    world = _world()
    service = _service(world, scan_store=LocalChequeScanStore(tmp_path / "filestore"))
    cheque = _register_site(service, scan_url=url)
    assert cheque.scan_filestore_ref is not None
    assert cheque.scan_filestore_ref.startswith("cheques/1234567890123456-")
    assert cheque.scan_url == url  # the site URL stays on the record


def test_register_site_scan_failure_never_fails_intake(tmp_path) -> None:
    world = _world()
    service = _service(world, scan_store=LocalChequeScanStore(tmp_path / "filestore"))
    cheque = _register_site(
        service, scan_url="http://127.0.0.1:1/scan.png"
    )
    assert cheque.state is ChequeState.REGISTERED
    assert cheque.scan_filestore_ref is None
    assert cheque.scan_url == "http://127.0.0.1:1/scan.png"


def test_cancel_site_registration_before_held_voids() -> None:
    world = _world()
    service = _service(world)
    cheque = _register_site(service)
    outcome = service.cancel_site_registration(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        sayad_id=cheque.sayad_id,
        actor="system:store-intake:hayat",
        reason="issuer withdrew",
    )
    assert outcome.outcome == "voided"
    assert outcome.cheque.state is ChequeState.VOIDED
    assert outcome.cheque.move_ids == {}  # voiding an intake: no journal
    # The sayad_id is free again — a new registration is a new record.
    again = _register_site(service)
    assert again.id != cheque.id


def test_cancel_site_registration_after_held_files_review() -> None:
    world = _world()
    service = _service(world)
    cheque = _held(service)
    outcome = service.cancel_site_registration(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        sayad_id=cheque.sayad_id,
        actor="system:store-intake:hayat",
    )
    assert outcome.outcome == "review_filed"
    assert outcome.cheque.state is ChequeState.HELD  # never auto-voided
    assert world[2].items[-1]["subtype"] == "cancellation_after_held"


def test_cancel_site_registration_unknown_sayad_is_unresolved() -> None:
    service = _service(_world())
    with pytest.raises(MappingUnresolvedError, match="no cheque with that id"):
        service.cancel_site_registration(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            sayad_id="9999999999999999",
            actor="system:store-intake:hayat",
        )


def test_cancel_site_registration_already_terminal_is_noop() -> None:
    world = _world()
    service = _service(world)
    cheque = _register_site(service)
    service.cancel_site_registration(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        sayad_id=cheque.sayad_id,
        actor="system:store-intake:hayat",
    )
    outcome = service.cancel_site_registration(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        sayad_id=cheque.sayad_id,
        actor="system:store-intake:hayat",
    )
    assert outcome.outcome == "already_terminal"


def test_verify_leaf_match_then_mismatch() -> None:
    world = _world()
    service = _service(world)
    cheque = _register_site(service)
    # Leaf agrees with the site claim — the record is unchanged;
    # the caller then confirms receipt.
    same = service.verify_leaf_against_sources(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        leaf_amount_irr=17_500_000,
        leaf_due_date="1405-10-15",
    )
    assert same.state is ChequeState.REGISTERED
    # The leaf disagrees on amount — never confirm: the intake is
    # rejected and a three_way_mismatch review item is filed.
    rejected = service.verify_leaf_against_sources(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        leaf_amount_irr=18_000_000,
        leaf_due_date="1405-10-15",
    )
    assert rejected.state is ChequeState.REJECTED_MISMATCH
    assert world[0].cheques[cheque.id].state is ChequeState.REJECTED_MISMATCH
    assert world[2].items[-1]["subtype"] == "three_way_mismatch"


def test_file_ambiguous_matching() -> None:
    world = _world()
    service = _service(world)
    cheque = _register_site(service)
    service.file_ambiguous_matching(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        candidate_cheque_ids=("cand-1", "cand-2"),
    )
    assert world[2].items[-1]["subtype"] == "ambiguous_matching"


def test_daily_job_files_never_arrived_on_expiry() -> None:
    world = _world()
    repo = world[0]
    service = _service(world)
    cheque = _register_site(service)
    cheque.created_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=7)
    repo.cheques[cheque.id] = cheque
    report = service.run_daily_cheque_job(datetime.date(2026, 11, 19))
    assert report.expired == 1
    assert any(
        item["subtype"] == "never_arrived_cheque" for item in world[2].items
    )


def test_daily_job_files_stale_in_clearing_once() -> None:
    world = _world()
    repo = world[0]
    repo.settings[INSTALLATION_ID] = ChequeSettings(
        installation_id=INSTALLATION_ID, clearing_window_days=3
    )
    service = _service(world)
    cheque = _register_site(service)
    # Move the cheque to in-clearing with a due date 5 days past.
    cheque.state = ChequeState.IN_CLEARING
    cheque.due_date = FiscalDate(datetime.date(2026, 11, 14))
    repo.cheques[cheque.id] = cheque
    first = service.run_daily_cheque_job(datetime.date(2026, 11, 19))
    assert first.stale_in_clearing_filed == 1
    assert any(
        item["subtype"] == "stale_in_clearing" for item in world[2].items
    )
    assert repo.cheques[cheque.id].state is ChequeState.IN_CLEARING  # never auto-bounced
    second = service.run_daily_cheque_job(datetime.date(2026, 11, 19))
    assert second.stale_in_clearing_filed == 0  # filed once per cheque


def test_intake_queue_is_registered_oldest_first() -> None:
    world = _world()
    repo = world[0]
    service = _service(world)
    old = _register_site(service, sayad_id="1111111111111111")
    new = _register_site(service, sayad_id="2222222222222222")
    old.created_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=2)
    repo.cheques[old.id] = old
    query = ChequeQueryService(repo)
    queue = query.list_intake_queue(
        tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    )
    assert [c.id for c in queue] == [old.id, new.id]


# ---------------------------------------------------------------------------
# Pipeline handler adapter (no DB, no engine beyond the fakes)
# ---------------------------------------------------------------------------


def _normalized(payload: dict, event_type: CanonicalEventType) -> NormalizedEvent:
    entity = "cheque" if event_type is CanonicalEventType.CHEQUE_RECEIVED_REGISTERED else "cheque_cancellation"
    entity_id = "evt-1" if entity == "cheque" else payload["sayad_id"]
    envelope = CanonicalEvent.from_raw(
        raw_envelope(
            payload=payload,
            external_entity_type=entity,
            external_entity_id=entity_id,
        )
    )
    return NormalizedEvent(
        event=envelope,
        event_type=event_type,
        store_settings=StoreSettings(source_system="hayat", source_store_id="store-1"),
        converted_payload=payload,
    )


def test_site_intake_handler_registers_and_cancels() -> None:
    world = _world()
    service = _service(world)
    handler = SiteChequeIntakeHandler(service)
    mapping = ResolvedMapping(
        mapping_version="1", company=CompanyRef(engine_id=COMPANY_ENGINE_ID)
    )
    outcome = handler.register_site_cheque(
        event=_normalized(_payload(), CanonicalEventType.CHEQUE_RECEIVED_REGISTERED),
        mapping=mapping,
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
    )
    assert outcome.engine_cheque_id is not None
    assert outcome.amount_irr == 17_500_000
    assert "no journal" in outcome.detail
    cancelled = handler.cancel_site_cheque(
        event=_normalized(
            {"sayad_id": "1234567890123456"},
            CanonicalEventType.CHEQUE_RECEIVED_CANCELLED,
        ),
        mapping=mapping,
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
    )
    assert "voided" in cancelled.detail
    assert world[0].cheques[
        next(iter(world[0].cheques))
    ].state is ChequeState.VOIDED


def test_site_intake_handler_duplicate_conflict_is_an_outcome() -> None:
    world = _world()
    service = _service(world)
    handler = SiteChequeIntakeHandler(service)
    mapping = ResolvedMapping(
        mapping_version="1", company=CompanyRef(engine_id=COMPANY_ENGINE_ID)
    )
    handler.register_site_cheque(
        event=_normalized(_payload(), CanonicalEventType.CHEQUE_RECEIVED_REGISTERED),
        mapping=mapping,
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
    )
    outcome = handler.register_site_cheque(
        event=_normalized(
            _payload(amount_toman=9990000),
            CanonicalEventType.CHEQUE_RECEIVED_REGISTERED,
        ),
        mapping=mapping,
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
    )
    assert outcome.duplicate_conflict is True
    assert outcome.engine_cheque_id is not None  # the existing cheque
    assert len(world[0].cheques) == 1
