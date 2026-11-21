"""Mint a dev operator API key for the HesabYar admin dashboard (US-301).

Usage (from the repo root)::

    export HESABYAR_DATABASE_URL="postgresql+psycopg://hesabyar:hesabyar_pass@127.0.0.1:5432/hesabyar_test"
    uv run python apps/admin/scripts/mint-dev-key.py

The script provisions (idempotently) a dev tenant, API client and engine
mapping for the "HesabYar Adapter Test" company — the same pattern the
Phase 1 e2e fixtures use — then issues a fresh operator key with every
scope the dashboard needs and prints the secret ONCE.

Save it in ``apps/admin/.env.local`` (gitignored, never committed)::

    HESABYAR_DEV_API_KEY=<redacted>

Dev only: refuses to run against a database whose URL contains neither
"test" nor "dev" (override with HESABYAR_ALLOW_DEV_KEY=1).
"""

from __future__ import annotations

import os
import sys
import uuid

# The script lives in apps/admin/scripts/; the repo root (with src/) is
# three levels up. `uv run` from the repo root already puts src/ on the
# path via the installed package, but keep this working standalone too.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if os.path.join(REPO_ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter  # noqa: E402
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient  # noqa: E402
from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService  # noqa: E402
from hesabyar.infrastructure.config import AppSettings  # noqa: E402
from hesabyar.infrastructure.persistence.postgres.models import (  # noqa: E402
    ApiClientModel,
    Base,
    EngineCompanyModel,
    EngineConnectionModel,
    TenantModel,
)

DEFAULT_DB_URL = "postgresql+psycopg://hesabyar:hesabyar_pass@127.0.0.1:5432/hesabyar_test"
TEST_COMPANY_NAME = "HesabYar Adapter Test"
INSTALLATION_ID = "00000000-0000-0000-0000-000000000004"

DEV_TENANT_SLUG = "hesabyar-dev-admin"
DEV_CLIENT_NAME = "admin-dev-client"
DEV_CONNECTION_NAME = "tryton-dev"

# Every scope the Phase 3 dashboard needs (spec §14.2 catalog).
OPERATOR_SCOPES = [
    "company:read",
    "account:read",
    "ledger:read",
    "ledger:draft:create",
    "ledger:draft:update",
    "ledger:post",
    "ledger:reverse",
    "invoice:read",
    "invoice:draft:create",
    "payment:read",
    "banking:read",
    "banking:import",
    "banking:reconcile",
    "reports:read",
    "settings:manage",  # audit trail
    # Phase 4 (US-405 compliance center): standards, tax policy, Moadian.
    "standards:read",
    "standards:validate",
    "tax:read",
    "tax:decide",
    "moadian:read",
    "moadian:submit",
    # Phase 5 (US-504 review queue): the review-queue e2e seeds items via
    # POST /api/bff/review-queue/items, which requires review:file.
    "review:read",
    "review:resolve",
    "review:file",
    # Phase 5 wave 2 (US-513 manual entries): the manual-entry page calls
    # the BFF, which forwards the dev key to the backend's manual-entry
    # endpoints (integrations:read for list/get, integrations:manage for
    # create) and the integrations inbox/audit endpoints.
    "integrations:read",
    "integrations:manage",
    # Phase 5-2 (US-515 packaging): the packaging e2e seeds settings via
    # PUT /api/bff/packaging/settings and reads locations/materials.
    "packaging:read",
    "packaging:manage",
    # Phase 5-2 (US-516 cash box): the cash-box e2e registers boxes and
    # runs counting sessions via the BFF, which requires cashbox:*.
    "cashbox:read",
    "cashbox:count",
    "cashbox:manage",
    # Phase 5-3 (US-525 settlement center): the settlement pages read
    # payments/tranches/exceptions/fee-records/imports and the import
    # wizard + tie-out write through the BFF, which requires
    # settlement:*.
    "settlement:read",
    "settlement:manage",
    # Phase 5-4 (US-535 cheque center): the cheque pages read/write
    # cheques and run transitions through the BFF, which requires
    # cheques:* (the scopes exist in the catalog since US-530, but
    # the dev key minted before ۵ث lacked them — 403 on /cheques).
    "cheques:read",
    "cheques:write",
    "cheques:transition",
    # Phase 5-5 (US-545 installments center) + Phase 5-6 (US-551
    # unified dashboard): the installments pages and the dashboard's
    # money-on-the-way / alerts sections read plans + the overdue
    # report through the BFF, which requires installments:*.
    "installments:read",
    "installments:write",
    # Phase 5-7 (US-562 rules management UI): the workflows pages
    # read/write rules, dry-run, live-evaluate and read execution
    # history through the BFF, which requires rules:* (without them
    # the workflows page 403s — the same trap as cheques:* in ۵ث).
    "rules:read",
    "rules:manage",
    # Phase 5-7 (US-560 scheduler + US-564 reminders): the jobs
    # endpoints and the reminder preferences/log endpoints require
    # jobs:manage / reminders:manage (coordinator fix — neither
    # worker added them, and the CI Playwright job mints its key
    # from this script).
    "jobs:manage",
    "reminders:manage",
]


