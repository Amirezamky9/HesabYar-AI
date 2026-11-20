"""In-memory job queue (test/dev implementation of ``JobQueuePort``).

Records deferred jobs instead of scheduling them: tests assert on
what was deferred, and the periodic sweep (not the defer) is what
actually processes events in tests. Production uses
:class:`hesabyar.infrastructure.events.procrastinate_queue.ProcrastinateJobQueue`.
"""

from __future__ import annotations


class InMemoryJobQueue:
    """Job queue that records defers in memory (for tests)."""

    def __init__(self) -> None:
        self.deferred_events: list[str] = []
        self.sweeps_deferred: int = 0

    def defer_process_event(self, event_id: str) -> None:
        """Record a deferred process-event job."""
        self.deferred_events.append(event_id)

    def defer_sweep(self) -> None:
        """Record a deferred sweep job."""
        self.sweeps_deferred += 1
