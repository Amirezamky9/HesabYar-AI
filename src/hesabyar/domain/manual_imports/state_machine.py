"""Manual-import batch state machine (US-554).

The transition table for :class:`ManualImportBatchStatus
<hesabyar.domain.manual_imports.models.ManualImportBatchStatus>`:

- happy path: ``uploaded → mapped → validated → previewed →
  awaiting_review → approved → posted``;
- reject from any pre-approval state: ``→ rejected → archived``
  (the rejection carries its reason; the archived batch keeps its
  rows and is re-runnable);
- re-run: ``archived → mapped`` (a new attempt on the same batch,
  re-validating the stored rows — never silently dropped);
- ``approved → posted`` only (once approved, the pipeline runs;
  a held/failed posting keeps the batch ``approved`` with its
  ``posting_status`` explaining why, and the operator retries the
  posting step — the batch itself is not rejected after approval).

``posted`` is terminal.
"""

from __future__ import annotations

from hesabyar.domain.manual_imports.errors import ManualImportInvalidStateError
from hesabyar.domain.manual_imports.models import ManualImportBatchStatus

#: Allowed transitions: from-state → set of to-states.
MANUAL_IMPORT_TRANSITIONS: dict[ManualImportBatchStatus, frozenset[ManualImportBatchStatus]] = {
    ManualImportBatchStatus.UPLOADED: frozenset(
        {ManualImportBatchStatus.MAPPED, ManualImportBatchStatus.REJECTED}
    ),
    ManualImportBatchStatus.MAPPED: frozenset(
        {ManualImportBatchStatus.VALIDATED, ManualImportBatchStatus.REJECTED}
    ),
    ManualImportBatchStatus.VALIDATED: frozenset(
        {ManualImportBatchStatus.PREVIEWED, ManualImportBatchStatus.REJECTED}
    ),
    ManualImportBatchStatus.PREVIEWED: frozenset(
        {ManualImportBatchStatus.AWAITING_REVIEW, ManualImportBatchStatus.REJECTED}
    ),
    ManualImportBatchStatus.AWAITING_REVIEW: frozenset(
        {ManualImportBatchStatus.APPROVED, ManualImportBatchStatus.REJECTED}
    ),
    ManualImportBatchStatus.APPROVED: frozenset({ManualImportBatchStatus.POSTED}),
    ManualImportBatchStatus.POSTED: frozenset(),
    ManualImportBatchStatus.REJECTED: frozenset({ManualImportBatchStatus.ARCHIVED}),
    ManualImportBatchStatus.ARCHIVED: frozenset({ManualImportBatchStatus.MAPPED}),
}


def require_transition(
    current: ManualImportBatchStatus, target: ManualImportBatchStatus
) -> None:
    """Raise unless ``current → target`` is an allowed transition."""
    if target not in MANUAL_IMPORT_TRANSITIONS.get(current, frozenset()):
        raise ManualImportInvalidStateError(
            f"Import batch cannot move from {current.value} to {target.value}.",
            details={"from": current.value, "to": target.value},
        )


def can_transition(current: ManualImportBatchStatus, target: ManualImportBatchStatus) -> bool:
    """True when ``current → target`` is an allowed transition."""
    return target in MANUAL_IMPORT_TRANSITIONS.get(current, frozenset())
