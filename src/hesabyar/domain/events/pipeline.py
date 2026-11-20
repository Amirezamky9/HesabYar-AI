"""Pipeline mechanics for the §10.3 reliable-delivery pipeline (US-501).

Pure functions: bounded exponential backoff for transient failures.
The retryable/permanent classification lives in the application
layer (``hesabyar.application.commands.events``) because it reads
the application error taxonomy — the domain layer cannot import it.

Retry policy (phased design §6.3): transient failures (engine
unreachable, network errors) retry with bounded exponential backoff
— base 60s, doubling, capped at 1 hour, at most 5 attempts. After
the last attempt the event is dead-lettered (manual replay from the
UI, same idempotency key, no duplicate effect). Permanent failures
(unknown mapping, invalid data, amount mismatch) fail closed to the
review queue immediately — retrying them would never help.
"""

from __future__ import annotations

import datetime

#: Maximum processing attempts before an event is dead-lettered.
MAX_ATTEMPTS = 5

#: First retry delay (seconds); doubles on each subsequent attempt.
RETRY_BASE_SECONDS = 60

#: Maximum retry delay (seconds) — the backoff cap.
RETRY_MAX_SECONDS = 3600

#: How long a ``processing`` claim is held before another worker may
#: reclaim the event (a crashed worker's events are not stuck forever).
CLAIM_TIMEOUT = datetime.timedelta(minutes=10)

#: Default inbox-staleness threshold: an event stuck in
#: ``received``/``processing`` longer than this raises the alert.
DEFAULT_STALE_AFTER = datetime.timedelta(hours=24)

#: How long a completed idempotency record is kept for replays
#: (spec §10.4 TTL; expired records are purged by maintenance).
IDEMPOTENCY_TTL = datetime.timedelta(days=30)


def retry_delay_seconds(attempt: int) -> int:
    """Bounded exponential backoff for a 1-based failed-attempt count.

    Attempt 1 → 60s, 2 → 120s, 3 → 240s, 4 → 480s, 5+ → 960s…
    capped at 3600s.
    """
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    return min(RETRY_BASE_SECONDS * (2 ** (attempt - 1)), RETRY_MAX_SECONDS)


def next_retry_at(attempts: int, now: datetime.datetime) -> datetime.datetime | None:
    """When to retry next, or None when the event exhausted its attempts.

    ``attempts`` is the number of failed attempts so far. Returns
    None at ``MAX_ATTEMPTS`` — the caller dead-letters the event.
    """
    if attempts >= MAX_ATTEMPTS:
        return None
    # A direct process() call without a prior claim leaves attempts at
    # 0; treat it as the first attempt (60s) rather than failing.
    return now + datetime.timedelta(seconds=retry_delay_seconds(max(attempts, 1)))
