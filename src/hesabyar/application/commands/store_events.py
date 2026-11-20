"""Store-event mapping + document building for the §10.3 pipeline (US-511).

- :class:`StoreMappingResolver` — the registry-backed
  :class:`MappingResolverPort` (the F3 seam): resolves a store
  event's company, party, revenue/shipping accounts and payment
  journal from the mapping center (§10.5), and its products from the
  §10.4 external-id map. Anything unresolvable is reported in
  ``ResolvedMapping.unresolved`` — the pipeline fails closed to the
  review queue, never to a default account.
- :class:`StoreDraftBuilder` — the store-aware
  :class:`DraftBuilderPort`: builds the engine document for a store
  event. Completed orders become customer invoice drafts (Path B of
  phased design §2 §1.6 — the design allows starting with direct
  invoices and migrating to Sale → Invoice later, provided the
  canonical event and the mapping center are designed for both paths
  from the start, which they are); cancellations cancel the draft
  invoice or credit a posted one; refunds become credit notes (full
  or amount-based partial); captured/refunded payments become
  receivable/payable payment drafts.

Target-reference conventions (the mapping center stores
``target_ref`` as a string; the resolver interprets it):

- ``company`` / ``party`` / ``journal`` → the engine id, as a bare
  integer (``"42"``) or a ``tryton:{kind}:{id}`` reference.
- ``account`` → the account CODE in the company's chart (e.g.
  ``"4.1.1"``), resolved through the engine chart; a
  ``tryton:account:{id}`` reference is also accepted and used
  directly.

Both classes depend only on domain ports (spec §6.3): the mapping
registry, the external-id map, and the accounting engine.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from hesabyar.application.errors import translate_port_errors
from hesabyar.domain.accounting.commands import (
    CancelInvoice,
    CreateCreditNote,
    CreateInvoiceDraft,
    CreatePayment,
    InvoiceDraftLine,
)
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import (
    AccountRef,
    CompanyRef,
    InvoiceRef,
    PartyRef,
    PaymentJournalRef,
    ProductRef,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.events.envelope import CanonicalEventType
from hesabyar.domain.events.errors import InvalidEventEnvelopeError, MappingUnresolvedError
from hesabyar.domain.events.models import NormalizedEvent, ResolvedMapping
from hesabyar.domain.events.ports import (
    EngineDocumentCommand,
    ExternalIdMapPort,
)
from hesabyar.domain.events.store_webhooks import (
    DEFAULT_MAPPING_SOURCE_KEY,
    ORDER_CANCELLATION_ENTITY_TYPE,
    STORE_DOCUMENT_EVENT_TYPES,
    customer_match_keys,
    product_match_key,
    validate_store_payload,
)
from hesabyar.domain.integrations.models import IntegrationMapping, MappingKind
from hesabyar.domain.integrations.ports import IntegrationsRegistryPort

#: Target entity type for products in the §10.4 external-id map.
TARGET_ENTITY_PRODUCT = "product.product"

#: Target entity type for invoices in the §10.4 external-id map.
TARGET_ENTITY_INVOICE = "account.invoice"


def _parse_engine_id(target_ref: str, kind: str) -> int | None:
    """Parse an engine-id reference: ``"42"`` or ``"tryton:{kind}:42"``."""
    text = target_ref.strip()
    if text.isdigit():
        return int(text)
    prefix = f"tryton:{kind}:"
    if text.startswith(prefix) and text[len(prefix) :].isdigit():
        return int(text[len(prefix) :])
    return None


def _store_company_source_key(event: NormalizedEvent) -> str:
    """Source key for the store→company mapping: ``system:store``.

    The composite disambiguates two connectors that happen to use
    the same store id.
    """
    source = event.event
    return f"{source.source_system}:{source.source_store_id}"


class StoreMappingResolver:
    """Resolve a store event's mappings from the registry + id map.

    Resolution is installation-scoped (``NormalizedEvent
    .installation_id``, from the inbox row): a row of another
    installation looks exactly like a row that does not exist. Every
    gap is collected and reported at once, so one review item lists
    everything the operator must map — not one gap per retry.
    """

    def __init__(
        self,
        registry: IntegrationsRegistryPort,
        external_ids: ExternalIdMapPort,
        engine: AccountingEnginePort,
    ) -> None:
        self._registry = registry
        self._external_ids = external_ids
        self._engine = engine

    def resolve(self, event: NormalizedEvent) -> ResolvedMapping:
        """Resolve company/party/accounts/journal/products for an event."""
        installation_id = event.installation_id
        if installation_id is None:
            return ResolvedMapping(
                mapping_version="unresolved",
                unresolved=("installation_context",),
            )
        unresolved: list[str] = []
        versions: list[str] = []
        accounts: dict[str, AccountRef] = {}
        products: dict[str, ProductRef] = {}
        company = self._resolve_company(event, installation_id, unresolved, versions)
        chart: dict[str, AccountRef] | None = None

        def chart_by_code() -> dict[str, AccountRef]:
            nonlocal chart
            if chart is None:
                assert company is not None  # only called after the company resolves
                with translate_port_errors():
                    views = self._engine.get_chart_of_accounts(company)
                chart = {view.code: view.ref for view in views}
            return chart

        payload = event.converted_payload
        party: PartyRef | None = None
        payment_journal: PaymentJournalRef | None = None
        event_type = event.event_type

        if event_type in (
            CanonicalEventType.CHEQUE_RECEIVED_REGISTERED,
            CanonicalEventType.CHEQUE_RECEIVED_CANCELLED,
        ):
            # The cheque issuer (customer) resolves from the payload's
            # ``customer_ref`` (a plain string, not a customer object);
            # an unmapped ref fails closed to the review queue (§5
            # §1.2). The payee-vs-company check is NOT here — it needs
            # the company's national id, which the intake service
            # verifies (US-533).
            customer_ref = payload.get("customer_ref")
            if isinstance(customer_ref, str) and customer_ref.strip():
                party = self._resolve_party_by_ref(
                    installation_id, customer_ref.strip(), unresolved, versions
                )
        elif event_type is CanonicalEventType.SALES_ORDER_COMPLETED:
            party = self._resolve_party(event, installation_id, payload, unresolved, versions)
            products = self._resolve_products(event)
            if company is not None:
                categories = {
                    str(line.get("revenue_category") or DEFAULT_MAPPING_SOURCE_KEY)
                    for line in payload.get("lines", [])
                    if isinstance(line, Mapping)
                }
                for category in sorted(categories):
                    account = self._resolve_account(
                        installation_id,
                        MappingKind.REVENUE_CATEGORY,
                        "",
                        category,
                        chart_by_code(),
                        unresolved,
                        versions,
                    )
                    if account is not None:
                        accounts[f"revenue:{category}"] = account
                shipping = payload.get("shipping_amount")
                if isinstance(shipping, int) and not isinstance(shipping, bool) and shipping > 0:
                    shipping_method = payload.get("shipping_method")
                    source_key = (
                        shipping_method
                        if isinstance(shipping_method, str) and shipping_method.strip()
                        else DEFAULT_MAPPING_SOURCE_KEY
                    )
                    account = self._resolve_account(
                        installation_id,
                        MappingKind.SHIPPING_CHARGE,
                        "",
                        source_key,
                        chart_by_code(),
                        unresolved,
                        versions,
                    )
                    if account is not None:
                        accounts["shipping"] = account
        elif event_type in (CanonicalEventType.PAYMENT_CAPTURED, CanonicalEventType.PAYMENT_REFUNDED):
            party = self._resolve_party(event, installation_id, payload, unresolved, versions)
            payment_journal = self._resolve_payment_journal(
                event, installation_id, payload, unresolved, versions
            )

        return ResolvedMapping(
            mapping_version=",".join(versions) if versions else "registry:empty",
            company=company,
            accounts=accounts,
            journal=None,
            party=party,
            unresolved=tuple(unresolved),
            products=products,
            payment_journal=payment_journal,
        )

    def _resolve_company(
        self,
        event: NormalizedEvent,
        installation_id: str,
        unresolved: list[str],
        versions: list[str],
    ) -> CompanyRef | None:
        source_key = _store_company_source_key(event)
        mapping = self._registry.resolve_mapping(
            installation_id, MappingKind.STORE_COMPANY, "", source_key
        )
        if mapping is None:
            unresolved.append(f"store_company:{source_key}")
            return None
        engine_id = self._engine_id_target(mapping, "company", unresolved)
        if engine_id is None:
            return None
        versions.append(f"store_company:{source_key}:v{mapping.version}")
        return CompanyRef(engine_id)

    def _resolve_party_by_ref(
        self,
        installation_id: str,
        customer_ref: str,
        unresolved: list[str],
        versions: list[str],
    ) -> PartyRef | None:
        """Resolve a customer ref (plain string) → party (US-533 cheques)."""
        mapping = self._registry.resolve_mapping(
            installation_id, MappingKind.EXTERNAL_PARTY, "", customer_ref
        )
        if mapping is None:
            unresolved.append(f"external_party:{customer_ref}")
            return None
        engine_id = self._engine_id_target(mapping, "party", unresolved)
        if engine_id is None:
            return None
        versions.append(f"external_party:{customer_ref}:v{mapping.version}")
        return PartyRef(engine_id)

    def _resolve_party(
        self,
        event: NormalizedEvent,
        installation_id: str,
        payload: Mapping[str, Any],
        unresolved: list[str],
        versions: list[str],
    ) -> PartyRef | None:
        """Resolve the customer → party mapping (external id first).

        A payload without a customer leaves the party unresolved for
        the builder to fall back to the order's invoice party — the
        resolver does not fail the event for a missing customer here.
        """
        customer = payload.get("customer")
        if not isinstance(customer, Mapping):
            return None
        customer_id = customer.get("id")
        if not isinstance(customer_id, str) or not customer_id.strip():
            return None
        mapping = self._registry.resolve_mapping(
            installation_id, MappingKind.EXTERNAL_PARTY, "", customer_id.strip()
        )
        if mapping is None:
            keys = ", ".join(customer_match_keys(customer))
            unresolved.append(f"external_party:{customer_id.strip()} (match keys: {keys})")
            return None
        engine_id = self._engine_id_target(mapping, "party", unresolved)
        if engine_id is None:
            return None
        versions.append(f"external_party:{customer_id.strip()}:v{mapping.version}")
        return PartyRef(engine_id)

    def _resolve_products(self, event: NormalizedEvent) -> dict[str, ProductRef]:
        """Resolve order-line SKUs → engine products via the id map.

        Products are resolved (for the Path A sale migration) but do
        NOT block the invoice path: invoice lines are account-based,
        so an unmapped SKU is recorded in the mapping and left for
        the sale migration to enforce. The lookup itself is the
        documented matching rule (SKU → external-id map → product).
        """
        products: dict[str, ProductRef] = {}
        lines = event.converted_payload.get("lines")
        if not isinstance(lines, list):
            return products
        for line in lines:
            if not isinstance(line, Mapping):
                continue
            try:
                sku = product_match_key(line)
            except InvalidEventEnvelopeError:
                continue
            found = self._external_ids.get(event.event.source_system, "product", sku)
            if (
                found is not None
                and found.target_engine == "tryton"
                and found.target_entity_type == TARGET_ENTITY_PRODUCT
                and found.target_external_id.isdigit()
            ):
                products[sku] = ProductRef(int(found.target_external_id))
        return products

    def _resolve_account(
        self,
        installation_id: str,
        kind: MappingKind,
        channel_key: str,
        source_key: str,
        chart: Mapping[str, AccountRef],
        unresolved: list[str],
        versions: list[str],
    ) -> AccountRef | None:
        mapping = self._registry.resolve_mapping(installation_id, kind, channel_key, source_key)
        if mapping is None:
            unresolved.append(f"{kind.value}:{source_key}")
            return None
        if mapping.target_type != "account":
            unresolved.append(
                f"{kind.value}:{source_key} (mapping targets {mapping.target_type!r}, not 'account')"
            )
            return None
        engine_id = _parse_engine_id(mapping.target_ref, "account")
        if engine_id is not None:
            versions.append(f"{kind.value}:{source_key}:v{mapping.version}")
            return AccountRef(engine_id)
        account = chart.get(mapping.target_ref.strip())
        if account is None:
            unresolved.append(
                f"{kind.value}:{source_key} (mapped account code "
                f"{mapping.target_ref!r} is not in the company chart)"
            )
            return None
        versions.append(f"{kind.value}:{source_key}:v{mapping.version}")
        return account

    def _resolve_payment_journal(
        self,
        event: NormalizedEvent,
        installation_id: str,
        payload: Mapping[str, Any],
        unresolved: list[str],
        versions: list[str],
    ) -> PaymentJournalRef | None:
        channel = payload.get("payment_channel")
        if not isinstance(channel, str) or not channel.strip():
            return None
        channel = channel.strip()
        mapping = self._registry.resolve_mapping(
            installation_id, MappingKind.GATEWAY_JOURNAL, channel, channel
        )
        if mapping is None:
            unresolved.append(f"gateway_journal:{channel}")
            return None
        engine_id = self._engine_id_target(mapping, "journal", unresolved)
        if engine_id is None:
            return None
        versions.append(f"gateway_journal:{channel}:v{mapping.version}")
        return PaymentJournalRef(engine_id)

    def _engine_id_target(
        self, mapping: IntegrationMapping, kind: str, unresolved: list[str]
    ) -> int | None:
        """Parse a mapping's target_ref as an engine id (or record the gap)."""
        if mapping.target_type != kind:
            unresolved.append(
                f"{mapping.mapping_kind.value}:{mapping.source_key} "
                f"(mapping targets {mapping.target_type!r}, not {kind!r})"
            )
            return None
        engine_id = _parse_engine_id(mapping.target_ref, kind)
        if engine_id is None:
            unresolved.append(
                f"{mapping.mapping_kind.value}:{mapping.source_key} "
                f"(target_ref {mapping.target_ref!r} is not an engine id)"
            )
            return None
        return engine_id


