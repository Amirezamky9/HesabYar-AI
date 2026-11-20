"""Generic store connectors (US-512, S2 — spec §10.1, ADR 0003).

Two transports implement :class:`CommerceConnectorPort` for the
generic (custom-coded store) contract:

- :class:`GenericHttpStoreConnector` — poll the store's read-only
  HTTP changes API (the primary reconciliation transport).
- :class:`StoreDatabaseConnector` — read the store's database
  directly, read-only, reconciliation/backfill only (ADR 0003
  decision 3), via :class:`StoreDatabaseReader` + the versioned
  queries in :mod:`hesabyar.infrastructure.store_connectors.queries`.

:class:`StoreConnectorFactory` builds the right connector for a
sync run from the store registry.
"""

from hesabyar.infrastructure.store_connectors.common import BaseStoreConnector
from hesabyar.infrastructure.store_connectors.factory import StoreConnectorFactory
from hesabyar.infrastructure.store_connectors.http_connector import GenericHttpStoreConnector
from hesabyar.infrastructure.store_connectors.store_db import (
    StoreDatabaseConnector,
    StoreDatabaseReader,
    create_read_only_engine,
)

__all__ = [
    "BaseStoreConnector",
    "GenericHttpStoreConnector",
    "StoreConnectorFactory",
    "StoreDatabaseConnector",
    "StoreDatabaseReader",
    "create_read_only_engine",
]
