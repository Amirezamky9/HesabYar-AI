"""Idempotency-Key enforcement for mutating journal endpoints (US-202).

Spec §9.1 requires ``Idempotency-Key`` on mutating endpoints and §10.4
requires that reusing a key with a different payload hash is a conflict,
never silently the same operation. The guard implements both:

- missing, blank or over-long key → 400 ``IDEMPOTENCY_KEY_REQUIRED``;
- same key + same payload hash + completed response → the stored
  response is replayed verbatim (same status, same body), with no
  second engine call and no duplicate posting;
- same key + different payload hash → 409 ``IDEMPOTENCY_CONFLICT``;
- same key + same hash + still in progress → 409
  ``IDEMPOTENCY_IN_PROGRESS`` (retry later);
- a stale pending claim (a crashed request's row past its TTL) is
  released and the key re-claimed.

The claim insert commits immediately, so the key is visible to
concurrent requests from the moment the mutation starts — the unique
constraint on ``(installation_id, operation, idempotency_key)`` is the
arbiter, not application memory. A failed mutation releases its claim
so the key can be retried.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from hesabyar.infrastructure.persistence.postgres.idempotency import (
    IdempotencyStore,
    hash_payload,
)
from hesabyar.infrastructure.persistence.postgres.models import (
    IdempotencyRecordModel,
)
from hesabyar.interfaces.rest.errors import ApiError

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
#: The ``idempotency_records.idempotency_key`` column is VARCHAR(128).
MAX_IDEMPOTENCY_KEY_LENGTH = 128

#: Claim attempts before giving up to a concurrent request.
_MAX_CLAIM_ATTEMPTS = 3


@dataclass(frozen=True)
class Replay:
    """A stored response to return verbatim for a repeated key."""

    status_code: int
    body: dict[str, Any]


@dataclass
class IdempotencyClaim:
    """A held idempotency key for one in-flight mutation."""

    store: IdempotencyStore
    record: IdempotencyRecordModel
    payload_hash: str

    def complete(
        self,
        *,
        response_status: int,
        response_body: dict[str, Any],
        response_reference: str | None = None,
    ) -> None:
        """Store the completed response for future replays (flushes; caller commits)."""
        self.store.complete(
            self.record,
            response_status=response_status,
            response_body=response_body,
            response_reference=response_reference,
        )

    def release(self) -> None:
        """Release a pending claim after a failed mutation (flushes; caller commits)."""
        self.store.release(self.record)


def require_idempotency_key(raw: str | None) -> str:
    """Validate the ``Idempotency-Key`` header value (400 when unusable)."""
    if raw is None or not raw.strip():
        raise ApiError(
            status_code=400,
            code="IDEMPOTENCY_KEY_REQUIRED",
            detail=(
                "The Idempotency-Key header is required for this operation. "
                "Send a unique key per mutation; retries with the same key "
                "and payload replay the original response."
            ),
        )
    key = raw.strip()
    if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ApiError(
            status_code=400,
            code="IDEMPOTENCY_KEY_REQUIRED",
            detail=(
                f"The Idempotency-Key must be at most "
                f"{MAX_IDEMPOTENCY_KEY_LENGTH} characters."
            ),
        )
    return key


def _decide_existing(
    store: IdempotencyStore, record: IdempotencyRecordModel, request_hash: str
) -> Replay | None:
    """Decide what an existing record means for this request.

    Returns a :class:`Replay` for a completed record, releases a stale
    pending record and returns None (the caller claims the key afresh),
    or raises for a hash conflict or a live in-progress claim.
    """
    if record.request_hash != request_hash:
        raise ApiError(
            status_code=409,
            code="IDEMPOTENCY_CONFLICT",
            detail=(
                "The Idempotency-Key was already used with a different "
                "request payload. Use a new key for a different request."
            ),
        )
    if record.response_status is not None and record.response_body is not None:
        return Replay(
            status_code=record.response_status, body=dict(record.response_body)
        )
    if not IdempotencyStore.is_expired(record):
        raise ApiError(
            status_code=409,
            code="IDEMPOTENCY_IN_PROGRESS",
            detail=(
                "Another request with this Idempotency-Key is still in "
                "progress. Retry later with the same key."
            ),
        )
    # A crashed request's pending claim: release it and let the caller
    # re-claim the key.
    store.release(record)
    return None


def begin_mutation(
    session: Session,
    *,
    installation_id: str,
    operation: str,
    key: str,
    payload: dict[str, Any],
) -> IdempotencyClaim | Replay:
    """Claim an idempotency key for a mutation, or replay its result.

    Returns a :class:`Replay` when this exact operation already
    completed under the key, otherwise a :class:`IdempotencyClaim` the
    caller must :meth:`complete` on success or :meth:`release` on
    failure. The claim insert commits immediately (see module docstring).
    Raises ``ApiError`` for hash conflicts and live in-progress claims.
    """
    request_hash = hash_payload(payload)
    store = IdempotencyStore(session)
    for _ in range(_MAX_CLAIM_ATTEMPTS):
        existing = store.find(installation_id, operation, key)
        if existing is not None:
            replay = _decide_existing(store, existing, request_hash)
            if replay is not None:
                return replay
            # Stale pending claim released above; fall through to claim.
        record = store.claim(installation_id, operation, key, request_hash)
        if record is not None:
            session.commit()
            return IdempotencyClaim(
                store=store, record=record, payload_hash=request_hash
            )
        # Lost a race to another request: loop and re-read the winner.
    raise ApiError(
        status_code=409,
        code="IDEMPOTENCY_IN_PROGRESS",
        detail=(
            "Another request with this Idempotency-Key is still in "
            "progress. Retry later with the same key."
        ),
    )