class StoreDraftBuilder:
    """Build the engine document for a store event.

    The builder validates the payload first (fail-closed to review
    on any contract violation), then reads the engine state it needs
    to decide the document: a cancellation credits a posted invoice
    but cancels a draft one, a refund is full or partial against the
    invoice total, a payment falls back to the order's invoice party
    when the payload carries no customer. Engine reads are wrapped
    in :func:`translate_port_errors` so a transient engine failure
    retries instead of routing to review.
    """

    def __init__(self, engine: AccountingEnginePort, external_ids: ExternalIdMapPort) -> None:
        self._engine = engine
        self._external_ids = external_ids

    def build(self, event: NormalizedEvent, mapping: ResolvedMapping) -> EngineDocumentCommand:
        """Build the document command for a normalized store event."""
        event_type = event.event_type
        if event_type not in STORE_DOCUMENT_EVENT_TYPES:
            raise MappingUnresolvedError(
                f"No store document builder for event type {event_type.value!r}; "
                "the event belongs to a later stream.",
                details={"event_type": event_type.value},
            )
        if event_type in (
            CanonicalEventType.CHEQUE_RECEIVED_REGISTERED,
            CanonicalEventType.CHEQUE_RECEIVED_CANCELLED,
        ):
            # Cheque intake creates NO engine document — it writes
            # the HesabYar cheque registry (+ the engine cheque
            # record) through ChequeCommandService. The pipeline
            # branches cheque events to the intake handler before
            # the builder runs (US-533); reaching here is a wiring
            # bug, and falling through to the payment builder below
            # would draft a payment for a cheque.
            raise MappingUnresolvedError(
                f"Cheque event {event_type.value!r} is handled by the pipeline's "
                "cheque intake branch, not the draft builder.",
                details={"event_type": event_type.value},
            )
        payload = event.converted_payload
        validate_store_payload(
            event_type,
            payload,
            external_entity_type=event.event.external_entity_type,
            external_entity_id=event.event.external_entity_id,
        )
        if event_type is CanonicalEventType.SALES_ORDER_COMPLETED:
            return self._build_order_invoice(event, mapping)
        if event_type is CanonicalEventType.SALES_ORDER_CANCELLED:
            return self._build_cancellation(event, mapping)
        if event_type is CanonicalEventType.SALES_REFUND_CREATED:
            return self._build_refund(event, mapping)
        if event_type is CanonicalEventType.PAYMENT_CAPTURED:
            return self._build_payment(event, mapping, kind="receivable")
        return self._build_payment(event, mapping, kind="payable")

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _require_company(mapping: ResolvedMapping) -> CompanyRef:
        if mapping.company is None:
            raise MappingUnresolvedError(
                "No company mapping resolved for this event.",
                details={"unresolved": list(mapping.unresolved)},
            )
        return mapping.company

    @staticmethod
    def _event_date(event: NormalizedEvent) -> FiscalDate:
        return FiscalDate.from_gregorian(event.event.occurred_at.date())

    def _order_invoice(self, event: NormalizedEvent, order_id: str) -> InvoiceRef:
        """The invoice a store order produced, via the §10.4 id map."""
        found = self._external_ids.get(event.event.source_system, "order", order_id)
        if found is None:
            raise MappingUnresolvedError(
                f"No engine record exists for store order {order_id!r}; "
                "the order event may not have arrived yet.",
                details={"order_id": order_id},
            )
        if found.target_entity_type != TARGET_ENTITY_INVOICE or not found.target_external_id.isdigit():
            raise MappingUnresolvedError(
                f"Store order {order_id!r} maps to an unsupported engine record "
                f"({found.target_entity_type}/{found.target_external_id}).",
                details={"order_id": order_id},
            )
        return InvoiceRef(int(found.target_external_id))

    def _invoice_party(self, event: NormalizedEvent, order_id: str) -> PartyRef | None:
        """A payment's party from the linked order's invoice (fallback)."""
        try:
            invoice = self._order_invoice(event, order_id)
        except MappingUnresolvedError:
            return None
        with translate_port_errors():
            view = self._engine.get_invoice(invoice)
        return view.party if view is not None else None

    # -- completed order → customer invoice draft (Path B) -----------------

    def _build_order_invoice(self, event: NormalizedEvent, mapping: ResolvedMapping) -> CreateInvoiceDraft:
        payload = event.converted_payload
        company = self._require_company(mapping)
        if mapping.party is None:
            raise MappingUnresolvedError(
                "No party mapping resolved for this order's customer.",
                details={"unresolved": list(mapping.unresolved)},
            )
        order_id = str(payload["order_id"])
        # Out-of-order guard: a completion that arrives after the
        # order's cancellation must not create an invoice. Two
        # signals prove the order is gone: the cancellation mapping
        # (when the cancellation carried no distinct id) and the
        # order's own invoice being cancelled (which also covers a
        # cancellation that carried a distinct cancellation id).
        cancelled = self._external_ids.get(
            event.event.source_system, ORDER_CANCELLATION_ENTITY_TYPE, order_id
        )
        if cancelled is not None:
            raise InvalidEventEnvelopeError(
                f"Store order {order_id!r} was already cancelled; its completion "
                "event arrived late and no invoice is created.",
                details={"order_id": order_id},
            )
        existing_invoice = self._external_ids.get(event.event.source_system, "order", order_id)
        if (
            existing_invoice is not None
            and existing_invoice.target_entity_type == TARGET_ENTITY_INVOICE
            and existing_invoice.target_external_id.isdigit()
        ):
            with translate_port_errors():
                view = self._engine.get_invoice(
                    InvoiceRef(int(existing_invoice.target_external_id))
                )
            if view is not None and view.state == "cancelled":
                raise InvalidEventEnvelopeError(
                    f"Store order {order_id!r} was already cancelled; its completion "
                    "event arrived late and no invoice is created.",
                    details={"order_id": order_id},
                )
            if view is not None:
                # The pipeline's pre-check normally suppresses this;
                # reaching here means the order was already invoiced.
                raise InvalidEventEnvelopeError(
                    f"Store order {order_id!r} already has invoice "
                    f"{view.ref.engine_id}; no second invoice is created.",
                    details={"order_id": order_id, "invoice_id": view.ref.engine_id},
                )
        lines: list[InvoiceDraftLine] = []
        for raw_line in payload["lines"]:
            sku = product_match_key(raw_line)
            category = raw_line.get("revenue_category") or DEFAULT_MAPPING_SOURCE_KEY
            account = mapping.accounts.get(f"revenue:{category}")
            if account is None:
                raise MappingUnresolvedError(
                    f"No revenue account resolved for category {category!r} (line SKU {sku!r}).",
                    details={"revenue_category": str(category), "product_sku": sku},
                )
            description = raw_line.get("description") or sku
            lines.append(
                InvoiceDraftLine(
                    account=account,
                    unit_price=MoneyIRR(int(raw_line["unit_price"])),
                    quantity=Decimal(str(raw_line["quantity"])),
                    description=f"{description} (SKU {sku})",
                )
            )
        shipping = payload.get("shipping_amount") or 0
        if isinstance(shipping, int) and shipping > 0:
            account = mapping.accounts.get("shipping")
            if account is None:
                raise MappingUnresolvedError(
                    "No shipping account resolved for this order.",
                    details={"shipping_amount_irr": shipping},
                )
            lines.append(
                InvoiceDraftLine(
                    account=account,
                    unit_price=MoneyIRR(shipping),
                    quantity=Decimal(1),
                    description="Shipping",
                )
            )
        total = MoneyIRR(sum(line.amount.amount for line in lines))
        if total.is_zero or total.is_negative:
            raise InvalidEventEnvelopeError(
                f"Store order {order_id!r} has no billable amount; no invoice is created.",
                details={"order_id": order_id},
            )
        source = event.event
        description = (
            f"Store order {order_id} from {source.source_system} "
            f"(event {source.source_event_id})"
        )
        tax = payload.get("tax_amount") or 0
        discount = payload.get("discount_amount") or 0
        if (isinstance(tax, int) and tax > 0) or (isinstance(discount, int) and discount > 0):
            # Tax and discount are validated for arithmetic
            # consistency but NOT posted as invoice lines yet: no tax
            # account is resolvable from the nine §10.5 kinds, and
            # invoice drafts cannot carry negative (discount) lines.
            # The gap is recorded here and in the audit log so the
            # reviewing human sees exactly what is missing.
            description += (
                f" — store total {payload['total_amount']} IRR incl. tax {tax} "
                f"and discount {discount}; tax/discount lines are not posted yet"
            )
        return CreateInvoiceDraft(
            company=company,
            invoice_type="out",
            party=mapping.party,
            date=self._event_date(event),
            lines=tuple(lines),
            description=description,
            reference=f"{source.source_system}:{order_id}",
        )

    # -- cancelled order → cancel the draft / credit the posted invoice ----

    def _build_cancellation(self, event: NormalizedEvent, mapping: ResolvedMapping) -> EngineDocumentCommand:
        self._require_company(mapping)
        payload = event.converted_payload
        order_id = str(payload["order_id"])
        invoice = self._order_invoice(event, order_id)
        with translate_port_errors():
            view = self._engine.get_invoice(invoice)
        if view is None:
            raise MappingUnresolvedError(
                f"Invoice {invoice.engine_id} for store order {order_id!r} no longer "
                "exists in the engine.",
                details={"order_id": order_id, "invoice_id": invoice.engine_id},
            )
        source = event.event
        if view.state in ("draft", "validated"):
            return CancelInvoice(
                invoice=invoice,
                description=(
                    f"Cancelled: store order {order_id} "
                    f"(event {source.source_event_id})"
                ),
            )
        if view.state in ("posted", "paid"):
            # Post-invoice cancellation is a corrective document —
            # the posted invoice is never rewritten or deleted.
            return CreateCreditNote(
                invoice=invoice,
                date=self._event_date(event),
                description=(
                    f"Credit note: store order {order_id} cancelled after invoicing "
                    f"(event {source.source_event_id})"
                ),
            )
        raise InvalidEventEnvelopeError(
            f"Store order {order_id!r} cannot be cancelled: its invoice is "
            f"already {view.state}.",
            details={"order_id": order_id, "invoice_state": view.state},
        )

    # -- refund → full or partial credit note (never a rewrite) ------------

    def _build_refund(self, event: NormalizedEvent, mapping: ResolvedMapping) -> EngineDocumentCommand:
        self._require_company(mapping)
        payload = event.converted_payload
        order_id = str(payload["order_id"])
        refund_id = str(payload["refund_id"])
        refund_amount = MoneyIRR(int(payload["refund_amount"]))
        invoice = self._order_invoice(event, order_id)
        with translate_port_errors():
            view = self._engine.get_invoice(invoice)
        if view is None:
            raise MappingUnresolvedError(
                f"Invoice {invoice.engine_id} for store order {order_id!r} no longer "
                "exists in the engine.",
                details={"order_id": order_id, "invoice_id": invoice.engine_id},
            )
        source = event.event
        if view.state in ("draft", "validated"):
            if refund_amount == view.total_amount:
                # A full refund of an unposted invoice cancels it —
                # there is nothing posted to credit.
                return CancelInvoice(
                    invoice=invoice,
                    description=(
                        f"Cancelled: full refund {refund_id} for store order {order_id} "
                        f"(event {source.source_event_id})"
                    ),
                )
            raise InvalidEventEnvelopeError(
                f"Partial refund {refund_id} ({refund_amount.amount} IRR) for "
                f"unposted invoice {invoice.engine_id} ({view.total_amount.amount} "
                "IRR) needs manual amendment of the draft invoice.",
                details={"refund_id": refund_id, "order_id": order_id},
            )
        if view.state in ("posted", "paid"):
            if refund_amount.amount > view.total_amount.amount:
                raise InvalidEventEnvelopeError(
                    f"Refund {refund_id} ({refund_amount.amount} IRR) exceeds invoice "
                    f"{invoice.engine_id} total ({view.total_amount.amount} IRR).",
                    details={"refund_id": refund_id, "order_id": order_id},
                )
            if refund_amount == view.total_amount:
                return CreateCreditNote(
                    invoice=invoice,
                    date=self._event_date(event),
                    description=(
                        f"Credit note: full refund {refund_id} for store order {order_id} "
                        f"(event {source.source_event_id})"
                    ),
                )
            return CreateCreditNote(
                invoice=invoice,
                date=self._event_date(event),
                description=(
                    f"Credit note: partial refund {refund_id} ({refund_amount.amount} "
                    f"IRR of {view.total_amount.amount} IRR) for store order {order_id} "
                    f"(event {source.source_event_id})"
                ),
                partial_amount=refund_amount,
            )
        raise InvalidEventEnvelopeError(
            f"Refund {refund_id} for store order {order_id!r} cannot be applied: "
            f"its invoice is {view.state}.",
            details={"refund_id": refund_id, "order_id": order_id, "invoice_state": view.state},
        )

    # -- captured / refunded payment → payment draft -----------------------

    def _build_payment(
        self, event: NormalizedEvent, mapping: ResolvedMapping, *, kind: str
    ) -> CreatePayment:
        payload = event.converted_payload
        company = self._require_company(mapping)
        party = mapping.party
        if party is None:
            order_id = payload.get("order_id")
            if isinstance(order_id, str) and order_id.strip():
                party = self._invoice_party(event, order_id.strip())
        if party is None:
            raise MappingUnresolvedError(
                "No party could be resolved for this payment (no customer mapping "
                "and no linked order invoice).",
                details={"unresolved": list(mapping.unresolved)},
            )
        if mapping.payment_journal is None:
            raise MappingUnresolvedError(
                "No payment journal resolved for this payment's channel.",
                details={"payment_channel": str(payload.get("payment_channel"))},
            )
        reference = payload.get("gateway_payment_id") or payload.get("refund_id") or ""
        return CreatePayment(
            company=company,
            party=party,
            kind=kind,
            amount=MoneyIRR(int(payload["gross_amount"])),
            date=self._event_date(event),
            journal=mapping.payment_journal,
            line=None,
            # Unlinked draft: the payment is not attached to an
            # invoice line yet (the invoice may not exist when the
            # payment arrives). Settlement (5پ) links it; the
            # clearing module needs the account only when the
            # payment succeeds, not at draft time.
            account=None,
            reference=str(reference),
        )
