"""Idempotency storage for mutating API operations (US-202, spec §10.4).

One row per ``(installation_id, operation, idempotency_key)`` — the
unique constraint the US-102 schema already enforces. A mutation claims
its key before executing (a pending row with no response), then stores
the completed response; a retry with the same key and the same request
hash replays the stored response, and a retry with a different hash is
a conflict — never silently treated as the same operation.

The store is deliberately dumb: it reads and writes rows and never
decides HTTP outcomes. The REST guard
(``hesabyar.interfaces.rest.idempotency``) interprets records into
replays, conflicts and in-progress errors, so this module needs no
knowledge of HTTP or of the operations it serves.

Like ``ApiKeyService``, the store flushes but never commits — the caller
owns the transaction.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from hesabyar.infrastructure.persistence.postgres.models import (
    IdempotencyRecordModel,
)

#: How long a pending (claimed but uncompleted) record blocks its key.
#: A crash between claim and completion leaves a pending row; after
#: this window the guard treats it as stale and lets a retry re-claim.
PENDING_TTL = timedelta(minutes=10)

#: How long a completed record is kept for replays. Expired records may
#: be purged by a later housekeeping story; expiry never changes a
#: stored response while the row still exists.
COMPLETED_TTL = timedelta(hours=24)


def hash_payload(payload: dict[str, Any]) -> str:
    """SHA-256 hex of a request payload's canonical JSON form.

    Keys are sorted and separators fixed so the same payload always
    hashes the same way, whatever order the client sent its fields in.
    """
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def utcnow() -> datetime:
    """Current UTC time, timezone-aware (matches the column type)."""
    return datetime.now(UTC)


class IdempotencyStore:
    """Read/write access to the ``idempotency_records`` table."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def find(
        self, installation_id: str, operation: str, key: str
    ) -> IdempotencyRecordModel | None:
        """Return the record for a key, or None when the key is unused."""
        stmt = select(IdempotencyRecordModel).where(
            IdempotencyRecordModel.installation_id == installation_id,
            IdempotencyRecordModel.operation == operation,
            IdempotencyRecordModel.idempotency_key == key,
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def claim(
        self, installation_id: str, operation: str, key: str, request_hash: str
    ) -> IdempotencyRecordModel | None:
        """Insert a pending record claiming the key for this operation.

        Returns the new record, or None when another request already
        holds the key (the unique constraint fired) — the caller then
        re-reads the existing record and decides replay vs conflict.
        The failed insert rolls back to a savepoint, so the session
        stays usable.
        """
        record = IdempotencyRecordModel(
            installation_id=installation_id,
            operation=operation,
            idempotency_key=key,
            request_hash=request_hash,
            expires_at=utcnow() + PENDING_TTL,
        )
        try:
            with self.session.begin_nested():
                self.session.add(record)
                self.session.flush()
        except IntegrityError:
            return None
        return record

    def complete(
        self,
        record: IdempotencyRecordModel,
        *,
        response_status: int,
        response_body: dict[str, Any],
        response_reference: str | None = None,
    ) -> None:
        """Store a completed response on a claimed record for replay."""
        record.response_status = response_status
        record.response_body = response_body
        record.response_reference = response_reference
        record.expires_at = utcnow() + COMPLETED_TTL
        self.session.flush()

    def release(self, record: IdempotencyRecordModel) -> None:
        """Delete a pending record after a failed operation.

        A failed mutation must not brick its key: the next retry with
        the same key re-claims and re-executes. Only pending records
        are released — a completed record is never deleted here.
        """
        self.session.delete(record)
        self.session.flush()

    @staticmethod
    def is_completed(record: IdempotencyRecordModel) -> bool:
        """True when the record holds a stored response for replay."""
        return (
            record.response_status is not None and record.response_body is not None
        )

    @staticmethod
    def is_expired(record: IdempotencyRecordModel) -> bool:
        """True when the record's expiry has passed (stale pending claim)."""
        return record.expires_at is not None and record.expires_at <= utcnow()
