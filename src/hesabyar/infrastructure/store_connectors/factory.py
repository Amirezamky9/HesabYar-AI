"""Build one store's connector for a sync run (US-512, S2).

The factory resolves a store's polling transport from the registry:

- ``"api"`` — the store's HTTP changes API, authenticated with the
  scoped read-only API key stored encrypted in the registry
  (``api_key_ciphertext``), decrypted here via the credential vault.
  Needs ``api_base_url`` on the store row.
- ``"db"`` — the read-only store-DB read, authenticated with the
  read-only DSN stored encrypted in the registry
  (``store_db_dsn_ciphertext``). Needs a DSN stored for the store.

The caller (the REST route) resolves the store first — an unknown
store key is a 404 there, not a factory concern — and the vault is
a required constructor argument: the dependency layer fails closed
with 503 when no credential key is configured, so the factory never
sees a missing vault.

Lifecycle: the factory owns every connector it builds; :meth:`close`
disposes them all (HTTP clients, DB engines). Dependencies are
per-request, so the route closes the factory when the request ends.
"""

from __future__ import annotations

from typing import Protocol

from hesabyar.domain.events.errors import InvalidSyncRequestError, StoreNotConfiguredError
from hesabyar.domain.events.ports import CommerceConnectorPort
from hesabyar.domain.integrations.models import Store
from hesabyar.domain.integrations.ports import CredentialVaultPort
from hesabyar.infrastructure.store_connectors.http_connector import GenericHttpStoreConnector
from hesabyar.infrastructure.store_connectors.queries import DEFAULT_STORE_DB_QUERY_VERSION
from hesabyar.infrastructure.store_connectors.store_db import (
    StoreDatabaseConnector,
    StoreDatabaseReader,
    create_read_only_engine,
)

#: Sync sources the factory builds connectors for.
SYNC_SOURCES = ("api", "db")


class StoreCiphertextReader(Protocol):
    """The only registry capability the factory needs: ciphertext reads.

    The full ``IntegrationsRegistryPort`` is wider than the factory
    needs; depending on this narrow protocol keeps the factory
    testable with a tiny fake and honest about what it touches.
    """

    def get_store_ciphertext(self, store_id: str, kind: str) -> str | None:
        """Return the encrypted credential, or None when not stored."""


class StoreConnectorFactory:
    """Build :class:`CommerceConnectorPort` implementations from the registry."""

    def __init__(
        self,
        registry: StoreCiphertextReader,
        vault: CredentialVaultPort,
    ) -> None:
        self._registry = registry
        self._vault = vault
        self._built: list[GenericHttpStoreConnector | StoreDatabaseConnector] = []

    def build(
        self,
        store: Store,
        *,
        source_store_id: str,
        source: str,
    ) -> CommerceConnectorPort:
        """Build the connector for one store + sync source."""
        if source == "api":
            return self._build_http(store, source_store_id=source_store_id)
        if source == "db":
            return self._build_db(store, source_store_id=source_store_id)
        raise InvalidSyncRequestError(
            f"Unknown sync source {source!r}; expected one of: {', '.join(SYNC_SOURCES)}.",
            details={"source": source},
        )

    def _build_http(self, store: Store, *, source_store_id: str) -> GenericHttpStoreConnector:
        if not store.api_base_url:
            raise StoreNotConfiguredError(
                f"Store {store.store_key!r} has no API base URL configured; "
                "HTTP polling needs one.",
                details={"store_key": store.store_key},
            )
        ciphertext = self._registry.get_store_ciphertext(store.id, "api_key")
        if ciphertext is None:
            raise StoreNotConfiguredError(
                f"Store {store.store_key!r} has no read-only API key stored; "
                "HTTP polling needs one.",
                details={"store_key": store.store_key},
            )
        connector = GenericHttpStoreConnector(
            base_url=store.api_base_url,
            api_key=self._vault.decrypt(ciphertext),
            source_system=store.store_key,
            source_store_id=source_store_id,
            prices_in_toman=store.prices_in_toman,
        )
        self._built.append(connector)
        return connector

    def _build_db(self, store: Store, *, source_store_id: str) -> StoreDatabaseConnector:
        ciphertext = self._registry.get_store_ciphertext(store.id, "store_db_dsn")
        if ciphertext is None:
            raise StoreNotConfiguredError(
                f"Store {store.store_key!r} has no read-only database DSN stored; "
                "DB backfill needs one (POST /integrations/stores/{store_key}/db-credentials).",
                details={"store_key": store.store_key},
            )
        reader = StoreDatabaseReader(
            create_read_only_engine(self._vault.decrypt(ciphertext)),
            source_system=store.store_key,
            query_version=DEFAULT_STORE_DB_QUERY_VERSION,
        )
        connector = StoreDatabaseConnector(
            reader,
            source_system=store.store_key,
            source_store_id=source_store_id,
            prices_in_toman=store.prices_in_toman,
        )
        self._built.append(connector)
        return connector

    def close(self) -> None:
        """Dispose every connector built by this factory (idempotent)."""
        built, self._built = self._built, []
        for connector in built:
            connector.close()
