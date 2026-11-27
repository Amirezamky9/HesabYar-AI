"""REST tests for the bank import + reconcile endpoints (US-514, B1/B2).

Real app + real DB + real keys; only the engine is faked (an
in-memory FakeBankEngine). The bank tables are created by
``Base.metadata.create_all`` in the module engine fixture.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime
import os
import uuid
from collections.abc import Generator, Sequence
from dataclasses import dataclass
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from hesabyar.domain.accounting.commands import (
    CreateBankStatement,
    UpdateBankStatementLine,
)
from hesabyar.domain.accounting.refs import (
    AccountRef,
    BankStatementRef,
    CompanyRef,
    InvoiceRef,
    PartyRef,
    PaymentRef,
    StatementJournalRef,
)
from hesabyar.domain.accounting.value_objects import (
    AccountView,
    BankStatementLineView,
    BankStatementView,
    InvoiceLineView,
    InvoiceView,
    PaymentView,
    StatementJournalView,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.persistence.postgres.models import (
    ApiClientModel,
    Base,
    EngineCompanyModel,
    EngineConnectionModel,
    IdempotencyRecordModel,
    ReviewItemModel,
    TenantModel,
)
from hesabyar.interfaces.rest.app import create_app
from hesabyar.interfaces.rest.dependencies import get_engine_adapter

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

INSTALLATION_ID = "00000000-0000-0000-0000-000000000014"

ENGINE_A = 101
ENGINE_B = 102
PARTY_ID = 201
RECEIVABLE_ID = 301
REVENUE_ID = 401
SUSPENSE_ID = 900
JOURNAL_ID = 700
MISSING_ID = 999_999_999
SHABA = "IR062060000000100324200001"

ALL_SCOPES = ["banking:read", "banking:import", "banking:reconcile"]

CSV_TEXT = (
    "تاریخ,شرح,برداشت,واریز,مانده,شماره سند\n"
    "1405/08/26,واریز مشتری,,۱٬۰۰۰٬۰۰۰,۵٬۰۰۰٬۰۰۰,REF-1\n"
    "1405/08/26,پرداخت تأمین‌کننده,۲۵۰٬۰۰۰,,۴٬۷۵۰٬۰۰۰,REF-2\n"
)
CSV_B64 = base64.b64encode(CSV_TEXT.encode("utf-8")).decode("ascii")

COLUMNS = {
    "date": "تاریخ",
    "description": "شرح",
    "debit": "برداشت",
    "credit": "واریز",
    "balance": "مانده",
    "bank_ref": "شماره سند",
}


def _date(day: int = 17) -> FiscalDate:
    return FiscalDate.from_gregorian(datetime.date(2026, 11, day))


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


# ---------------------------------------------------------------------------
# Fake bank engine
# ---------------------------------------------------------------------------


class FakeBankEngine:
    """In-memory engine for the bank endpoints (statements + invoices + payments)."""

    def __init__(self) -> None:
        self._statements: dict[int, BankStatementView] = {}
        self._invoices: dict[int, InvoiceView] = {}
        self._payments: dict[int, PaymentView] = {}
        self._next_id = 1
        self._next_line_id = 9000

    # -- seeding -----------------------------------------------------------

    def seed_invoice(
        self, company_id: int = ENGINE_A, *, state: str = "posted", total: int = 1_000_000,
        day: int = 17, invoice_type: str = "out",
    ) -> int:
        invoice_id = self._next_id
        self._next_id += 1
        self._invoices[invoice_id] = InvoiceView(
            ref=InvoiceRef(invoice_id),
            company=CompanyRef(company_id),
            invoice_type=invoice_type,
            party=PartyRef(PARTY_ID),
            party_name="Acme Customer",
            number=f"INV-{invoice_id}",
            invoice_date=_date(day),
            accounting_date=_date(day),
            state=state,
            description="test invoice",
            reference=f"INV-{invoice_id}",
            journal=None,
            account=AccountRef(RECEIVABLE_ID),
            currency_code="IRR",
            untaxed_amount=MoneyIRR(total),
            tax_amount=MoneyIRR(0),
            total_amount=MoneyIRR(total),
            lines=(
                InvoiceLineView(
                    account=AccountRef(REVENUE_ID),
                    quantity=Decimal(1),
                    unit_price=MoneyIRR(total),
                    amount=MoneyIRR(total),
                ),
            ),
        )
        return invoice_id

    def seed_statement(
        self, company_id: int = ENGINE_A, *, state: str = "draft",
        lines: list[BankStatementLineView] | None = None,
    ) -> int:
        statement_id = self._next_id
        self._next_id += 1
        if lines is None:
            lines = [
                BankStatementLineView(
                    id=9001,
                    date=_date(),
                    amount=MoneyIRR(1_000_000),
                    account=AccountRef(SUSPENSE_ID),
                    description="واریز مشتری",
                ),
                BankStatementLineView(
                    id=9002,
                    date=_date(),
                    amount=MoneyIRR(-250_000),
                    account=AccountRef(SUSPENSE_ID),
                    description="پرداخت تأمین‌کننده",
                ),
            ]
        self._statements[statement_id] = BankStatementView(
            ref=BankStatementRef(statement_id),
            company=CompanyRef(company_id),
            name=f"Statement {statement_id}",
            journal_name="بانک ملی",
            journal_id=JOURNAL_ID,
            date=_date(),
            state=state,
            validation="balance",
            currency_code="IRR",
            start_balance=MoneyIRR(0),
            end_balance=MoneyIRR(750_000),
            balance=MoneyIRR(750_000),
            total_amount=None,
            number_of_lines=len(lines),
            lines=tuple(lines),
        )
        return statement_id

    # -- port: chart / journals --------------------------------------------

    def get_chart_of_accounts(self, company: CompanyRef) -> Sequence[AccountView]:
        return [
            AccountView(
                ref=AccountRef(100),
                company=company,
                code="1.1.1",
                name="بانک ملی",
                type_name="asset",
                parent=None,
                closed=False,
                party_required=False,
                active=True,
            ),
            AccountView(
                ref=AccountRef(SUSPENSE_ID),
                company=company,
                code="1.1.9",
                name="حساب معلق",
                type_name="asset",
                parent=None,
                closed=False,
                party_required=False,
                active=True,
            ),
            AccountView(
                ref=AccountRef(RECEIVABLE_ID),
                company=company,
                code="1.2.1",
                name="دریافتنی",
                type_name="asset",
                parent=None,
                closed=False,
                party_required=True,
                active=True,
            ),
        ]

    def list_statement_journals(self, company: CompanyRef) -> Sequence[StatementJournalView]:
        return [
            StatementJournalView(
                ref=StatementJournalRef(JOURNAL_ID),
                company=company,
                name="بانک ملی",
                account=AccountRef(100),
                validation="balance",
                currency_code="IRR",
                bank_account_number=SHABA,
            )
        ]

    # -- port: statements ----------------------------------------------------

    def get_bank_statement(self, statement: BankStatementRef) -> BankStatementView | None:
        return self._statements.get(statement.engine_id)

    def list_bank_statements(
        self, company: CompanyRef, state: str | None = None
    ) -> Sequence[BankStatementView]:
        return [
            view
            for view in self._statements.values()
            if view.company == company and (state is None or view.state == state)
        ]

    def create_bank_statement(self, command: CreateBankStatement) -> BankStatementRef:
        statement_id = self._next_id
        self._next_id += 1
        lines: list[BankStatementLineView] = []
        for line in command.lines:
            self._next_line_id += 1
            lines.append(
                BankStatementLineView(
                    id=self._next_line_id,
                    date=line.date,
                    amount=line.amount,
                    account=line.account,
                    description=line.description,
                    number=line.number,
                    party=line.party,
                    related_invoice=line.related_invoice,
                )
            )
        self._statements[statement_id] = BankStatementView(
            ref=BankStatementRef(statement_id),
            company=command.company,
            name=command.name,
            journal_name="بانک ملی",
            journal_id=command.journal.engine_id,
            date=command.date,
            state="draft",
            validation="balance",
            currency_code="IRR",
            start_balance=command.start_balance,
            end_balance=command.end_balance,
            balance=None,
            total_amount=None,
            number_of_lines=len(lines),
            lines=tuple(lines),
        )
        return BankStatementRef(statement_id)

    def update_bank_statement_line(self, command: UpdateBankStatementLine) -> None:
        view = self._statements[command.statement.engine_id]
        lines = []
        for line in view.lines:
            if line.id != command.line.engine_id:
                lines.append(line)
                continue
            lines.append(
                dataclasses.replace(
                    line,
                    account=command.account if command.account is not None else line.account,
                    party=command.party if command.party is not None else line.party,
                    related_invoice=(
                        None
                        if command.clear_related_invoice
                        else (
                            command.related_invoice
                            if command.related_invoice is not None
                            else line.related_invoice
                        )
                    ),
                )
            )
        self._statements[command.statement.engine_id] = dataclasses.replace(
            view, lines=tuple(lines)
        )

    # -- port: invoices / payments -------------------------------------------

    def list_invoices(
        self,
        company: CompanyRef,
        invoice_type: str | None = None,
        state: str | None = None,
    ) -> Sequence[InvoiceView]:
        return [
            view
            for view in self._invoices.values()
            if view.company == company
            and (invoice_type is None or view.invoice_type == invoice_type)
            and (state is None or view.state == state)
        ]

    def get_invoice(self, invoice: InvoiceRef) -> InvoiceView | None:
        return self._invoices.get(invoice.engine_id)

    def list_payments(
        self,
        company: CompanyRef,
        kind: str | None = None,
        state: str | None = None,
    ) -> Sequence[PaymentView]:
        return [
            view
            for view in self._payments.values()
            if view.company == company
            and (kind is None or view.kind == kind)
            and (state is None or view.state == state)
        ]

    def get_payment(self, payment: PaymentRef) -> PaymentView | None:
        return self._payments.get(payment.engine_id)


# ---------------------------------------------------------------------------
# Provisioned world (real DB, real keys)
# ---------------------------------------------------------------------------


@dataclass
class World:
    """Provisioned tenant, companies and keys for one test."""

    tenant_id: str
    company_a_id: str
    company_b_id: str
    client_id: str
    key_full_secret: str
    key_banking_read_secret: str


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    eng = create_engine(TEST_DB_URL, echo=False)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def settings() -> AppSettings:
    return AppSettings()


@pytest.fixture()
def fake_engine() -> FakeBankEngine:
    return FakeBankEngine()


@pytest.fixture()
def world(engine: Engine, settings: AppSettings) -> Generator[World, None, None]:
    """Provision a tenant mapped to fake engine companies 101 + 102."""
    session = sessionmaker(bind=engine)()
    service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
    tenant_id = ""
    connection_ids: list[str] = []
    try:
        tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"rest-514-{uuid.uuid4().hex[:8]}", name="514 Tenant"
        )
        session.add(tenant)
        session.flush()
        client = ApiClientModel(
            id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-514 client"
        )
        session.add(client)
        session.flush()

        def _company(external_id: str) -> EngineCompanyModel:
            connection = EngineConnectionModel(
                id=str(uuid.uuid4()),
                name=f"conn-{uuid.uuid4().hex[:8]}",
                engine="tryton",
                base_url="http://localhost:8000",
                database_name=f"hesabyar_tryton_test_{uuid.uuid4().hex[:8]}",
            )
            session.add(connection)
            session.flush()
            connection_ids.append(connection.id)
            row = EngineCompanyModel(
                id=str(uuid.uuid4()),
                tenant_id=tenant.id,
                engine_connection_id=connection.id,
                external_company_id=external_id,
                company_name=f"Company {external_id}",
            )
            session.add(row)
            session.flush()
            return row

        row_a = _company(str(ENGINE_A))
        row_b = _company(str(ENGINE_B))

        def _key(scopes: list[str]):
            return service.issue_key(
                session,
                client=client,
                name="rest-514 key",
                scopes=scopes,
                installation_id=INSTALLATION_ID,
            )

        key_full = _key(ALL_SCOPES)
        key_banking_read = _key(["banking:read"])
        session.commit()
        tenant_id = tenant.id
        yield World(
            tenant_id=tenant.id,
            company_a_id=row_a.id,
            company_b_id=row_b.id,
            client_id=client.id,
            key_full_secret=getattr(key_full, "secret"),
            key_banking_read_secret=getattr(key_banking_read, "secret"),
        )
    finally:
        session.close()
        cleanup = sessionmaker(bind=engine)()
        try:
            cleanup.execute(
                delete(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.installation_id == INSTALLATION_ID
                )
            )
            if tenant_id:
                cleanup.query(TenantModel).filter(
                    TenantModel.id == tenant_id
                ).delete(synchronize_session=False)
            for connection_id in connection_ids:
                cleanup.query(EngineConnectionModel).filter(
                    EngineConnectionModel.id == connection_id
                ).delete(synchronize_session=False)
            cleanup.commit()
        finally:
            cleanup.close()


@pytest.fixture()
def api(settings: AppSettings, fake_engine: FakeBankEngine) -> Generator[TestClient, None, None]:
    """The real app with the engine faked (auth + DB are real)."""
    app = create_app(settings=settings)
    app.dependency_overrides[get_engine_adapter] = lambda: fake_engine
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def _auth(secret: str, key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {secret}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _import_body(world: World, **overrides) -> dict:
    body = {
        "company": world.company_a_id,
        "bank_key": "melli",
        "statement_journal_id": JOURNAL_ID,
        "filename": "baam.csv",
        "content_base64": CSV_B64,
        "currency_unit": "rial",
        "default_account_id": SUSPENSE_ID,
        "columns": COLUMNS,
        "date_kind": "jalali",
        "header_row": 0,
    }
    body.update(overrides)
    return body


def _create_import(api: TestClient, world: World, key: str = "imp-1", **overrides) -> dict:
    response = api.post(
        "/api/v1/bank/statements/imports",
        json=_import_body(world, **overrides),
        headers=_auth(world.key_full_secret, key),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _review_items(engine: Engine, tenant_id: str) -> list[ReviewItemModel]:
    session = sessionmaker(bind=engine)()
    try:
        return list(
            session.query(ReviewItemModel)
            .filter(ReviewItemModel.tenant_id == tenant_id)
            .order_by(ReviewItemModel.created_at.asc())
            .all()
        )
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_detect_file_returns_columns_and_suggested_mapping(
    api: TestClient, world: World
) -> None:
    response = api.post(
        "/api/v1/bank/statements/imports/detect",
        json={"filename": "baam.csv", "content_base64": CSV_B64},
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["file_format"] == "csv"
    assert body["encoding"] == "utf-8"
    assert body["columns"][0] == "تاریخ"
    assert body["suggested_mapping"]["date"] == "تاریخ"
    assert body["suggested_mapping"]["description"] == "شرح"
    assert body["missing_fields"] == []
    assert len(body["sample_rows"]) == 2


def test_detect_file_rejects_bad_base64(api: TestClient, world: World) -> None:
    response = api.post(
        "/api/v1/bank/statements/imports/detect",
        json={"filename": "baam.csv", "content_base64": "!!!not-base64!!!"},
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 422


def test_detect_file_requires_import_scope(api: TestClient, world: World) -> None:
    response = api.post(
        "/api/v1/bank/statements/imports/detect",
        json={"filename": "baam.csv", "content_base64": CSV_B64},
        headers=_auth(world.key_banking_read_secret),
    )
    assert response.status_code == 403
    assert "banking:import" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Import lifecycle
# ---------------------------------------------------------------------------


def test_import_creates_batch_with_preview_and_review_item(
    api: TestClient, world: World, engine: Engine
) -> None:
    batch = _create_import(api, world)
    assert batch["status"] == "awaiting_review"
    assert batch["total_rows"] == 2
    assert batch["new_rows"] == 2
    assert batch["duplicate_rows"] == 0
    assert batch["invalid_rows"] == 0
    assert batch["total_deposits_irr"] == 1_000_000
    assert batch["total_withdrawals_irr"] == 250_000
    assert batch["engine_statement_id"] is None
    assert batch["review_item_id"] is not None
    assert len(batch["rows"]) == 2
    assert batch["rows"][0]["amount_irr"] == 1_000_000
    assert batch["rows"][0]["suggested_account_id"] == SUSPENSE_ID
    assert batch["rows"][0]["suggestion_source"] == "default"
    items = _review_items(engine, world.tenant_id)
    assert len(items) == 1
    assert items[0].item_type == "manual_import_review"
    assert items[0].status == "open"


def test_import_second_upload_dedups(api: TestClient, world: World) -> None:
    _create_import(api, world, key="imp-1")
    second = _create_import(api, world, key="imp-2")
    assert second["new_rows"] == 0
    assert second["duplicate_rows"] == 2
    assert all(row["is_duplicate"] for row in second["rows"])
    assert all(row["duplicate_reason"] == "bank_ref" for row in second["rows"])


def test_import_requires_idempotency_key(api: TestClient, world: World) -> None:
    response = api.post(
        "/api/v1/bank/statements/imports",
        json=_import_body(world),
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_import_rejects_unknown_journal(api: TestClient, world: World) -> None:
    response = api.post(
        "/api/v1/bank/statements/imports",
        json=_import_body(world, statement_journal_id=9999),
        headers=_auth(world.key_full_secret, "imp-journal"),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "BANK_STATEMENT_JOURNAL_NOT_FOUND"


def test_import_enforces_row_cap(api: TestClient, world: World) -> None:
    lines = ["تاریخ,شرح,مبلغ"] + [f"1405/08/26,ردیف {i},1000" for i in range(2001)]
    response = api.post(
        "/api/v1/bank/statements/imports",
        json=_import_body(
            world,
            content_base64=_b64("\n".join(lines)),
            columns={"date": "تاریخ", "description": "شرح", "amount": "مبلغ"},
        ),
        headers=_auth(world.key_full_secret, "imp-cap"),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "BANK_IMPORT_TOO_MANY_ROWS"


def test_import_get_and_list(api: TestClient, world: World) -> None:
    batch = _create_import(api, world)
    response = api.get(
        f"/api/v1/bank/statements/imports/{batch['id']}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 200
    assert response.json()["id"] == batch["id"]
    assert len(response.json()["rows"]) == 2
    response = api.get(
        f"/api/v1/bank/statements/imports?company={world.company_a_id}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [batch["id"]]
    response = api.get(
        "/api/v1/bank/statements/imports/does-not-exist",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "BANK_IMPORT_NOT_FOUND"


def test_confirm_import_writes_engine_statement(
    api: TestClient, world: World, engine: Engine, fake_engine: FakeBankEngine
) -> None:
    batch = _create_import(api, world)
    response = api.post(
        f"/api/v1/bank/statements/imports/{batch['id']}/confirm",
        headers=_auth(world.key_full_secret, "confirm-1"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "confirmed"
    assert body["engine_statement_id"] is not None
    statement_id = body["engine_statement_id"]
    # The statement exists in the engine as a draft with the coded lines.
    response = api.get(
        f"/api/v1/bank/statements/{statement_id}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 200
    statement = response.json()
    assert statement["state"] == "draft"
    assert len(statement["lines"]) == 2
    assert statement["lines"][0]["amount"] == 1_000_000
    assert statement["lines"][0]["account_id"] == SUSPENSE_ID
    assert statement["lines"][0]["number"] == "REF-1"
    # The review item is resolved.
    items = _review_items(engine, world.tenant_id)
    assert items[0].status == "resolved"
    # Confirming again is a conflict.
    response = api.post(
        f"/api/v1/bank/statements/imports/{batch['id']}/confirm",
        headers=_auth(world.key_full_secret, "confirm-2"),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "BANK_IMPORT_INVALID_STATE"


def test_cancel_import_writes_nothing(
    api: TestClient, world: World, engine: Engine, fake_engine: FakeBankEngine
) -> None:
    batch = _create_import(api, world)
    response = api.post(
        f"/api/v1/bank/statements/imports/{batch['id']}/cancel",
        headers=_auth(world.key_full_secret, "cancel-1"),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert fake_engine._statements == {}
    items = _review_items(engine, world.tenant_id)
    assert items[0].status == "resolved"


# ---------------------------------------------------------------------------
# Column mappings
# ---------------------------------------------------------------------------


def test_column_mapping_crud(api: TestClient, world: World) -> None:
    response = api.post(
        "/api/v1/bank/column-mappings",
        json={
            "bank_key": "melli",
            "name": "Baam",
            "file_format": "csv",
            "columns": COLUMNS,
            "date_kind": "jalali",
            "currency_unit": "rial",
            "header_row": 0,
        },
        headers=_auth(world.key_full_secret, "map-1"),
    )
    assert response.status_code == 201, response.text
    mapping = response.json()
    assert mapping["status"] == "active"
    response = api.get(
        "/api/v1/bank/column-mappings?bank_key=melli",
        headers=_auth(world.key_full_secret),
    )
    assert [item["id"] for item in response.json()] == [mapping["id"]]
    response = api.post(
        f"/api/v1/bank/column-mappings/{mapping['id']}/supersede",
        headers=_auth(world.key_full_secret, "map-2"),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "superseded"


def test_column_mapping_validation(api: TestClient, world: World) -> None:
    response = api.post(
        "/api/v1/bank/column-mappings",
        json={
            "bank_key": "melli",
            "name": "bad",
            "file_format": "csv",
            "columns": {"date": "تاریخ"},
        },
        headers=_auth(world.key_full_secret, "map-bad"),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "BANK_COLUMN_MAPPING_INVALID"


def test_import_with_saved_mapping(api: TestClient, world: World) -> None:
    response = api.post(
        "/api/v1/bank/column-mappings",
        json={
            "bank_key": "melli",
            "name": "Baam",
            "file_format": "csv",
            "columns": COLUMNS,
        },
        headers=_auth(world.key_full_secret, "map-3"),
    )
    mapping_id = response.json()["id"]
    batch = _create_import(api, world, mapping_id=mapping_id, columns=None)
    assert batch["column_mapping_id"] == mapping_id
    assert batch["new_rows"] == 2


# ---------------------------------------------------------------------------
# Journals + reconciliation status
# ---------------------------------------------------------------------------


def test_list_statement_journals(api: TestClient, world: World) -> None:
    response = api.get(
        f"/api/v1/bank/statement-journals?company={world.company_a_id}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 200
    journals = response.json()
    assert len(journals) == 1
    assert journals[0]["id"] == JOURNAL_ID
    assert journals[0]["bank_account_number"] == SHABA


# ---------------------------------------------------------------------------
# Reconcile + manual match + rules
# ---------------------------------------------------------------------------


def test_reconcile_matches_invoice_and_files_unmatched(
    api: TestClient, world: World, engine: Engine, fake_engine: FakeBankEngine
) -> None:
    statement_id = fake_engine.seed_statement()
    invoice_id = fake_engine.seed_invoice(state="posted", total=1_000_000, day=17)
    response = api.post(
        f"/api/v1/bank/statements/{statement_id}/reconcile",
        json={},
        headers=_auth(world.key_full_secret, "recon-1"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_lines"] == 2
    assert body["matched_count"] == 1
    assert body["priority2_count"] == 1
    assert body["unmatched_count"] == 1
    assert body["review_items_filed"] == 1
    assert body["lines"][0]["target_id"] == invoice_id
    # The match is recorded and the engine line is linked.
    response = api.get(
        f"/api/v1/bank/statements/{statement_id}/matches",
        headers=_auth(world.key_full_secret),
    )
    matches = response.json()
    assert len(matches) == 2
    auto = next(m for m in matches if m["status"] == "auto_applied")
    assert auto["target_id"] == invoice_id
    assert auto["destination_shaba"] == SHABA
    unmatched = next(m for m in matches if m["status"] == "unmatched")
    assert unmatched["review_item_id"] is not None
    items = _review_items(engine, world.tenant_id)
    assert len(items) == 1
    assert items[0].item_type == "bank_unmatched"
    # Re-running is idempotent: no duplicate review item.
    response = api.post(
        f"/api/v1/bank/statements/{statement_id}/reconcile",
        json={},
        headers=_auth(world.key_full_secret, "recon-2"),
    )
    assert response.json()["already_matched_count"] == 1
    assert response.json()["review_items_filed"] == 0
    assert len(_review_items(engine, world.tenant_id)) == 1


def test_reconcile_non_draft_is_a_conflict(
    api: TestClient, world: World, fake_engine: FakeBankEngine
) -> None:
    statement_id = fake_engine.seed_statement(state="posted")
    response = api.post(
        f"/api/v1/bank/statements/{statement_id}/reconcile",
        json={},
        headers=_auth(world.key_full_secret, "recon-3"),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "BANK_STATEMENT_NOT_DRAFT"


def test_manual_match_and_learned_rule(
    api: TestClient, world: World, fake_engine: FakeBankEngine
) -> None:
    statement_id = fake_engine.seed_statement()
    invoice_id = fake_engine.seed_invoice(state="posted", total=999_999, day=17)
    # The invoice does not P2-match (different amount); match it manually.
    response = api.post(
        f"/api/v1/bank/statements/{statement_id}/lines/9001/match",
        json={
            "target_type": "invoice",
            "target_id": str(invoice_id),
            "save_as_rule": True,
        },
        headers=_auth(world.key_full_secret, "match-1"),
    )
    assert response.status_code == 200, response.text
    match = response.json()
    assert match["status"] == "confirmed"
    assert match["target_id"] == invoice_id
    # The engine line is linked.
    response = api.get(
        f"/api/v1/bank/statements/{statement_id}",
        headers=_auth(world.key_full_secret),
    )
    assert response.json()["lines"][0]["related_invoice_id"] == invoice_id
    # The mapping was learned as a rule.
    response = api.get(
        f"/api/v1/bank/match-rules?company={world.company_a_id}",
        headers=_auth(world.key_full_secret),
    )
    rules = response.json()
    assert len(rules) == 1
    assert rules[0]["pattern"] == "واریز مشتری"
    assert rules[0]["mode"] == "suggest"
    # Matching the same line again is a conflict.
    response = api.post(
        f"/api/v1/bank/statements/{statement_id}/lines/9001/match",
        json={"target_type": "invoice", "target_id": str(invoice_id)},
        headers=_auth(world.key_full_secret, "match-2"),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "BANK_LINE_ALREADY_MATCHED"


def test_rule_mode_toggle(api: TestClient, world: World, fake_engine: FakeBankEngine) -> None:
    statement_id = fake_engine.seed_statement()
    api.post(
        f"/api/v1/bank/statements/{statement_id}/lines/9001/match",
        json={
            "target_type": "manual",
            "target_id": "manual",
            "account_id": SUSPENSE_ID,
            "save_as_rule": True,
        },
        headers=_auth(world.key_full_secret, "match-3"),
    )
    rules = api.get(
        f"/api/v1/bank/match-rules?company={world.company_a_id}",
        headers=_auth(world.key_full_secret),
    ).json()
    rule_id = rules[0]["id"]
    response = api.post(
        f"/api/v1/bank/match-rules/{rule_id}/mode",
        json={"mode": "auto"},
        headers=_auth(world.key_full_secret, "rule-1"),
    )
    assert response.status_code == 200
    assert response.json()["mode"] == "auto"


def test_reconciliation_status_report(
    api: TestClient, world: World, fake_engine: FakeBankEngine
) -> None:
    statement_id = fake_engine.seed_statement()
    fake_engine.seed_invoice(state="posted", total=1_000_000, day=17)
    api.post(
        f"/api/v1/bank/statements/{statement_id}/reconcile",
        json={},
        headers=_auth(world.key_full_secret, "recon-4"),
    )
    response = api.get(
        f"/api/v1/bank/reconciliation-status?company={world.company_a_id}"
        "&date_from=2026-11-01&date_to=2026-11-30",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["freshness"] == "live"
    assert body["lines_total"] == 2
    assert body["matched_lines"] == 1
    assert body["unmatched_lines"] == 1
    assert body["match_rate"] == 0.5
    assert body["unmatched_balance_irr"] == -250_000
    assert len(body["rows"]) == 1
    assert body["rows"][0]["journal_id"] == JOURNAL_ID
    assert body["rows"][0]["destination_shaba"] == SHABA
    assert body["rows"][0]["oldest_unmatched_line_id"] == 9002
