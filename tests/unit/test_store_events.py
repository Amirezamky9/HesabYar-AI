"""Store event resolver + builder + pipeline (US-511).

The store connector end to end through the in-memory fakes in
``tests/event_fixtures.py``: registry-backed mapping resolution,
document building (invoice / cancel / credit note / payment), the
§10.3 pipeline dispatch for each document kind, ingest fan-out and
the registry-backed webhook secret store. No database, no server.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from hesabyar.application.commands.events import (
    EventIngestService,
    EventPipelineService,
    normalize_event,
)
from hesabyar.application.commands.store_events import StoreDraftBuilder, StoreMappingResolver
from hesabyar.domain.accounting.commands import (
    CreateInvoiceDraft,
)
from hesabyar.domain.accounting.refs import AccountRef, CompanyRef, PartyRef, PaymentJournalRef, ProductRef
from hesabyar.domain.events.envelope import CanonicalEvent, CanonicalEventType
from hesabyar.domain.events.errors import (
    EventTypePendingAdrError,
    InvalidEventEnvelopeError,
    MappingUnresolvedError,
    WebhookVaultUnavailableError,
)
from hesabyar.domain.events.models import (
    ExternalIdMapping,
    InboxEventStatus,
    IncomingEvent,
    NormalizedEvent,
    PipelineOutcome,
    StoreSettings,
)
from hesabyar.domain.integrations.commands import CreateIntegrationMapping, CreateStore
from hesabyar.domain.integrations.models import MappingKind
from hesabyar.infrastructure.events.in_memory_queue import InMemoryJobQueue
from hesabyar.infrastructure.events.store_secrets import RegistryWebhookSecretStore
from tests.event_fixtures import (
    INSTALLATION_ID,
    TENANT_ID,
    FakeAuditLog,
    FakeEngine,
    FakeExternalIdMap,
    FakeIdempotencyStore,
    FakeInboxRepository,
    FakeOutboxRepository,
    FakePolicyGate,
    FakeReviewQueue,
    FakeStoreSettings,
    FakeWebhookSecrets,
    must_get,
    raw_envelope,
)
from tests.unit.test_integrations_services import FakeIntegrationsRepository

# ---------------------------------------------------------------------------
# Payloads (Toman — the store prices in Toman, ×10 at the boundary)
# ---------------------------------------------------------------------------


def order_payload(**overrides: Any) -> dict[str, Any]:
    """A valid order.completed payload: 2×10000 + 1×5000, shipping 3000,
    discount 1000, tax 500, total 27500 Toman."""
    payload: dict[str, Any] = {
        "order_id": "order-1",
        "customer": {"id": "cust-1", "email": "a@b.ir"},
        "lines": [
            {
                "product_sku": "SKU-1",
                "quantity": 2,
                "unit_price": 10000,
                "line_total": 20000,
                "revenue_category": "electronics",
            },
            {"product_sku": "SKU-2", "quantity": 1, "unit_price": 5000},
        ],
        "subtotal_amount": 25000,
        "shipping_amount": 3000,
        "discount_amount": 1000,
        "tax_amount": 500,
        "total_amount": 27500,
        "status": "completed",
    }
    payload.update(overrides)
    return payload


def cancel_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"order_id": "order-1", "reason": "customer request"}
    payload.update(overrides)
    return payload


def refund_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "refund_id": "refund-1",
        "order_id": "order-1",
        "refund_amount": 5000,
        "payment_channel": "zarinpal",
        "gross_amount": 5000,
    }
    payload.update(overrides)
    return payload


def capture_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "payment_channel": "zarinpal",
        "gateway_payment_id": "pay-1",
        "gross_amount": 27500,
        "fee_amount": 500,
        "net_amount": 27000,
        "order_id": "order-1",
        "customer": {"id": "cust-1"},
        "verified": True,
    }
    payload.update(overrides)
    return payload


def payment_refund_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "refund_id": "refund-1",
        "gross_amount": 5000,
        "payment_channel": "zarinpal",
        "gateway_payment_id": "pay-1",
        "order_id": "order-1",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------


def add_mapping(
    registry: FakeIntegrationsRepository,
    mapping_kind: MappingKind,
    channel_key: str,
    source_key: str,
    target_type: str,
    target_ref: str,
    version: int = 1,
) -> None:
    """Seed one mapping (the fake resolves the highest version)."""
    registry.save_mapping(
        CreateIntegrationMapping(
            installation_id=INSTALLATION_ID,
            mapping_kind=mapping_kind,
            channel_key=channel_key,
            source_key=source_key,
            target_type=target_type,
            target_ref=target_ref,
            version=version,
        )
    )


def seed_registry(registry: FakeIntegrationsRepository) -> None:
    """Seed the mappings a store order/payment needs."""
    add_mapping(registry, MappingKind.STORE_COMPANY, "", "hayat:store-1", "company", "1")
    add_mapping(registry, MappingKind.EXTERNAL_PARTY, "", "cust-1", "party", "5")
    add_mapping(registry, MappingKind.REVENUE_CATEGORY, "", "electronics", "account", "4.1.1")
    add_mapping(registry, MappingKind.REVENUE_CATEGORY, "", "default", "account", "4.1.1")
    add_mapping(registry, MappingKind.SHIPPING_CHARGE, "", "default", "account", "4.1.2")
    add_mapping(registry, MappingKind.GATEWAY_JOURNAL, "zarinpal", "zarinpal", "journal", "7")


def add_store(
    registry: FakeIntegrationsRepository,
    store_key: str,
    *,
    webhook_secret_ciphertext: str = "",
) -> Any:
    """Seed one store; a non-empty ciphertext means it has a secret."""
    return registry.save_store(
        CreateStore(
            installation_id=INSTALLATION_ID,
            store_key=store_key,
            display_name=store_key,
            store_type="custom",
            prices_in_toman=True,
            webhook_secret_ciphertext=webhook_secret_ciphertext,
            api_key_ciphertext="",
        )
    )


class Harness:
    """A store pipeline with fakes + a seeded registry."""

    def __init__(self, *, gate_allowed: bool = False, seed: bool = True) -> None:
        self.engine = FakeEngine()
        self.inbox = FakeInboxRepository()
        self.outbox = FakeOutboxRepository()
        self.external_ids = FakeExternalIdMap()
        self.audit_log = FakeAuditLog()
        self.settings = FakeStoreSettings()
        self.registry = FakeIntegrationsRepository()
        self.review_queue = FakeReviewQueue()
        if seed:
            seed_registry(self.registry)
        self.pipeline = EventPipelineService(
            inbox=self.inbox,
            outbox=self.outbox,
            external_ids=self.external_ids,
            audit_log=self.audit_log,
            store_settings=self.settings,
            mapping_resolver=StoreMappingResolver(self.registry, self.external_ids, self.engine),
            draft_builder=StoreDraftBuilder(self.engine, self.external_ids),
            policy_gate=FakePolicyGate(allowed=gate_allowed),
            review_queue=self.review_queue,
            engine=self.engine,
        )

    def store_event(
        self,
        event_type: CanonicalEventType,
        payload: dict[str, Any],
        *,
        source_event_id: str = "evt-1",
        entity_type: str = "order",
        entity_id: str = "order-1",
        installation_id: str | None = INSTALLATION_ID,
    ) -> str:
        """Store one inbox event and return its id."""
        event = CanonicalEvent.from_raw(
            raw_envelope(
                source_event_id=source_event_id,
                payload=payload,
                external_entity_type=entity_type,
                external_entity_id=entity_id,
            )
        )
        outcome = self.inbox.store(
            event, event_type, TENANT_ID, installation_id=installation_id
        )
        assert outcome.status == "created"
        assert outcome.event is not None
        return outcome.event.id

    def process_order(
        self,
        expected_outcome: PipelineOutcome = PipelineOutcome.PROCESSED_DRAFT,
        **overrides: Any,
    ) -> str:
        """Store + process a completed order; return the invoice id (as str)."""
        source_event_id = str(overrides.pop("source_event_id", "evt-order-1"))
        event_id = self.store_event(
            CanonicalEventType.SALES_ORDER_COMPLETED,
            order_payload(**overrides),
            source_event_id=source_event_id,
        )
        result = self.pipeline.process(event_id)
        assert result.outcome == expected_outcome
        assert result.draft_ref is not None
        return result.draft_ref

    def invoice_for_order(self, order_id: str = "order-1"):
        """The invoice the order produced, via the §10.4 id map."""
        found = self.external_ids.get("hayat", "order", order_id)
        assert found is not None
        return self.engine.invoices[int(found.target_external_id)]


def normalized(
    event_type: CanonicalEventType,
    payload: dict[str, Any],
    *,
    installation_id: str | None = INSTALLATION_ID,
    entity_type: str = "order",
    entity_id: str = "order-1",
) -> NormalizedEvent:
    """Normalize one event the way the pipeline does (Toman→Rial)."""
    event = CanonicalEvent.from_raw(
        raw_envelope(
            payload=payload,
            external_entity_type=entity_type,
            external_entity_id=entity_id,
        )
    )
    return normalize_event(
        event,
        StoreSettings(source_system="hayat", source_store_id="store-1"),
        event_type=event_type,
        installation_id=installation_id,
    )


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def test_resolver_resolves_a_complete_order_mapping() -> None:
    registry = FakeIntegrationsRepository()
    seed_registry(registry)
    resolver = StoreMappingResolver(registry, FakeExternalIdMap(), FakeEngine())
    mapping = resolver.resolve(normalized(CanonicalEventType.SALES_ORDER_COMPLETED, order_payload()))

    assert mapping.is_complete
    assert mapping.company == CompanyRef(1)
    assert mapping.party == PartyRef(5)
    assert mapping.accounts["revenue:electronics"] == AccountRef(12)
    assert mapping.accounts["revenue:default"] == AccountRef(12)
    assert mapping.accounts["shipping"] == AccountRef(13)
    assert mapping.mapping_version.startswith("store_company:")


def test_resolver_fails_closed_without_installation_context() -> None:
    registry = FakeIntegrationsRepository()
    seed_registry(registry)
    resolver = StoreMappingResolver(registry, FakeExternalIdMap(), FakeEngine())
    mapping = resolver.resolve(
        normalized(
            CanonicalEventType.SALES_ORDER_COMPLETED,
            order_payload(),
            installation_id=None,
        )
    )
    assert not mapping.is_complete
    assert mapping.unresolved == ("installation_context",)


def test_resolver_collects_every_gap_at_once() -> None:
    # With only the company mapped, the party + revenue gaps surface
    # together (accounts cannot resolve before the company does).
    registry = FakeIntegrationsRepository()
    add_mapping(registry, MappingKind.STORE_COMPANY, "", "hayat:store-1", "company", "1")
    resolver = StoreMappingResolver(registry, FakeExternalIdMap(), FakeEngine())
    mapping = resolver.resolve(normalized(CanonicalEventType.SALES_ORDER_COMPLETED, order_payload()))
    assert not mapping.is_complete
    assert any(u.startswith("external_party:cust-1") for u in mapping.unresolved)
    assert "revenue_category:electronics" in mapping.unresolved
    assert "revenue_category:default" in mapping.unresolved
    assert "shipping_charge:default" in mapping.unresolved


def test_resolver_resolves_an_account_by_engine_id_ref() -> None:
    registry = FakeIntegrationsRepository()
    seed_registry(registry)
    # Version 2 supersedes the seeded version-1 mapping.
    add_mapping(
        registry,
        MappingKind.REVENUE_CATEGORY,
        "",
        "electronics",
        "account",
        "tryton:account:12",
        version=2,
    )
    resolver = StoreMappingResolver(registry, FakeExternalIdMap(), FakeEngine())
    mapping = resolver.resolve(normalized(CanonicalEventType.SALES_ORDER_COMPLETED, order_payload()))
    assert mapping.accounts["revenue:electronics"] == AccountRef(12)


def test_resolver_fails_closed_when_the_mapped_code_is_not_in_the_chart() -> None:
    registry = FakeIntegrationsRepository()
    seed_registry(registry)
    # Version 2 supersedes the seeded version-1 mapping.
    add_mapping(
        registry,
        MappingKind.REVENUE_CATEGORY,
        "",
        "electronics",
        "account",
        "9.9.9",
        version=2,
    )
    resolver = StoreMappingResolver(registry, FakeExternalIdMap(), FakeEngine())
    mapping = resolver.resolve(normalized(CanonicalEventType.SALES_ORDER_COMPLETED, order_payload()))
    assert not mapping.is_complete
    assert any("9.9.9" in u for u in mapping.unresolved)


def test_resolver_resolves_products_without_blocking() -> None:
    registry = FakeIntegrationsRepository()
    seed_registry(registry)
    external_ids = FakeExternalIdMap()
    external_ids.save(
        ExternalIdMapping(
            source_system="hayat",
            external_entity_type="product",
            external_entity_id="SKU-1",
            target_engine="tryton",
            target_entity_type="product.product",
            target_external_id="99",
            engine_company_id=None,
            tenant_id=TENANT_ID,
        )
    )
    resolver = StoreMappingResolver(registry, external_ids, FakeEngine())
    mapping = resolver.resolve(normalized(CanonicalEventType.SALES_ORDER_COMPLETED, order_payload()))
    # SKU-1 resolves; SKU-2 does not — and the mapping is still complete
    # (products are non-blocking on the invoice path).
    assert mapping.products == {"SKU-1": ProductRef(99)}
    assert mapping.is_complete


def test_resolver_resolves_a_payment_journal_from_the_channel() -> None:
    registry = FakeIntegrationsRepository()
    seed_registry(registry)
    resolver = StoreMappingResolver(registry, FakeExternalIdMap(), FakeEngine())
    mapping = resolver.resolve(
        normalized(
            CanonicalEventType.PAYMENT_CAPTURED,
            capture_payload(),
            entity_type="payment",
            entity_id="pay-1",
        )
    )
    assert mapping.is_complete
    assert mapping.payment_journal == PaymentJournalRef(7)
    assert mapping.party == PartyRef(5)


def test_resolver_fails_closed_for_an_unmapped_channel() -> None:
    registry = FakeIntegrationsRepository()
    seed_registry(registry)
    resolver = StoreMappingResolver(registry, FakeExternalIdMap(), FakeEngine())
    mapping = resolver.resolve(
        normalized(
            CanonicalEventType.PAYMENT_CAPTURED,
            capture_payload(payment_channel="sadad"),
            entity_type="payment",
            entity_id="pay-1",
        )
    )
    assert not mapping.is_complete
    assert "gateway_journal:sadad" in mapping.unresolved


# ---------------------------------------------------------------------------
# Builder (direct)
# ---------------------------------------------------------------------------


def test_builder_builds_an_invoice_draft_from_an_order() -> None:
    harness = Harness()
    event = normalized(CanonicalEventType.SALES_ORDER_COMPLETED, order_payload())
    mapping = StoreMappingResolver(
        harness.registry, harness.external_ids, harness.engine
    ).resolve(event)
    builder = StoreDraftBuilder(harness.engine, harness.external_ids)
    command = builder.build(event, mapping)

    assert isinstance(command, CreateInvoiceDraft)
    assert command.invoice_type == "out"
    assert command.party == PartyRef(5)
    assert command.company == CompanyRef(1)
    # Products (250000) + shipping (30000); tax/discount are NOT lines.
    assert len(command.lines) == 3
    assert command.total_amount.amount == 280000
    assert command.lines[2].account == AccountRef(13)
    assert "store total 275000 IRR" in command.description
    assert "tax 5000" in command.description
    assert "discount 10000" in command.description
    assert command.reference == "hayat:order-1"


def test_builder_rejects_a_non_store_event_type() -> None:
    builder = StoreDraftBuilder(FakeEngine(), FakeExternalIdMap())
    event = normalized(
        CanonicalEventType.BANK_TRANSACTION_IMPORTED,
        {},
        entity_type="bank_transaction",
        entity_id="txn-1",
    )
    with pytest.raises(MappingUnresolvedError):
        builder.build(event, mapping=_empty_mapping())


def _empty_mapping():
    from hesabyar.domain.events.models import ResolvedMapping

    return ResolvedMapping(mapping_version="test", unresolved=())


def test_builder_rejects_an_invalid_payload() -> None:
    harness = Harness()
    builder = StoreDraftBuilder(harness.engine, harness.external_ids)
    event = normalized(
        CanonicalEventType.SALES_ORDER_COMPLETED,
        order_payload(total_amount=99999),
    )
    with pytest.raises(InvalidEventEnvelopeError):
        builder.build(event, _empty_mapping())


# ---------------------------------------------------------------------------
# Ingest: store topics + fan-out
# ---------------------------------------------------------------------------


def _incoming(
    payload: dict[str, Any] | None = None,
    event_type: str | None = "order.completed",
    **overrides: Any,
) -> IncomingEvent:
    body = json.dumps(raw_envelope(payload=payload, **overrides)).encode()
    return IncomingEvent(connector="hayat", raw_body=body, event_type=event_type)


def _ingest_service(inbox: FakeInboxRepository | None = None) -> tuple[EventIngestService, FakeInboxRepository]:
    inbox = inbox or FakeInboxRepository()
    service = EventIngestService(
        inbox, FakeIdempotencyStore(), FakeWebhookSecrets(), InMemoryJobQueue()
    )
    return service, inbox


def test_ingest_with_a_store_topic_maps_to_the_canonical_type() -> None:
    service, inbox = _ingest_service()
    result = service.ingest(
        _incoming(order_payload(), external_entity_type="order", external_entity_id="order-1"),
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
    )
    assert result.status == "created"
    assert result.event.event_type == "sales.order.completed"
    assert result.event.installation_id == INSTALLATION_ID
    assert result.derived_events == ()
    assert inbox.count_events() == 1


def test_ingest_refund_created_fans_out_to_two_canonical_events() -> None:
    service, inbox = _ingest_service()
    result = service.ingest(
        _incoming(
            refund_payload(),
            event_type="refund.created",
            source_event_id="webhook-refund-1",
            external_entity_type="refund",
            external_entity_id="refund-1",
        ),
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
    )
    assert result.status == "created"
    assert result.event.event_type == "sales.refund.created"
    assert len(result.derived_events) == 1
    assert result.derived_events[0].event_type == "payment.refunded"
    assert result.derived_events[0].source_event_id == "webhook-refund-1:payment-refunded"
    assert result.derived_events[0].external_entity_type == "payment_refund"
    assert result.derived_events[0].external_entity_id == "refund-1"
    assert inbox.count_events() == 2


def test_ingest_refund_created_twice_replays_both_legs_as_duplicates() -> None:
    service, inbox = _ingest_service()
    request = _incoming(
        refund_payload(),
        event_type="refund.created",
        source_event_id="webhook-refund-1",
        external_entity_type="refund",
        external_entity_id="refund-1",
    )
    first = service.ingest(request, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    second = service.ingest(request, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert first.status == "created"
    assert second.status == "duplicate"
    assert second.event.id == first.event.id
    assert len(second.derived_events) == 1
    assert second.derived_events[0].id == first.derived_events[0].id
    assert inbox.count_events() == 2


def test_ingest_pending_adr_topic_is_rejected() -> None:
    # cheque.registered was the pending example until ADR 0004 (US-530)
    # approved it and US-533 wired it in; bank_transfer.recorded is
    # still pending ADR (bank layer, 5ت), so it carries the test now.
    service, _ = _ingest_service()
    with pytest.raises(EventTypePendingAdrError) as exc_info:
        service.ingest(
            _incoming({}, event_type="bank_transfer.recorded"),
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
        )
    assert exc_info.value.code == "EVENT_TYPE_PENDING_ADR_APPROVAL"


# ---------------------------------------------------------------------------
# Registry-backed webhook secret store
# ---------------------------------------------------------------------------


class FakeVault:
    """CredentialVaultPort fake: decrypts by lookup, ValueError on 'bad'."""

    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self.secrets = dict(secrets or {})

    @property
    def key_id(self) -> str:
        return "test-key-v1"

    def encrypt(self, plaintext: str) -> str:
        return f"enc:{plaintext}"

    def decrypt(self, ciphertext: str) -> str:
        if ciphertext == "bad":
            raise ValueError("bad ciphertext")
        try:
            return self.secrets[ciphertext]
        except KeyError as exc:
            # The real vault raises ValueError for undecryptable input.
            raise ValueError("unknown ciphertext") from exc


def test_secret_store_decrypts_the_store_secret() -> None:
    registry = FakeIntegrationsRepository()
    add_store(registry, "hayat", webhook_secret_ciphertext="ct-1")
    vault = FakeVault({"ct-1": "s3cr3t"})
    store_secrets = RegistryWebhookSecretStore(registry, vault)
    assert store_secrets.get_secret("hayat", "store-1", INSTALLATION_ID) == "s3cr3t"


def test_secret_store_returns_none_for_a_store_without_a_secret() -> None:
    registry = FakeIntegrationsRepository()
    store = add_store(registry, "hayat")
    # The fake's save_store marks every store as having a secret, so
    # flip the flag directly to model a store with no secret yet.
    registry.stores[(INSTALLATION_ID, "hayat")] = dataclasses.replace(
        store, has_webhook_secret=False
    )
    store_secrets = RegistryWebhookSecretStore(registry, FakeVault())
    assert store_secrets.get_secret("hayat", "store-1", INSTALLATION_ID) is None


def test_secret_store_returns_none_for_an_unknown_store() -> None:
    store_secrets = RegistryWebhookSecretStore(FakeIntegrationsRepository(), FakeVault())
    assert store_secrets.get_secret("hayat", "store-1", INSTALLATION_ID) is None


def test_secret_store_returns_none_without_installation_context() -> None:
    registry = FakeIntegrationsRepository()
    add_store(registry, "hayat", webhook_secret_ciphertext="ct-1")
    store_secrets = RegistryWebhookSecretStore(registry, FakeVault())
    assert store_secrets.get_secret("hayat", "store-1", None) is None


def test_secret_store_fails_closed_when_the_ciphertext_is_missing() -> None:
    registry = FakeIntegrationsRepository()
    store = add_store(registry, "hayat", webhook_secret_ciphertext="ct-1")
    # The flag says a secret exists, but the ciphertext row is gone.
    del registry.store_ciphertexts[(store.id, "webhook_secret")]
    store_secrets = RegistryWebhookSecretStore(registry, FakeVault())
    with pytest.raises(WebhookVaultUnavailableError) as exc_info:
        store_secrets.get_secret("hayat", "store-1", INSTALLATION_ID)
    assert exc_info.value.code == "EVENT_WEBHOOK_VAULT_UNAVAILABLE"


def test_secret_store_fails_closed_without_a_vault() -> None:
    registry = FakeIntegrationsRepository()
    add_store(registry, "hayat", webhook_secret_ciphertext="ct-1")
    store_secrets = RegistryWebhookSecretStore(registry, None)
    with pytest.raises(WebhookVaultUnavailableError):
        store_secrets.get_secret("hayat", "store-1", INSTALLATION_ID)


def test_secret_store_fails_closed_on_an_undecryptable_secret() -> None:
    registry = FakeIntegrationsRepository()
    add_store(registry, "hayat", webhook_secret_ciphertext="bad")
    store_secrets = RegistryWebhookSecretStore(registry, FakeVault())
    with pytest.raises(WebhookVaultUnavailableError):
        store_secrets.get_secret("hayat", "store-1", INSTALLATION_ID)


# ---------------------------------------------------------------------------
# Pipeline: completed order → invoice draft
# ---------------------------------------------------------------------------


def test_order_creates_an_invoice_draft_with_traceability() -> None:
    harness = Harness()
    event_id = harness.store_event(
        CanonicalEventType.SALES_ORDER_COMPLETED, order_payload()
    )
    result = harness.pipeline.process(event_id)

    assert result.outcome == PipelineOutcome.PROCESSED_DRAFT
    assert result.draft_ref is not None
    assert result.posted_ref is None
    invoice = harness.engine.invoices[int(result.draft_ref)]
    assert invoice.state == "draft"
    assert invoice.invoice_type == "out"
    assert invoice.party == PartyRef(5)
    assert invoice.total_amount.amount == 280000
    assert invoice.reference == "hayat:order-1"
    assert "evt-1" in invoice.description
    # The §10.4 mapping ties the order to its invoice.
    found = harness.external_ids.get("hayat", "order", "order-1")
    assert found is not None
    assert found.target_entity_type == "account.invoice"
    assert found.target_external_id == result.draft_ref
    assert must_get(harness.inbox, event_id).status == InboxEventStatus.PROCESSED


def test_order_posts_when_the_gate_allows() -> None:
    harness = Harness(gate_allowed=True)
    event_id = harness.store_event(
        CanonicalEventType.SALES_ORDER_COMPLETED, order_payload()
    )
    result = harness.pipeline.process(event_id)

    assert result.outcome == PipelineOutcome.PROCESSED_POSTED
    assert result.posted_ref == result.draft_ref
    invoice = harness.engine.invoices[int(result.draft_ref)]  # type: ignore[arg-type]
    assert invoice.state == "posted"
    assert invoice.number is not None


def test_order_resend_is_suppressed_without_a_second_invoice() -> None:
    harness = Harness()
    invoice_id = harness.process_order()
    # A re-sent completion (new source event, same order entity).
    event_id = harness.store_event(
        CanonicalEventType.SALES_ORDER_COMPLETED,
        order_payload(),
        source_event_id="evt-order-1-resend",
    )
    result = harness.pipeline.process(event_id)

    assert result.outcome == PipelineOutcome.PROCESSED_DRAFT
    assert result.draft_ref == invoice_id
    assert len(harness.engine.invoices) == 1
    assert harness.audit_log.decisions[-1].details["duplicate_suppressed"] is True


def test_order_with_missing_mapping_routes_to_review() -> None:
    harness = Harness(seed=False)
    event_id = harness.store_event(
        CanonicalEventType.SALES_ORDER_COMPLETED, order_payload()
    )
    result = harness.pipeline.process(event_id)

    assert result.outcome == PipelineOutcome.NEEDS_REVIEW
    assert len(harness.engine.invoices) == 0
    assert len(harness.review_queue.items) == 1
    assert must_get(harness.inbox, event_id).status == InboxEventStatus.NEEDS_REVIEW


def test_order_with_an_invalid_payload_routes_to_review() -> None:
    harness = Harness()
    event_id = harness.store_event(
        CanonicalEventType.SALES_ORDER_COMPLETED,
        order_payload(total_amount=99999),
    )
    result = harness.pipeline.process(event_id)
    assert result.outcome == PipelineOutcome.NEEDS_REVIEW
    assert len(harness.engine.invoices) == 0


def test_order_without_installation_context_routes_to_review() -> None:
    harness = Harness()
    event_id = harness.store_event(
        CanonicalEventType.SALES_ORDER_COMPLETED,
        order_payload(),
        installation_id=None,
    )
    result = harness.pipeline.process(event_id)
    assert result.outcome == PipelineOutcome.NEEDS_REVIEW
    assert len(harness.engine.invoices) == 0


# ---------------------------------------------------------------------------
# Pipeline: cancellation
# ---------------------------------------------------------------------------


def _cancel(harness: Harness, **overrides: Any) -> Any:
    source_event_id = str(overrides.pop("source_event_id", "evt-cancel-1"))
    event_id = harness.store_event(
        CanonicalEventType.SALES_ORDER_CANCELLED,
        cancel_payload(**overrides),
        source_event_id=source_event_id,
        entity_type="order_cancellation",
        entity_id="order-1",
    )
    return harness.pipeline.process(event_id)


def test_cancel_of_a_draft_invoice_cancels_it() -> None:
    harness = Harness()
    invoice_id = harness.process_order()
    result = _cancel(harness)

    assert result.outcome == PipelineOutcome.PROCESSED_DRAFT
    assert result.draft_ref == invoice_id
    assert harness.engine.invoices[int(invoice_id)].state == "cancelled"
    found = harness.external_ids.get("hayat", "order_cancellation", "order-1")
    assert found is not None
    assert found.target_entity_type == "account.invoice"


def test_cancel_of_a_posted_invoice_creates_a_credit_note() -> None:
    harness = Harness(gate_allowed=True)
    invoice_id = harness.process_order(PipelineOutcome.PROCESSED_POSTED)
    assert harness.engine.invoices[int(invoice_id)].state == "posted"
    result = _cancel(harness)

    # The gate allows auto-post, so the credit note posts like any invoice.
    assert result.outcome == PipelineOutcome.PROCESSED_POSTED
    assert result.draft_ref is not None
    assert result.draft_ref != invoice_id
    credit = harness.engine.invoices[int(result.draft_ref)]
    assert credit.state == "posted"
    assert credit.total_amount.amount == -280000
    # The posted invoice is untouched — corrected, never rewritten.
    assert harness.engine.invoices[int(invoice_id)].state == "posted"


def test_cancel_before_the_order_routes_to_review() -> None:
    harness = Harness()
    result = _cancel(harness)
    assert result.outcome == PipelineOutcome.NEEDS_REVIEW


def test_second_cancellation_of_the_same_order_is_a_duplicate() -> None:
    harness = Harness()
    harness.process_order()
    first = _cancel(harness)
    assert first.outcome == PipelineOutcome.PROCESSED_DRAFT
    second = _cancel(harness, source_event_id="evt-cancel-2")
    assert second.outcome == PipelineOutcome.PROCESSED_DRAFT
    assert second.draft_ref == first.draft_ref
    assert harness.audit_log.decisions[-1].details["duplicate_suppressed"] is True


def test_completion_after_a_draft_cancel_routes_to_review() -> None:
    harness = Harness()
    harness.process_order()
    _cancel(harness)
    # The completion arrives late (new source event, same order).
    event_id = harness.store_event(
        CanonicalEventType.SALES_ORDER_COMPLETED,
        order_payload(),
        source_event_id="evt-order-1-late",
    )
    result = harness.pipeline.process(event_id)
    assert result.outcome == PipelineOutcome.NEEDS_REVIEW
    assert len(harness.engine.invoices) == 1


def test_completion_after_a_credit_note_cancel_routes_to_review() -> None:
    harness = Harness(gate_allowed=True)
    harness.process_order(PipelineOutcome.PROCESSED_POSTED)
    _cancel(harness)
    assert len(harness.engine.invoices) == 2  # invoice + credit note
    event_id = harness.store_event(
        CanonicalEventType.SALES_ORDER_COMPLETED,
        order_payload(),
        source_event_id="evt-order-1-late",
    )
    result = harness.pipeline.process(event_id)
    assert result.outcome == PipelineOutcome.NEEDS_REVIEW
    assert len(harness.engine.invoices) == 2


# ---------------------------------------------------------------------------
# Pipeline: refund
# ---------------------------------------------------------------------------


def _refund(harness: Harness, **overrides: Any) -> Any:
    source_event_id = str(overrides.pop("source_event_id", "evt-refund-1"))
    refund_id = str(overrides.get("refund_id", "refund-1"))
    event_id = harness.store_event(
        CanonicalEventType.SALES_REFUND_CREATED,
        refund_payload(**overrides),
        source_event_id=source_event_id,
        entity_type="refund",
        entity_id=refund_id,
    )
    return harness.pipeline.process(event_id)


def _post_order(harness: Harness, **overrides: Any) -> str:
    """Process an order, then post its invoice; return the invoice id."""
    invoice_id = harness.process_order(**overrides)
    invoice = harness.engine.invoices[int(invoice_id)]
    harness.engine.validate_invoice(invoice.ref)
    harness.engine.post_invoice(invoice.ref)
    return invoice_id


#: Overrides for an order whose lines share ONE revenue account (no
#: shipping line): partial credits need a single account.
NO_SHIPPING = {"shipping_amount": 0, "total_amount": 24500}


def test_full_refund_of_a_posted_invoice_creates_a_full_credit_note() -> None:
    harness = Harness()
    invoice_id = _post_order(harness)
    result = _refund(harness, refund_amount=28000, gross_amount=28000)

    assert result.outcome == PipelineOutcome.PROCESSED_DRAFT
    credit = harness.engine.invoices[int(result.draft_ref)]  # type: ignore[arg-type]
    assert credit.total_amount.amount == -280000
    assert len(credit.lines) == 3  # every line mirrored
    assert harness.engine.invoices[int(invoice_id)].state == "posted"


def test_partial_refund_of_a_posted_invoice_creates_a_partial_credit_note() -> None:
    harness = Harness()
    _post_order(harness, **NO_SHIPPING)
    result = _refund(harness, refund_amount=5000, gross_amount=5000)

    assert result.outcome == PipelineOutcome.PROCESSED_DRAFT
    credit = harness.engine.invoices[int(result.draft_ref)]  # type: ignore[arg-type]
    assert credit.total_amount.amount == -50000
    assert len(credit.lines) == 1
    assert credit.lines[0].account == AccountRef(12)
    assert credit.lines[0].quantity == -1


def test_full_refund_of_a_draft_invoice_cancels_it() -> None:
    harness = Harness()
    invoice_id = harness.process_order()
    result = _refund(harness, refund_amount=28000, gross_amount=28000)

    assert result.outcome == PipelineOutcome.PROCESSED_DRAFT
    assert result.draft_ref == invoice_id
    assert harness.engine.invoices[int(invoice_id)].state == "cancelled"


def test_partial_refund_of_a_draft_invoice_routes_to_review() -> None:
    harness = Harness()
    harness.process_order()
    result = _refund(harness, refund_amount=5000, gross_amount=5000)
    assert result.outcome == PipelineOutcome.NEEDS_REVIEW
    assert len(harness.engine.invoices) == 1


def test_refund_exceeding_the_invoice_total_routes_to_review() -> None:
    harness = Harness()
    _post_order(harness)
    result = _refund(harness, refund_amount=99999, gross_amount=99999)
    assert result.outcome == PipelineOutcome.NEEDS_REVIEW
    assert len(harness.engine.invoices) == 1


def test_refund_before_the_order_routes_to_review() -> None:
    harness = Harness()
    result = _refund(harness)
    assert result.outcome == PipelineOutcome.NEEDS_REVIEW


def test_two_partial_refunds_of_one_order_both_credit() -> None:
    harness = Harness()
    _post_order(harness, **NO_SHIPPING)
    first = _refund(harness, refund_amount=5000, gross_amount=5000)
    second = _refund(
        harness,
        refund_id="refund-2",
        refund_amount=3000,
        gross_amount=3000,
        source_event_id="evt-refund-2",
    )
    assert first.outcome == PipelineOutcome.PROCESSED_DRAFT
    assert second.outcome == PipelineOutcome.PROCESSED_DRAFT
    assert len(harness.engine.invoices) == 3  # invoice + 2 credit notes


# ---------------------------------------------------------------------------
# Pipeline: payments
# ---------------------------------------------------------------------------


def _capture(harness: Harness, **overrides: Any) -> Any:
    source_event_id = str(overrides.pop("source_event_id", "evt-pay-1"))
    gateway_id = str(overrides.get("gateway_payment_id", "pay-1"))
    event_id = harness.store_event(
        CanonicalEventType.PAYMENT_CAPTURED,
        capture_payload(**overrides),
        source_event_id=source_event_id,
        entity_type="payment",
        entity_id=gateway_id,
    )
    return harness.pipeline.process(event_id)


def _payment_refund(harness: Harness, **overrides: Any) -> Any:
    source_event_id = str(overrides.pop("source_event_id", "evt-payrefund-1"))
    refund_id = str(overrides.get("refund_id", "refund-1"))
    event_id = harness.store_event(
        CanonicalEventType.PAYMENT_REFUNDED,
        payment_refund_payload(**overrides),
        source_event_id=source_event_id,
        entity_type="payment_refund",
        entity_id=refund_id,
    )
    return harness.pipeline.process(event_id)


def test_capture_creates_a_receivable_payment_draft() -> None:
    harness = Harness()
    result = _capture(harness)

    assert result.outcome == PipelineOutcome.PROCESSED_DRAFT
    payment = harness.engine.payments[int(result.draft_ref)]  # type: ignore[arg-type]
    assert payment.kind == "receivable"
    assert payment.state == "draft"
    assert payment.amount.amount == 275000
    assert payment.party == PartyRef(5)
    assert payment.journal_name == "journal-7"
    assert payment.reference == "pay-1"
    found = harness.external_ids.get("hayat", "payment", "pay-1")
    assert found is not None
    assert found.target_entity_type == "account.payment"


def test_capture_is_never_auto_posted_even_when_the_gate_allows() -> None:
    harness = Harness(gate_allowed=True)
    result = _capture(harness)

    assert result.outcome == PipelineOutcome.PROCESSED_DRAFT
    assert result.posted_ref is None
    payment = harness.engine.payments[int(result.draft_ref)]  # type: ignore[arg-type]
    assert payment.state == "draft"
    assert "never auto-posted" in harness.audit_log.decisions[-1].details["reason"]


def test_capture_without_a_customer_falls_back_to_the_order_invoice_party() -> None:
    harness = Harness()
    harness.process_order()
    payload = capture_payload(gateway_payment_id="pay-2")
    del payload["customer"]
    event_id = harness.store_event(
        CanonicalEventType.PAYMENT_CAPTURED,
        payload,
        source_event_id="evt-pay-2",
        entity_type="payment",
        entity_id="pay-2",
    )
    result = harness.pipeline.process(event_id)
    assert result.outcome == PipelineOutcome.PROCESSED_DRAFT
    payment = harness.engine.payments[int(result.draft_ref)]  # type: ignore[arg-type]
    assert payment.party == PartyRef(5)


def test_capture_without_a_customer_or_an_order_routes_to_review() -> None:
    harness = Harness()
    payload = capture_payload(gateway_payment_id="pay-3")
    del payload["customer"]
    del payload["order_id"]
    event_id = harness.store_event(
        CanonicalEventType.PAYMENT_CAPTURED,
        payload,
        source_event_id="evt-pay-3",
        entity_type="payment",
        entity_id="pay-3",
    )
    result = harness.pipeline.process(event_id)
    assert result.outcome == PipelineOutcome.NEEDS_REVIEW


def test_payment_refund_creates_a_payable_payment_draft() -> None:
    harness = Harness()
    harness.process_order()  # the payment leg's party comes from the order's invoice
    result = _payment_refund(harness)

    assert result.outcome == PipelineOutcome.PROCESSED_DRAFT
    payment = harness.engine.payments[int(result.draft_ref)]  # type: ignore[arg-type]
    assert payment.kind == "payable"
    assert payment.state == "draft"
    assert payment.amount.amount == 50000
    found = harness.external_ids.get("hayat", "payment_refund", "refund-1")
    assert found is not None
    assert found.target_entity_type == "account.payment"


def test_fanout_payment_leg_without_gross_amount_routes_to_review() -> None:
    # A refund payload without gross_amount fans out, but the
    # payment.refunded leg cannot build a payment from it.
    harness = Harness()
    payload = payment_refund_payload()
    del payload["gross_amount"]
    event_id = harness.store_event(
        CanonicalEventType.PAYMENT_REFUNDED,
        payload,
        source_event_id="evt-payrefund-2",
        entity_type="payment_refund",
        entity_id="refund-1",
    )
    result = harness.pipeline.process(event_id)
    assert result.outcome == PipelineOutcome.NEEDS_REVIEW
    assert len(harness.engine.payments) == 0


def test_refund_fanout_processes_both_legs_end_to_end() -> None:
    """Ingest refund.created → two inbox rows → credit note + payable payment."""
    harness = Harness()
    _post_order(harness, **NO_SHIPPING)
    inbox = harness.inbox
    service = EventIngestService(
        inbox, FakeIdempotencyStore(), FakeWebhookSecrets(), InMemoryJobQueue()
    )
    body = json.dumps(
        raw_envelope(
            source_event_id="webhook-refund-9",
            payload=refund_payload(refund_amount=5000, gross_amount=5000),
            external_entity_type="refund",
            external_entity_id="refund-1",
        )
    ).encode()
    ingest_result = service.ingest(
        IncomingEvent(connector="hayat", raw_body=body, event_type="refund.created"),
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
    )
    assert ingest_result.status == "created"
    assert len(ingest_result.derived_events) == 1

    refund_result = harness.pipeline.process(ingest_result.event.id)
    payment_result = harness.pipeline.process(ingest_result.derived_events[0].id)
    assert refund_result.outcome == PipelineOutcome.PROCESSED_DRAFT
    assert payment_result.outcome == PipelineOutcome.PROCESSED_DRAFT
    credit = harness.engine.invoices[int(refund_result.draft_ref)]  # type: ignore[arg-type]
    assert credit.total_amount.amount == -50000
    payment = harness.engine.payments[int(payment_result.draft_ref)]  # type: ignore[arg-type]
    assert payment.kind == "payable"
    assert payment.amount.amount == 50000