def main() -> int:
    settings = AppSettings()
    db_url = settings.database_url or DEFAULT_DB_URL
    if "test" not in db_url and "dev" not in db_url and os.getenv("HESABYAR_ALLOW_DEV_KEY") != "1":
        print(
            "REFUSED: the database URL looks like a non-dev database.\n"
            "Set HESABYAR_ALLOW_DEV_KEY=1 to override (not recommended).",
            file=sys.stderr,
        )
        return 2

    # --- find the Adapter Test company in live Tryton (by name, never a hardcoded id)
    rest = TrytonRestClient(settings.tryton.base_url, settings.tryton.database, settings.tryton.api_key)
    if not rest.ping():
        print(
            f"Tryton is not reachable at {settings.tryton.base_url} (start it per docs/dev-setup.md §3).",
            file=sys.stderr,
        )
        return 2
    engine_id: int | None = None
    for view in TrytonAccountingAdapter(rest).list_companies():
        if view.name == TEST_COMPANY_NAME:
            engine_id = view.ref.engine_id
            break
    rest.close()
    if engine_id is None:
        print(
            f"Test company {TEST_COMPANY_NAME!r} not found in Tryton DB {settings.tryton.database!r}. "
            "Provision it with setup_adapter_test_company.py (see docs/adapter.md).",
            file=sys.stderr,
        )
        return 2

    engine = create_engine(db_url, echo=False)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        tenant = session.query(TenantModel).filter(TenantModel.slug == DEV_TENANT_SLUG).one_or_none()
        if tenant is None:
            tenant = TenantModel(id=str(uuid.uuid4()), slug=DEV_TENANT_SLUG, name="HesabYar Dev Admin")
            session.add(tenant)
            session.flush()
            print(f"Created dev tenant {DEV_TENANT_SLUG} ({tenant.id}).")
        else:
            print(f"Reusing dev tenant {DEV_TENANT_SLUG} ({tenant.id}).")

        client = (
            session.query(ApiClientModel)
            .filter(ApiClientModel.tenant_id == tenant.id, ApiClientModel.name == DEV_CLIENT_NAME)
            .one_or_none()
        )
        if client is None:
            client = ApiClientModel(id=str(uuid.uuid4()), tenant_id=tenant.id, name=DEV_CLIENT_NAME)
            session.add(client)
            session.flush()
            print(f"Created API client {DEV_CLIENT_NAME}.")

        connection = (
            session.query(EngineConnectionModel)
            .filter(
                EngineConnectionModel.engine == "tryton",
                EngineConnectionModel.database_name == settings.tryton.database,
            )
            .one_or_none()
        )
        if connection is None:
            connection = EngineConnectionModel(
                id=str(uuid.uuid4()),
                name=DEV_CONNECTION_NAME,
                engine="tryton",
                base_url=settings.tryton.base_url,
                database_name=settings.tryton.database,
            )
            session.add(connection)
            session.flush()
            print(f"Created engine connection {DEV_CONNECTION_NAME}.")

        # Match the unique constraint uq_engine_companies_connection_external
        # (engine_connection_id, external_company_id): an earlier mint's
        # mapping survives even if its tenant row was later truncated by a
        # test run, so reuse it instead of inserting a duplicate.
        mapping = (
            session.query(EngineCompanyModel)
            .filter(
                EngineCompanyModel.engine_connection_id == connection.id,
                EngineCompanyModel.external_company_id == str(engine_id),
            )
            .one_or_none()
        )
        if mapping is None:
            mapping = EngineCompanyModel(
                id=str(uuid.uuid4()),
                tenant_id=tenant.id,
                engine_connection_id=connection.id,
                external_company_id=str(engine_id),
                company_name=TEST_COMPANY_NAME,
            )
            session.add(mapping)
            session.flush()
            print(f"Mapped tenant to {TEST_COMPANY_NAME} (engine id {engine_id}).")

        service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
        issued = service.issue_key(
            session,
            client=client,
            name="admin dev operator key",
            scopes=OPERATOR_SCOPES,
            installation_id=INSTALLATION_ID,
        )
        session.commit()
        # Read everything needed while the session is open: commit expires
        # attributes, and the session closes in the finally block below.
        secret = issued.secret
        key_id = issued.record.id
        key_prefix = issued.record.key_prefix
    finally:
        session.close()
        engine.dispose()

    print()
    print("Dev operator key minted (shown ONCE — it cannot be read back):")
    print()
    print(f"    {secret}")
    print()
    print("Save it in apps/admin/.env.local (gitignored, never commit):")
    print()
    print(f"    HESABYAR_DEV_API_KEY={secret}")
    print()
    print(f"Key id: {key_id} | prefix: {key_prefix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
