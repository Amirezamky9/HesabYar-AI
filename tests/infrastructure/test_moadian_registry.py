"""Infrastructure tests for the Moadian tables (US-404).

Schema-level checks against the shared Supabase test DB: the four
tables exist with the expected columns, the unique constraints reject
duplicates, the check constraints reject bad states, and no foreign
key points outside the HesabYar schema (spec §7.1). Sessions are
transactional and rolled back after each test.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.infrastructure.persistence.postgres.models import (
    Base,
    MoadianDocumentModel,
    MoadianProfileModel,
    MoadianStatusEventModel,
    MoadianSubmissionModel,
    TenantModel,
)

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

EXPECTED_COLUMNS = {
    "moadian_profiles": {
        "id",
        "tenant_id",
        "name",
        "slug",
        "environment",
        "sender_mode",
        "fiscal_id",
        "economic_code",
        "packet_type",
        "api_base_url",
        "protocol_version",
        "source_authority",
        "source_url",
        "snapshot_uri",
        "verification_status",
        "key_reference",
        "key_fingerprint",
        "is_active",
        "notes",
        "created_at",
        "updated_at",
    },
    "moadian_documents": {
        "id",
        "tenant_id",
        "profile_id",
        "invoice_id",
        "taxid",
        "serial",
        "invoice_type",
        "invoice_pattern",
        "invoice_subject",
        "payload",
        "payload_hash",
        "status",
        "created_at",
        "updated_at",
    },
    "moadian_submissions": {
        "id",
        "tenant_id",
        "document_id",
        "profile_id",
        "uid",
        "reference_number",
        "correlation_id",
        "environment",
        "scenario",
        "status",
        "retry",
        "approved_by",
        "approved_at",
        "approval_note",
        "request_hash",
        "response_hash",
        "request_payload",
        "response_payload",
        "outbox_event_id",
        "last_error_code",
        "last_error_detail",
        "created_at",
        "updated_at",
    },
    "moadian_status_events": {
        "id",
        "submission_id",
        "status",
        "source",
        "evidence",
        "correlation_id",
        "occurred_at",
        "created_at",
    },
}


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    eng = create_engine(TEST_DB_URL, echo=False)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine: Engine) -> Generator[Session, None, None]:
    connection = engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection)()
    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


@pytest.fixture()
def tenant(db: Session) -> TenantModel:
    row = TenantModel(
        id=str(uuid.uuid4()),
        slug=f"moad-infra-404-{uuid.uuid4().hex[:8]}",
        name="Moadian Infra Test Tenant",
    )
    db.add(row)
    db.flush()
    return row


def _profile(tenant_id: str, slug: str) -> MoadianProfileModel:
    return MoadianProfileModel(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        name="Infra profile",
        slug=slug,
        environment="test",
    )


def _document(tenant_id: str, profile_id: str, taxid: str) -> MoadianDocumentModel:
    return MoadianDocumentModel(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        profile_id=profile_id,
        taxid=taxid,
        invoice_type=1,
        invoice_pattern=1,
        invoice_subject=1,
        payload={"header": {}, "body": []},
        payload_hash="0" * 64,
    )


def _submission(tenant_id: str, document_id: str, profile_id: str, uid: str) -> MoadianSubmissionModel:
    return MoadianSubmissionModel(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        document_id=document_id,
        profile_id=profile_id,
        uid=uid,
        correlation_id=str(uuid.uuid4()),
        environment="test",
    )


def test_moadian_tables_exist_with_expected_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    for table, columns in EXPECTED_COLUMNS.items():
        assert table in tables, f"{table} missing"
        db_columns = {c["name"] for c in inspector.get_columns(table)}
        assert db_columns == columns, (
            f"{table}: DB-only={sorted(db_columns - columns)}, "
            f"model-only={sorted(columns - db_columns)}"
        )


def test_no_foreign_keys_leave_hesabyar_schema(engine: Engine) -> None:
    """Every FK target is a HesabYar-owned table (spec §7.1)."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    for table in EXPECTED_COLUMNS:
        for fk in inspector.get_foreign_keys(table):
            assert fk["referred_table"] in tables, (
                f"{table}.{fk['constrained_columns']} references "
                f"{fk['referred_table']}, which is not a HesabYar-owned table"
            )


