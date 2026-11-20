"""Cheque domain errors (US-531, Phase 5-4 ۵ث).

Every error carries a stable machine-readable ``code`` so the REST
layer can map it to a problem-details response without string
matching, and a ``details`` mapping for the values a caller or an
operator needs to recover (never PII beyond what the caller supplied).
"""

from __future__ import annotations

from hesabyar.domain.common.errors import InvariantViolationError


class ChequeError(InvariantViolationError):
    """Base class for cheque-domain failures."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message, code=code)
        self.details: dict[str, object] = details or {}


class ChequeValidationError(ChequeError):
    """A cheque payload or command failed validation (fail-closed).

    ``details["field_errors"]`` maps field name -> human-readable
    problem; the REST layer renders it as a 422.
    """

    def __init__(self, message: str, *, field_errors: dict[str, str]) -> None:
        super().__init__(
            message, code="CHEQUE_VALIDATION_FAILED", details={"field_errors": field_errors}
        )
        self.field_errors = field_errors


class ChequeNotFoundError(ChequeError):
    """No cheque with the requested id exists in this tenant/installation."""

    def __init__(self, cheque_id: str) -> None:
        super().__init__(
            f"Cheque {cheque_id!r} does not exist.",
            code="CHEQUE_NOT_FOUND",
            details={"cheque_id": cheque_id},
        )


class ChequeTransitionNotAllowedError(ChequeError):
    """The requested state transition is not allowed from the current state.

    This is also the error for transitions the design deliberately
    disables — bounced -> in_clearing (re-presentment, §9.1 #5) —
    so a caller can never drive a cheque down a disabled path.
    """

    def __init__(self, from_state: str, transition: str, *, reason: str) -> None:
        super().__init__(
            f"Cheque cannot move from {from_state!r} via {transition!r}: {reason}",
            code="CHEQUE_TRANSITION_NOT_ALLOWED",
            details={"from_state": from_state, "transition": transition, "reason": reason},
        )


class ChequeDuplicateError(ChequeError):
    """A live cheque with the same sayad_id already exists (US-533 convergence).

    The DB partial unique index excludes ``voided`` /
    ``expired_unreceived`` / ``rejected_mismatch`` rows, so a
    re-registration after voiding, expiry or rejection is a new
    record, not a duplicate.
    """

    def __init__(self, sayad_id: str, *, existing_cheque_id: str) -> None:
        super().__init__(
            f"A live cheque with sayad_id {sayad_id!r} already exists "
            f"({existing_cheque_id!r}).",
            code="CHEQUE_DUPLICATE_SAYAD_ID",
            details={"sayad_id": sayad_id, "existing_cheque_id": existing_cheque_id},
        )


class ChequePreAcceptanceError(ChequeError):
    """Pre-acceptance (Sayyad item match + issuer credit) refused the transition.

    The cheque stays ``registered``; the operator's remedies are
    manager approval (when the installation policy allows it) or the
    ``registered -> rejected_mismatch`` path.
    """

    def __init__(self, message: str, *, verdict: str, details: dict[str, object] | None = None) -> None:
        super().__init__(message, code="CHEQUE_PRE_ACCEPTANCE_REFUSED", details={"verdict": verdict, **(details or {})})
        self.verdict = verdict


class ChequeEngineError(ChequeError):
    """The engine (account_check_ir) call failed or is unavailable.

    Raised by the Tryton adapter when the module is not installed, a
    REST action is not exposed, or the engine rejects the transition.
    The HesabYar registry is never written when this is raised: the
    engine is the atomic side of every transition.
    """

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message, code="CHEQUE_ENGINE_ERROR", details=details)


class ChequeRegistryDivergenceError(ChequeError):
    """The engine transition succeeded but the HesabYar registry write failed.

    The two systems now disagree: the engine holds the new state (and
    its journal) while the registry still shows the old state. This is
    never silent — the service files a ``cheque`` review-queue item
    carrying the engine ref so an operator can reconcile, and the
    error carries the engine state for recovery.
    """

    def __init__(
        self,
        message: str,
        *,
        cheque_id: str,
        engine_id: int,
        engine_state: str,
    ) -> None:
        super().__init__(
            message,
            code="CHEQUE_REGISTRY_DIVERGENCE",
            details={
                "cheque_id": cheque_id,
                "engine_id": engine_id,
                "engine_state": engine_state,
            },
        )
        self.cheque_id = cheque_id
        self.engine_id = engine_id
        self.engine_state = engine_state


class ChequePolicyError(ChequeError):
    """A cheque policy decision (gate / fiscal period / settings) refused the write."""

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message, code="CHEQUE_POLICY_DENIED", details=details)
