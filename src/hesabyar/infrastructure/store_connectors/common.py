"""Shared base for the generic store connectors (US-512, S2).

Every connector is bound to ONE store (``source_system`` /
``source_store_id``) — that binding is what keeps events from ever
mixing between stores in a multi-store installation. The base
implements the three ``CommerceConnectorPort`` methods that do not
depend on the transport:

- :meth:`verify_event` — the shared domain verification
  (envelope + type + HMAC when the store has a secret). The secret
  lookup fails closed for a foreign store: an event for another
  storefront never verifies against this connector's secret.
- :meth:`normalize_event` — the shared domain normalization
  (money fields to whole Rial, using this store's Toman setting).
- :meth:`acknowledge` — a no-op: the polling transports are
  read-only, there is nothing to acknowledge back to the store.

Subclasses implement :meth:`fetch_changes` for their transport
(HTTP changes API / read-only store-DB read) and :meth:`close`
when they own a resource (HTTP client / DB engine).
"""

from __future__ import annotations

import datetime
from abc import ABC, abstractmethod

from hesabyar.domain.events.envelope import CanonicalEvent
from hesabyar.domain.events.errors import InvalidEventEnvelopeError
from hesabyar.domain.events.models import (
    IncomingEvent,
    NormalizedEvent,
    ProcessingResult,
    StoreSettings,
    VerifiedEvent,
)
from hesabyar.domain.events.normalization import normalize_payload
from hesabyar.domain.events.sync import DEFAULT_SYNC_PAGE_SIZE, SyncBatch, SyncCursor
from hesabyar.domain.events.verification import verify_incoming_event


class BaseStoreConnector(ABC):
    """One store's connector: bound to (source_system, source_store_id)."""

    def __init__(
        self,
        *,
        source_system: str,
        source_store_id: str,
        prices_in_toman: bool = True,
        webhook_secret: str | None = None,
    ) -> None:
        self._source_system = source_system
        self._source_store_id = source_store_id
        self._prices_in_toman = prices_in_toman
        self._webhook_secret = webhook_secret

    @property
    def source_system(self) -> str:
        """The connector's source system (the store key)."""
        return self._source_system

    @property
    def source_store_id(self) -> str:
        """The connector's store id within the source system."""
        return self._source_store_id

    def _secret_lookup(self, source_system: str, source_store_id: str) -> str | None:
        """Resolve this connector's webhook secret — only for its own store."""
        if source_system != self._source_system or source_store_id != self._source_store_id:
            raise InvalidEventEnvelopeError(
                f"This connector serves store {self._source_system}/{self._source_store_id}; "
                f"it cannot verify an event for {source_system}/{source_store_id}.",
                details={"source_system": source_system, "source_store_id": source_store_id},
            )
        return self._webhook_secret

    def verify_event(self, request: IncomingEvent) -> VerifiedEvent:
        """Verify an incoming event (envelope + type + HMAC when configured)."""
        return verify_incoming_event(request, secret_lookup=self._secret_lookup)

    def normalize_event(self, event: VerifiedEvent) -> NormalizedEvent:
        """Normalize a verified event (money fields to whole Rial)."""
        converted_payload, conversions = normalize_payload(
            event.event.payload,
            event.event_type,
            prices_in_toman=self._prices_in_toman,
        )
        return NormalizedEvent(
            event=event.event,
            event_type=event.event_type,
            store_settings=StoreSettings(
                source_system=self._source_system,
                source_store_id=self._source_store_id,
                prices_in_toman=self._prices_in_toman,
            ),
            converted_payload=converted_payload,
            conversions=conversions,
        )

    def acknowledge(self, event: CanonicalEvent, result: ProcessingResult) -> None:
        """No-op: the polling transports are read-only; nothing is acked back."""
        return None

    @abstractmethod
    def fetch_changes(
        self,
        cursor: SyncCursor | None,
        *,
        updated_since: datetime.datetime | None = None,
        page_size: int = DEFAULT_SYNC_PAGE_SIZE,
    ) -> SyncBatch:
        """Poll the connector for changes (implemented per transport)."""
        ...

    def close(self) -> None:
        """Release resources owned by the connector (no-op by default)."""
        return None
