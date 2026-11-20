"""Tests for the standards registry schema + seed (US-401).

Runs against the shared Supabase test DB in the same style as
test_phase1_schema.py: tables are created by Base.metadata.create_all
(a no-op once Alembic is at head) and each test runs in a rolled-back
transaction. The migration roundtrip itself lives in
test_migrations.py on a separate throwaway database.
"""

from __future__ import annotations

import datetime
import hashlib
import os
import uuid
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.infrastructure.persistence.postgres.models import (
    Base,
    StandardRevisionModel,
    StandardRuleModel,
    StandardSourceModel,
)
from hesabyar.infrastructure.persistence.postgres.seed_standards import (
    load_corpus,
    seed_standards,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

TRUNCATE_SQL = "TRUNCATE TABLE standard_rules, standard_revisions, standard_sources RESTART IDENTITY CASCADE;"

REGISTRY_TABLES = (
    "standard_sources",
    "standard_revisions",
    "standard_rules",
    "validation_runs",
    "validation_results",
)


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    """Create a SQLAlchemy engine connected to the test database."""
    eng = create_engine(TEST_DB_URL, echo=False)
    # Ensure tables exist without dropping schema or desynchronizing Alembic.
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.execute(text(TRUNCATE_SQL))
    yield eng
    with eng.begin() as conn:
        conn.execute(text(TRUNCATE_SQL))
    eng.dispose()


@pytest.fixture()
def db(engine: Engine) -> Generator[Session, None, None]:
    """Provide a transactional session that rolls back after each test."""
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


def test_models_match_migrated_schema(engine: Engine) -> None:
    """ORM models and the migrated schema agree on column names (US-401)."""
    inspector = inspect(engine)
    for table in REGISTRY_TABLES:
        assert inspector.has_table(table), f"missing table {table}"
        migrated = {column["name"] for column in inspector.get_columns(table)}
        model = {column.name for column in Base.metadata.tables[table].columns}
        assert model == migrated, f"{table}: model {model ^ migrated} != migrated"


def test_seed_creates_35_standards_36_rules(db: Session) -> None:
    counts = seed_standards(db)
    assert counts["sources_created"] == 35
    assert counts["revisions_created"] == 35
    assert counts["rules_created"] == 36
    assert len(db.execute(select(StandardSourceModel)).scalars().all()) == 35
    assert len(db.execute(select(StandardRuleModel)).scalars().all()) == 36


def test_seed_is_idempotent(db: Session) -> None:
    first = seed_standards(db)
    assert first["sources_created"] == 35
    second = seed_standards(db)
    assert second["sources_created"] == 0
    assert second["sources_updated"] == 35
    assert second["revisions_created"] == 0
    assert second["revisions_updated"] == 35
    assert second["rules_created"] == 0
    assert second["rules_updated"] == 36
    assert len(db.execute(select(StandardSourceModel)).scalars().all()) == 35
    assert len(db.execute(select(StandardRuleModel)).scalars().all()) == 36


def test_seed_verification_statuses(db: Session) -> None:
    seed_standards(db)
    revisions = {
        source.standard_code: revision
        for source in db.execute(select(StandardSourceModel)).scalars()
        for revision in db.execute(
            select(StandardRevisionModel).where(StandardRevisionModel.source_id == source.id)
        ).scalars()
    }
    assert len(revisions) == 35
    assert revisions["43"].verification_status == "VERIFIED_TEXT"
    assert revisions["44"].verification_status == "VERIFIED_TEXT"
    assert revisions["15"].verification_status == "BLOCKED_SOURCE_GAP"
    others = [revision for code, revision in revisions.items() if code not in ("15", "43", "44")]
    assert len(others) == 32
    assert {revision.verification_status for revision in others} == {"UNVERIFIED"}


def test_seed_standard_15_effective_range(db: Session) -> None:
    seed_standards(db)
    revision = db.execute(
        select(StandardRevisionModel)
        .join(
            StandardSourceModel,
            StandardRevisionModel.source_id == StandardSourceModel.id,
        )
        .where(StandardSourceModel.standard_code == "15")
    ).scalar_one()
    assert revision.effective_from == datetime.date(2001, 3, 21)  # 1380/01/01
    assert revision.effective_to == datetime.date(2026, 3, 20)  # 1404/12/29
    assert revision.revision_id == "1399"
    assert "revised-1404" in (revision.notes or "")


def test_seed_snapshot_hashes_match_files(db: Session) -> None:
    seed_standards(db)
    corpus = load_corpus()
    by_code = {str(s.number): s for s in corpus.standards}
    for source in db.execute(select(StandardSourceModel)).scalars():
        revision = db.execute(
            select(StandardRevisionModel).where(StandardRevisionModel.source_id == source.id)
        ).scalar_one()
        seed = by_code[source.standard_code]
        assert revision.sha256 == seed.sha256
        assert revision.sha256 == hashlib.sha256((REPO_ROOT / seed.snapshot_uri).read_bytes()).hexdigest()
        assert revision.snapshot_uri == seed.snapshot_uri


def test_seed_rules_ported_to_dsl(db: Session) -> None:
    seed_standards(db)
    rules = db.execute(select(StandardRuleModel)).scalars().all()
    assert len(rules) == 36
    executable = [rule for rule in rules if rule.is_executable]
    assert len(executable) == 35
    assert all(rule.dsl_formula is not None for rule in executable)
    assert all("0.01" not in (rule.dsl_formula or "") for rule in executable)
    non_executable = [rule for rule in rules if not rule.is_executable]
    assert [rule.rule_id for rule in non_executable] == ["CF005"]
    assert non_executable[0].rule_type == "requirement"
    assert non_executable[0].dsl_formula is None
    by_id = {rule.rule_id: rule for rule in rules}
    assert by_id["BS001"].dsl_formula is not None
    assert "1/100" in by_id["BS001"].dsl_formula
    assert by_id["BS-W03"].dsl_formula == "total_liabilities / total_assets <= 7/10"
    assert {rule.ruleset_version for rule in rules} == {"1.0.0-legacy"}


def test_unique_standard_code_rejected(db: Session) -> None:
    seed_standards(db)
    db.add(
        StandardSourceModel(
            id=str(uuid.uuid4()),
            standard_code="1",
            title="duplicate",
            slug="duplicate-slug",
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


def test_unique_revision_per_source_rejected(db: Session) -> None:
    seed_standards(db)
    source = db.execute(select(StandardSourceModel).where(StandardSourceModel.standard_code == "1")).scalar_one()
    db.add(
        StandardRevisionModel(
            id=str(uuid.uuid4()),
            source_id=source.id,
            revision_id="1397",
            title="duplicate",
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


def test_unique_rule_per_revision_rejected(db: Session) -> None:
    seed_standards(db)
    revision = db.execute(
        select(StandardRevisionModel)
        .join(
            StandardSourceModel,
            StandardRevisionModel.source_id == StandardSourceModel.id,
        )
        .where(StandardSourceModel.standard_code == "1")
    ).scalar_one()
    db.add(
        StandardRuleModel(
            id=str(uuid.uuid4()),
            revision_id=revision.id,
            rule_id="BS001",
            category="balance_sheet",
            description="duplicate",
            dsl_formula="1 <= 1",
            rule_type="equation",
            severity="error",
            is_executable=True,
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


def test_bad_verification_status_rejected(db: Session) -> None:
    seed_standards(db)
    source = db.execute(select(StandardSourceModel).where(StandardSourceModel.standard_code == "1")).scalar_one()
    db.add(
        StandardRevisionModel(
            id=str(uuid.uuid4()),
            source_id=source.id,
            revision_id="bogus",
            title="bogus",
            verification_status="MADE_UP",
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


def test_executable_rule_without_formula_rejected(db: Session) -> None:
    seed_standards(db)
    revision = db.execute(
        select(StandardRevisionModel)
        .join(
            StandardSourceModel,
            StandardRevisionModel.source_id == StandardSourceModel.id,
        )
        .where(StandardSourceModel.standard_code == "1")
    ).scalar_one()
    db.add(
        StandardRuleModel(
            id=str(uuid.uuid4()),
            revision_id=revision.id,
            rule_id="XX001",
            category="balance_sheet",
            description="executable but no formula",
            dsl_formula=None,
            rule_type="equation",
            severity="error",
            is_executable=True,
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


def test_effective_range_constraint_rejected(db: Session) -> None:
    seed_standards(db)
    source = db.execute(select(StandardSourceModel).where(StandardSourceModel.standard_code == "1")).scalar_one()
    db.add(
        StandardRevisionModel(
            id=str(uuid.uuid4()),
            source_id=source.id,
            revision_id="bogus",
            title="bogus",
            effective_from=datetime.date(2026, 3, 21),
            effective_to=datetime.date(2025, 3, 21),
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()