def test_duplicate_profile_slug_rejected(db: Session, tenant: TenantModel) -> None:
    db.add(_profile(tenant.id, "infra-dup"))
    db.flush()
    db.add(_profile(tenant.id, "infra-dup"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_duplicate_document_taxid_rejected(db: Session, tenant: TenantModel) -> None:
    profile = _profile(tenant.id, "infra-taxid")
    db.add(profile)
    db.flush()
    db.add(_document(tenant.id, profile.id, "TS00000000000000000001"))
    db.flush()
    db.add(_document(tenant.id, profile.id, "TS00000000000000000001"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_duplicate_submission_uid_rejected(db: Session, tenant: TenantModel) -> None:
    profile = _profile(tenant.id, "infra-uid")
    db.add(profile)
    db.flush()
    document = _document(tenant.id, profile.id, "TS00000000000000000002")
    db.add(document)
    db.flush()
    uid = str(uuid.uuid4())
    db.add(_submission(tenant.id, document.id, profile.id, uid))
    db.flush()
    db.add(_submission(tenant.id, document.id, profile.id, uid))
    with pytest.raises(IntegrityError):
        db.flush()


def test_bad_environment_rejected(db: Session, tenant: TenantModel) -> None:
    profile = _profile(tenant.id, "infra-env")
    profile.environment = "sandbox"
    db.add(profile)
    with pytest.raises(IntegrityError):
        db.flush()


def test_bad_submission_status_rejected(db: Session, tenant: TenantModel) -> None:
    profile = _profile(tenant.id, "infra-status")
    db.add(profile)
    db.flush()
    document = _document(tenant.id, profile.id, "TS00000000000000000003")
    db.add(document)
    db.flush()
    submission = _submission(tenant.id, document.id, profile.id, str(uuid.uuid4()))
    submission.status = "flying"
    db.add(submission)
    with pytest.raises(IntegrityError):
        db.flush()


def test_approval_state_check_constraint(db: Session, tenant: TenantModel) -> None:
    """A non-draft submission without an approval violates the check."""
    import datetime

    profile = _profile(tenant.id, "infra-approval")
    db.add(profile)
    db.flush()
    document = _document(tenant.id, profile.id, "TS00000000000000000004")
    db.add(document)
    db.flush()
    submission = _submission(tenant.id, document.id, profile.id, str(uuid.uuid4()))
    submission.status = "approved"
    submission.approved_at = None
    db.add(submission)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()
    # The rollback removed the profile/document rows too; recreate them.
    tenant_row = TenantModel(
        id=str(uuid.uuid4()),
        slug=f"moad-infra-404-{uuid.uuid4().hex[:8]}",
        name="Moadian Infra Test Tenant 2",
    )
    db.add(tenant_row)
    db.flush()
    profile = _profile(tenant_row.id, "infra-approval-2")
    db.add(profile)
    db.flush()
    document = _document(tenant_row.id, profile.id, "TS00000000000000000008")
    db.add(document)
    db.flush()
    submission = _submission(tenant_row.id, document.id, profile.id, str(uuid.uuid4()))
    submission.status = "approved"
    submission.approved_by = "client-1"
    submission.approved_at = datetime.datetime.now(datetime.UTC)
    db.add(submission)
    db.flush()  # approved + approved_at is consistent


def test_bad_official_status_rejected(db: Session, tenant: TenantModel) -> None:
    profile = _profile(tenant.id, "infra-event")
    db.add(profile)
    db.flush()
    document = _document(tenant.id, profile.id, "TS00000000000000000005")
    db.add(document)
    db.flush()
    submission = _submission(tenant.id, document.id, profile.id, str(uuid.uuid4()))
    db.add(submission)
    db.flush()
    event = MoadianStatusEventModel(
        id=str(uuid.uuid4()),
        submission_id=submission.id,
        status="MYSTERIOUS",
    )
    db.add(event)
    with pytest.raises(IntegrityError):
        db.flush()


def test_invoice_type_range_rejected(db: Session, tenant: TenantModel) -> None:
    profile = _profile(tenant.id, "infra-range")
    db.add(profile)
    db.flush()
    document = _document(tenant.id, profile.id, "TS00000000000000000006")
    document.invoice_type = 9
    db.add(document)
    with pytest.raises(IntegrityError):
        db.flush()


def test_tenant_delete_cascades_to_moadian_rows(db: Session, tenant: TenantModel) -> None:
    profile = _profile(tenant.id, "infra-cascade")
    db.add(profile)
    db.flush()
    document = _document(tenant.id, profile.id, "TS00000000000000000007")
    db.add(document)
    db.flush()
    submission = _submission(tenant.id, document.id, profile.id, str(uuid.uuid4()))
    db.add(submission)
    db.flush()
    event = MoadianStatusEventModel(
        id=str(uuid.uuid4()),
        submission_id=submission.id,
        status="PENDING",
    )
    db.add(event)
    db.flush()
    profile_id = profile.id
    document_id = document.id
    submission_id = submission.id
    event_id = event.id
    db.delete(tenant)
    db.flush()
    # Expire the identity map: the deleted rows are gone from the
    # database (ON DELETE CASCADE), but the ORM objects linger in the
    # session cache until expired.
    db.expire_all()
    assert db.get(MoadianProfileModel, profile_id) is None
    assert db.get(MoadianDocumentModel, document_id) is None
    assert db.get(MoadianSubmissionModel, submission_id) is None
    assert db.get(MoadianStatusEventModel, event_id) is None
