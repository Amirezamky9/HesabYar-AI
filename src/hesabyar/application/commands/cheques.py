"""Cheque command service (US-531 + US-532, Phase 5-4 ۵ث).

Received commercial cheques (§2.1), issued cheques (§2.3) and
collateral cheques (§2.2, memorandum-only): manual registration +
the full lifecycle with one atomic Tryton transaction per
transition.

Ordering (the atomicity rule): for every transition the service
calls the ENGINE FIRST (``AccountingEnginePort`` — state + journal
in one Tryton transaction inside ``account_check_ir``) and only
then mirrors the result into the HesabYar registry. An engine
failure leaves the registry untouched — neither a journal without a
state nor a state without its journal. If the registry write then
fails, the divergence is never silent: a ``cheque_exception`` review
item carrying the engine ref is filed and a
:class:`ChequeRegistryDivergenceError` is raised so an operator can
reconcile.

Registration is the exception: intake is a CLAIM in HesabYar, so
the registry row is created first (state ``registered``, no
journal); the engine link follows. A row whose engine link failed
keeps ``engine_id=None`` and a later registration with the same
sayad_id retries the link instead of duplicating — the recovery
path, not a second cheque.

Policy gate (F3/F4): every transition that creates a journal passes
the three-layer gate (event type + amount cap + period lock) through
:class:`PolicyGateService`; the gate's verdict travels to the engine
as ``auto_post``. Draft is the default — a journal auto-posts only
when the installation opted in AND the event type has an allowed
policy row AND the amount is within the cap AND the period is open.
When no gate service is wired (unit tests), every journal stays a
draft. Intake creates NO journal, so no gate applies to it — the
gate is evaluated at the first journal-creating transition
(``registered -> held``).

Site intake (US-533): :meth:`ChequeCommandService.register_site`
wires a ``cheque.received.registered`` event into the same registry
the manual form writes (dual-entry convergence on ``sayad_id``);
:meth:`ChequeCommandService.cancel_site_registration` voids an
intake before ``held`` (new ``void_intake`` transition, no journal)
and files a review item after ``held`` (manual return, never an
automatic void); :meth:`verify_leaf_against_sources` runs the
three-way mismatch matrix (site vs leaf vs Sayyad) and never
confirms on mismatch.

Installment legs (US-542, ۵ج): after a successful ``clear`` /
``bounce`` for a received cheque carrying an
``installment_leg_id``, the service fires the optional leg-outcome
hook (:class:`InstallmentLegOutcomeHook` — the installment module
listens on it and records the collection on clear). The hook adds
NO cheque behaviour: unwired it is a no-op, and a hook failure
never masks the transition (it is logged + filed as a review
item). The cheque lifecycle itself is closed.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from dataclasses import dataclass

from hesabyar.application.commands.integrations import PolicyGateService
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.domain.accounting.commands import CreateEngineCheque, TransitionEngineCheque
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import AccountRef, ChequeEngineRef, CompanyRef
from hesabyar.domain.cheques.errors import (
    ChequeDuplicateError,
    ChequeEngineError,
    ChequeNotFoundError,
    ChequePreAcceptanceError,
    ChequeRegistryDivergenceError,
    ChequeValidationError,
)
from hesabyar.domain.cheques.mismatch import (
    SourceReading,
    evaluate_three_way_mismatch,
)
from hesabyar.domain.cheques.models import (
    BounceDestination,
    Cheque,
    ChequeKind,
    ChequePurpose,
    ChequeSettings,
    ChequeState,
    CustodyLogEntry,
    InquiryChannel,
    InquiryReceipt,
    InquiryType,
    InstallmentLegStatus,
    SayadStatus,
    StorageLocation,
)
from hesabyar.domain.cheques.ports import (
    ChequeRepositoryPort,
    ChequeScanDownloadError,
    ChequeScanStorePort,
    InstallmentLegOutcomeHook,
    registration_to_cheque,
)
from hesabyar.domain.cheques.state_machine import (
    TRANSITION_BOUNCE,
    TRANSITION_CALL_IN,
    TRANSITION_CLEAR,
    TRANSITION_CONFIRM_RECEIPT,
    TRANSITION_DEPOSIT,
    TRANSITION_ENDORSE,
    TRANSITION_ENDORSED_BOUNCE,
    TRANSITION_EXPIRE,
    TRANSITION_ISSUE,
    TRANSITION_REJECT_MISMATCH,
    TRANSITION_RETURN,
    TRANSITION_UNEXPECTED_CALL_IN,
    TRANSITION_VOID,
    TRANSITION_VOID_INTAKE,
    PreAcceptanceInput,
    assert_transition_scope,
    evaluate_pre_acceptance,
    journal_design_for,
    leg_status_effect,
    target_state,
)
from hesabyar.domain.cheques.validation import (
    ChequeRegistration,
    parse_jalali_date,
    same_cheque_claim,
    validate_registration_payload,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.events.errors import MappingUnresolvedError
from hesabyar.domain.review_queue.models import ReviewItemType, ReviewSeverity

logger = logging.getLogger(__name__)

#: Internal policy-gate event-type keys, one per journal-creating
#: transition. These are NOT canonical events (the only canonical
#: cheque events are ``cheque.received.registered`` and
#: ``cheque.received.cancelled``, ADR 0004/US-530) — they are
#: dashboard-only keys the F3/F4 gate looks up per-event policy
#: rows by. With no policy row the gate denies auto-post and the
#: journal stays a draft, which is the required default.
GATE_EVENT_TYPES: dict[str, str] = {
    TRANSITION_CONFIRM_RECEIPT: "cheque.transition.held",
    TRANSITION_DEPOSIT: "cheque.transition.in_clearing",
    TRANSITION_CLEAR: "cheque.transition.cleared",
    TRANSITION_BOUNCE: "cheque.transition.bounced",
    TRANSITION_ENDORSE: "cheque.transition.endorsed",
    TRANSITION_RETURN: "cheque.transition.returned",
    TRANSITION_ISSUE: "cheque.transition.issued",
    TRANSITION_VOID: "cheque.transition.voided",
    TRANSITION_CALL_IN: "cheque.transition.called_in",
    TRANSITION_UNEXPECTED_CALL_IN: "cheque.transition.unexpected_call_in",
}

SYSTEM_ACTOR = "system:cheque-job"


@dataclass(frozen=True, slots=True)
class ChequeDailyReport:
    """Outcome of one daily cheque-job run (reminders + expiries)."""

    reminders_sent: int = 0
    expired: int = 0
    skipped: int = 0
    stale_in_clearing_filed: int = 0


@dataclass(frozen=True, slots=True)
class SiteCancellationOutcome:
    """Outcome of a site cancellation (``cheque.received.cancelled``).

    ``voided`` — the intake was still ``registered`` and is now
    ``voided`` (no journal). ``review_filed`` — the cheque had
    already reached ``held`` (or further), so nothing was voided
    automatically; a ``cancellation_after_held`` review item was
    filed for a manual return. ``already_terminal`` — the intake
    was already terminal (voided / expired / rejected); the
    cancellation replays as a no-op.
    """

    cheque: Cheque
    outcome: str  # "voided" | "review_filed" | "already_terminal"


class ChequeCommandService:
    """Write use cases for received commercial cheques."""

    def __init__(
        self,
        repository: ChequeRepositoryPort,
        engine: AccountingEnginePort,
        *,
        gate: PolicyGateService | None = None,
        review: ReviewQueueCommandService | None = None,
        today: datetime.date | None = None,
        scan_store: ChequeScanStorePort | None = None,
        leg_hook: InstallmentLegOutcomeHook | None = None,
    ) -> None:
        self._repository = repository
        self._engine = engine
        self._gate = gate
        self._review = review
        self._today = today
        self._scan_store = scan_store
        # The ۵ج leg-side listener (US-542): fired after a clear /
        # bounce for a received cheque carrying an installment_leg_id.
        # None = no listener (the cheque lifecycle is unchanged).
        self._leg_hook = leg_hook

    # -- helpers -----------------------------------------------------------

    def _clock_date(self) -> datetime.date:
        return self._today or datetime.date.today()

    def get_cheque(
        self, cheque_id: str, *, tenant_id: str, installation_id: str
    ) -> Cheque:
        """Load one cheque, tenant/installation-scoped (router access checks use this)."""
        return self._require_cheque(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )

    def get_by_sayad_id(
        self, sayad_id: str, *, tenant_id: str, installation_id: str
    ) -> Cheque | None:
        """Load the live cheque for a sayad_id, if any (US-533 intake).

        The pipeline's intake handler uses this after a duplicate
        conflict, to report the existing cheque's engine id without
        re-raising. Voided / expired / rejected rows are not live
        (the repository mirrors the partial unique index).
        """
        return self._repository.get_by_sayad_id(
            sayad_id, tenant_id=tenant_id, installation_id=installation_id
        )

    def _settings_for(self, installation_id: str) -> ChequeSettings:
        settings = self._repository.get_settings(installation_id)
        if settings is not None:
            return settings
        return ChequeSettings(installation_id=installation_id)

    def _require_cheque(
        self, cheque_id: str, *, tenant_id: str, installation_id: str
    ) -> Cheque:
        cheque = self._repository.get(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        if cheque is None:
            raise ChequeNotFoundError(cheque_id)
        return cheque

    def _require_engine_link(self, cheque: Cheque) -> ChequeEngineRef:
        if cheque.engine_id is None:
            raise ChequeEngineError(
                "Cheque is not linked to the engine yet (registration did not "
                "reach account_check_ir). Re-register with the same sayad_id "
                "to retry the engine link.",
                details={"cheque_id": cheque.id},
            )
        return ChequeEngineRef(engine_id=cheque.engine_id)

    def _gate_auto_post(
        self,
        *,
        installation_id: str,
        transition: str,
        company: CompanyRef,
        amount_irr: int,
        event_date: datetime.date,
    ) -> bool:
        """Evaluate the F3/F4 gate for one journal-creating transition.

        No gate service wired -> draft (False). A denied gate -> draft
        (False). Only an explicit allow posts the journal.
        """
        event_type = GATE_EVENT_TYPES.get(transition)
        if event_type is None or self._gate is None:
            return False
        decision = self._gate.evaluate(
            installation_id,
            event_type=event_type,
            company=company,
            event_date=event_date,
            amount_irr=amount_irr,
        )
        return decision.allowed

    def _file_review(
        self,
        *,
        tenant_id: str,
        cheque: Cheque,
        subtype: str,
        severity: ReviewSeverity,
        reason: str,
        suggested_action: str,
        actor: str,
    ) -> None:
        """File a cheque exception item; never lets a filing failure mask the caller's outcome."""
        if self._review is None:
            logger.warning(
                "No review service wired; cheque exception not filed: %s (%s)",
                subtype,
                cheque.id,
            )
            return
        try:
            self._review.file_item(
                tenant_id=tenant_id,
                item_type=ReviewItemType.CHEQUE_EXCEPTION,
                severity=severity,
                reason=reason,
                suggested_action=suggested_action,
                company_id=str(cheque.company_engine_id),
                subtype=subtype,
                source_event_ref=f"cheque:{cheque.id}",
                amount_irr=cheque.amount_irr,
                actor=actor,
            )
        except Exception:  # noqa: BLE001 - filing must never mask the transition outcome
            logger.exception("Failed to file cheque review item %s for %s", subtype, cheque.id)

    def _fire_leg_hook(
        self,
        cheque: Cheque,
        outcome: InstallmentLegStatus,
        event_date: datetime.date,
        *,
        actor: str,
    ) -> None:
        """Notify the installment leg listener of a clear/bounce (US-542).

        Only for a RECEIVED cheque carrying an ``installment_leg_id``,
        and only when a hook is wired. NEVER raises: the engine
        transition has already committed, so a hook failure is logged
        and filed as a review item instead — the leg's status is
        still correct (it derives from the cheque's state), only the
        collection record for US-541's income recognition may be
        missing, and that is repairable.
        """
        if self._leg_hook is None:
            return
        if cheque.kind != ChequeKind.RECEIVED or cheque.installment_leg_id is None:
            return
        try:
            self._leg_hook.on_leg_outcome(
                cheque=cheque, outcome=outcome, event_date=event_date
            )
        except Exception as exc:  # noqa: BLE001 - a hook failure must never mask the transition
            logger.exception(
                "Installment leg outcome hook failed for cheque %s (%s)",
                cheque.id,
                outcome.value,
            )
            self._file_review(
                tenant_id=cheque.tenant_id,
                cheque=cheque,
                subtype="installment_collection_hook_failed",
                severity=ReviewSeverity.WARNING,
                reason=(
                    f"The installment leg outcome hook failed after cheque "
                    f"{cheque.sayad_id} {outcome.value} ({exc}). The leg's "
                    "status is still correct (it derives from the cheque's "
                    "state), but the collection record for US-541's income "
                    "recognition may be missing."
                ),
                suggested_action=(
                    "Re-run the collection recording for this cheque's "
                    "installment leg (the receipt's collected_at), then "
                    "resolve this item."
                ),
                actor=actor,
            )

    def _apply_engine_result(
        self,
        cheque: Cheque,
        view: object,
        transition: str,
        *,
        actor: str,
        custody_to: StorageLocation | None = None,
        custody_reason: str | None = None,
    ) -> Cheque:
        """Mirror an engine transition into the registry.

        The engine has already committed (state + journal). This
        applies the same state + the move link to the registry row;
        on failure it files a divergence review item and raises, so
        the two systems never silently disagree.
        """
        from hesabyar.domain.accounting.value_objects import (  # noqa: PLC0415 - local to avoid a top-level value-object import cycle
            ChequeEngineView,
        )

        assert isinstance(view, ChequeEngineView)
        expected = target_state(cheque.state, transition)
        if view.state != expected.value:
            raise ChequeEngineError(
                f"Engine transition {transition!r} returned state {view.state!r}, "
                f"expected {expected.value!r}; the registry was not updated.",
                details={
                    "cheque_id": cheque.id,
                    "transition": transition,
                    "engine_state": view.state,
                    "expected_state": expected.value,
                },
            )
        design = journal_design_for(transition, cheque.kind, cheque.purpose)
        cheque.state = expected
        cheque.sayad_status = _sayad_status_after(cheque.sayad_status, transition)
        # A call-in commercialises the cheque: the engine flips its
        # purpose to ``payment`` in the same transaction (received
        # collateral after ۹ب, issued collateral after the memorandum
        # reversal). The registry mirrors the engine's purpose —
        # from here the cheque follows the commercial cycle and
        # appears in commercial reports (§2.2).
        if view.purpose in (ChequePurpose.PAYMENT.value, ChequePurpose.COLLATERAL.value):
            cheque.purpose = ChequePurpose(view.purpose)
        # Merge, don't replace: the engine read-back cannot attribute
        # moves to transitions (``moves`` is an unattributed one2many
        # of move ids), so the view carries only THIS transition's
        # move — the registry is the accumulator of the audit trail.
        cheque.move_ids = {**cheque.move_ids, **view.move_ids}
        if design is not None and transition not in view.move_ids and not cheque.journal_pending_review:
            logger.warning(
                "Engine transition %s for cheque %s returned no move for سند %s",
                transition,
                cheque.id,
                design.number,
            )
        if custody_to is not None:
            previous = cheque.storage_location
            cheque.storage_location = custody_to
            entry = CustodyLogEntry(
                cheque_id=cheque.id,
                from_location=previous,
                to_location=custody_to,
                actor=actor,
                reason=custody_reason or f"cheque {transition}",
            )
        else:
            entry = None
        cheque.touch()
        try:
            saved = self._repository.save(cheque)
            if entry is not None:
                self._repository.add_custody_entry(entry)
            return saved
        except Exception as exc:
            self._file_review(
                tenant_id=cheque.tenant_id,
                cheque=cheque,
                subtype="registry_divergence",
                severity=ReviewSeverity.CRITICAL,
                reason=(
                    f"Engine transition {transition!r} succeeded for cheque "
                    f"{cheque.id} (engine state {view.state!r}) but the "
                    "HesabYar registry write failed — the two systems disagree."
                ),
                suggested_action=(
                    "Reconcile the registry row with the engine record "
                    f"(engine cheque {view.ref.engine_id}, state {view.state!r}) "
                    "and re-apply the registry update; do NOT re-run the transition."
                ),
                actor=actor,
            )
            raise ChequeRegistryDivergenceError(
                "Engine transition succeeded but the registry write failed; "
                "a divergence review item was filed.",
                cheque_id=cheque.id,
                engine_id=view.ref.engine_id,
                engine_state=view.state,
            ) from exc

    def _engine_transition(
        self,
        cheque: Cheque,
        command: TransitionEngineCheque,
    ) -> object:
        """Call the engine transition; an engine failure leaves the registry untouched."""
        return self._engine.transition_engine_cheque(command)

    # -- registration (manual entry, §5) ------------------------------------

    def register_manual(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        company_engine_id: int,
        payload: dict[str, object],
        actor: str,
        kind: ChequeKind | str = ChequeKind.RECEIVED,
        checkbook_engine_id: int | None = None,
        secured_obligation: str | None = None,
    ) -> Cheque:
        """Register a cheque manually (intake, NO journal).

        The same 24 fields + validations as the site payload. A live
        cheque with the same sayad_id is a duplicate — unless its
        engine link failed, in which case the link is retried (the
        recovery path). ``kind`` / ``checkbook_engine_id`` /
        ``secured_obligation`` travel alongside the 24-field payload
        (they are NOT payload fields): an issued cheque MUST come
        from a checkbook (fail-closed — the engine validates the leaf
        against the checkbook range), a received cheque must NOT
        carry one, and a secured obligation belongs to a collateral
        cheque only. Dual-entry convergence (US-533, §6): a manual
        entry of a cheque the site already registered is the SAME
        claim — the identical payload returns the existing record
        (ONE record, not two); a contradicting payload files a
        ``duplicate_sayad_id`` review item and raises
        :class:`ChequeDuplicateError`.
        """
        kind_value = kind.value if isinstance(kind, ChequeKind) else kind
        try:
            kind_enum = ChequeKind(kind_value)
        except ValueError as exc:
            raise ChequeValidationError(
                f"Unknown cheque kind {kind_value!r}.",
                field_errors={"kind": "Must be 'received' or 'issued'."},
            ) from exc
        if kind_enum == ChequeKind.ISSUED and checkbook_engine_id is None:
            raise ChequeValidationError(
                "An issued cheque must come from a checkbook.",
                field_errors={
                    "checkbook_engine_id": "Required for kind='issued'."
                },
            )
        if kind_enum == ChequeKind.RECEIVED and checkbook_engine_id is not None:
            raise ChequeValidationError(
                "A received cheque has no checkbook.",
                field_errors={
                    "checkbook_engine_id": "Must be empty for kind='received'."
                },
            )
        obligation = (secured_obligation or "").strip() or None
        registration = validate_registration_payload(payload, kind=kind_enum)
        if obligation is not None and registration.purpose != ChequePurpose.COLLATERAL:
            raise ChequeValidationError(
                "A secured obligation belongs to a collateral cheque.",
                field_errors={
                    "secured_obligation": "Only purpose='collateral' secures an obligation."
                },
            )
        existing = self._repository.get_by_sayad_id(
            registration.sayad_id, tenant_id=tenant_id, installation_id=installation_id
        )
        if existing is not None and existing.engine_id is not None:
            if same_cheque_claim(registration, existing):
                return existing
            self._file_review(
                tenant_id=tenant_id,
                cheque=existing,
                subtype="duplicate_sayad_id",
                severity=ReviewSeverity.CRITICAL,
                reason=(
                    f"A cheque with sayad_id {registration.sayad_id} is already "
                    f"registered ({existing.id}, source {existing.source}), but "
                    "the new registration contradicts it "
                    f"(amount {registration.amount_irr} IRR, due "
                    f"{registration.due_date.date.isoformat()}, order "
                    f"{registration.order_id})."
                ),
                suggested_action=(
                    "Compare the two registrations (site vs manual) — "
                    "same leaf registered twice with different details, "
                    "or two leaves sharing a sayad_id. Keep exactly one "
                    "record; void the wrong one."
                ),
                actor=actor,
            )
            raise ChequeDuplicateError(registration.sayad_id, existing_cheque_id=existing.id)
        if existing is not None:
            return self._link_engine_cheque(existing, registration, actor=actor)
        cheque = registration_to_cheque(
            registration,
            cheque_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            installation_id=installation_id,
            company_engine_id=company_engine_id,
            source="manual",
            secured_obligation=obligation,
            checkbook_engine_id=checkbook_engine_id,
        )
        created = self._repository.create(cheque)
        return self._link_engine_cheque(created, registration, actor=actor)

    # -- registration from the site (US-533) ---------------------------------

    def register_site(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        company_engine_id: int,
        payload: dict[str, object],
        actor: str,
        source_event_id: str | None = None,
    ) -> Cheque:
        """Register a cheque from the store site (intake, NO journal).

        The ``cheque.received.registered`` consumer: the 24-field
        payload is validated fail-closed (the adapter boundary
        already did this too — defence in depth, same validator),
        the payee must be the mapped company, and the record lands
        in state ``registered`` with ``source="site"`` and no move.
        Identical re-registration returns the existing record
        (idempotent OK); a contradicting payload files a
        ``duplicate_sayad_id`` review item and raises
        :class:`ChequeDuplicateError` (§10.4). A scan URL is
        downloaded into the filestore best-effort — a failed
        download never fails the intake.
        """
        registration = validate_registration_payload(payload)
        self._check_payee_matches_company(company_engine_id, registration.payee_national_id)
        existing = self._repository.get_by_sayad_id(
            registration.sayad_id, tenant_id=tenant_id, installation_id=installation_id
        )
        if existing is not None and existing.engine_id is not None:
            if same_cheque_claim(registration, existing):
                return existing
            self._file_review(
                tenant_id=tenant_id,
                cheque=existing,
                subtype="duplicate_sayad_id",
                severity=ReviewSeverity.CRITICAL,
                reason=(
                    f"The site re-registered sayad_id {registration.sayad_id} "
                    f"({existing.id}, source {existing.source}) with a "
                    "different payload "
                    f"(amount {registration.amount_irr} IRR, due "
                    f"{registration.due_date.date.isoformat()}, order "
                    f"{registration.order_id})."
                ),
                suggested_action=(
                    "Compare the two registrations — same leaf "
                    "registered twice with different details, or two "
                    "leaves sharing a sayad_id. Keep exactly one "
                    "record; void the wrong one."
                ),
                actor=actor,
            )
            raise ChequeDuplicateError(registration.sayad_id, existing_cheque_id=existing.id)
        if existing is not None:
            return self._link_engine_cheque(existing, registration, actor=actor)
        cheque = registration_to_cheque(
            registration,
            cheque_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            installation_id=installation_id,
            company_engine_id=company_engine_id,
            source="site",
            source_event_id=source_event_id,
        )
        created = self._repository.create(cheque)
        created = self._download_scan(created)
        return self._link_engine_cheque(created, registration, actor=actor)

    def _check_payee_matches_company(
        self, company_engine_id: int, payee_national_id: str
    ) -> None:
        """A cheque payable to another business is rejected (§5 §1.2).

        The mapped company's national id is its Tryton party's
        ``code`` (the codebase's convention for national ids). A
        mismatch — or a company with no national id recorded —
        fails closed to the review queue via
        :class:`MappingUnresolvedError` (the pipeline routes it
        there); the intake is never created on an unverified payee.
        An engine failure surfaces as :class:`ChequeEngineError`
        (the caller retries; nothing is half-created).
        """
        try:
            companies = self._engine.list_companies()
        except Exception as exc:
            if isinstance(exc, ChequeEngineError):
                raise
            raise ChequeEngineError(
                f"Could not list engine companies for the payee check: {exc}",
                details={"company_engine_id": company_engine_id},
            ) from exc
        company = next(
            (view for view in companies if view.ref.engine_id == company_engine_id),
            None,
        )
        if company is None:
            raise MappingUnresolvedError(
                f"Mapped company {company_engine_id} is not known to the engine.",
                details={"company_engine_id": company_engine_id},
            )
        if not company.national_id:
            raise MappingUnresolvedError(
                f"The mapped company {company_engine_id} ({company.name}) has no "
                "national id recorded (its Tryton party has no code), so the "
                "cheque's payee cannot be verified against it.",
                details={"company_engine_id": company_engine_id},
            )
        if company.national_id != payee_national_id:
            raise MappingUnresolvedError(
                f"Cheque payee national id {payee_national_id!r} does not match "
                f"the mapped company ({company.name}, national id "
                f"{company.national_id!r}); a cheque payable to another "
                "business is rejected.",
                details={
                    "company_engine_id": company_engine_id,
                    "payee_national_id": payee_national_id,
                },
            )

    def _download_scan(self, cheque: Cheque) -> Cheque:
        """Download the site scan into the filestore (best-effort).

        A failed download never fails the intake: the ``scan_url``
        stays on the record and the scan can be uploaded manually
        later. On success the filestore ref replaces nothing — it
        is stored on ``scan_filestore_ref``; the site's own
        ``scan_ref`` is kept untouched.
        """
        if cheque.scan_url is None or self._scan_store is None:
            return cheque
        try:
            ref = self._scan_store.store_scan(
                sayad_id=cheque.sayad_id, scan_url=cheque.scan_url
            )
        except ChequeScanDownloadError as exc:
            logger.warning(
                "Cheque %s: leaf scan download failed (%s); intake continues "
                "with scan_url only.",
                cheque.id,
                exc,
            )
            return cheque
        cheque.scan_filestore_ref = ref
        cheque.touch()
        return self._repository.save(cheque)

    # -- site cancellation (US-533) ------------------------------------------

    def cancel_site_registration(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        sayad_id: str,
        actor: str,
        reason: str | None = None,
    ) -> SiteCancellationOutcome:
        """Handle ``cheque.received.cancelled`` from the site.

        Before ``held`` the intake is voided (``registered ->
        voided``, no journal — the engine's ``void_intake``
        transition). After ``held`` nothing is voided automatically:
        a ``cancellation_after_held`` review item is filed for a
        manual return. An already-terminal intake replays as a
        no-op. An unknown ``sayad_id`` fails closed to the review
        queue (:class:`MappingUnresolvedError`).
        """
        existing = self._repository.find_by_sayad_id(
            sayad_id, tenant_id=tenant_id, installation_id=installation_id
        )
        if existing is None:
            raise MappingUnresolvedError(
                f"Site cancellation names unknown sayad_id {sayad_id!r}: no "
                "cheque with that id exists in this installation.",
                details={"sayad_id": sayad_id},
            )
        if existing.is_terminal:
            return SiteCancellationOutcome(
                cheque=existing, outcome="already_terminal"
            )
        if existing.state != ChequeState.REGISTERED:
            self._file_review(
                tenant_id=tenant_id,
                cheque=existing,
                subtype="cancellation_after_held",
                severity=ReviewSeverity.WARNING,
                reason=(
                    f"The site cancelled cheque {sayad_id} after it reached "
                    f"{existing.state.value} — it was NOT voided automatically. "
                    + (f"Site reason: {reason.strip()}. " if reason else "")
                    + "The leaf must be returned manually."
                ),
                suggested_action=(
                    "Return the leaf to the issuer manually (the "
                    "``held -> returned`` transition, سند ۶) and resolve "
                    "the site order the cheque was paying for."
                ),
                actor=actor,
            )
            return SiteCancellationOutcome(cheque=existing, outcome="review_filed")
        target_state(existing.state, TRANSITION_VOID_INTAKE)
        view = self._engine_transition(
            existing,
            TransitionEngineCheque(
                cheque=self._require_engine_link(existing),
                company=CompanyRef(engine_id=existing.company_engine_id),
                transition=TRANSITION_VOID_INTAKE,
            ),
        )
        voided = self._apply_engine_result(
            existing, view, TRANSITION_VOID_INTAKE, actor=actor
        )
        return SiteCancellationOutcome(cheque=voided, outcome="voided")

    def void_intake(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        cheque_id: str,
        actor: str,
    ) -> Cheque:
        """Void a registered intake (``registered -> voided``, no journal).

        The manual/operator form of the site cancellation: only a
        ``registered`` cheque can be voided; anything further along
        must go through its own transition (or the review queue).
        """
        cheque = self._require_cheque(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        target_state(cheque.state, TRANSITION_VOID_INTAKE)
        view = self._engine_transition(
            cheque,
            TransitionEngineCheque(
                cheque=self._require_engine_link(cheque),
                company=CompanyRef(engine_id=cheque.company_engine_id),
                transition=TRANSITION_VOID_INTAKE,
            ),
        )
        return self._apply_engine_result(
            cheque, view, TRANSITION_VOID_INTAKE, actor=actor
        )

    def _link_engine_cheque(
        self, cheque: Cheque, registration: ChequeRegistration, *, actor: str
    ) -> Cheque:
        """Create the engine record for a registry row (or retry the link).

        On engine failure the registry row stays (a valid intake
        claim with ``engine_id=None``) and the error surfaces — the
        caller retries by registering the same sayad_id again.
        """
        try:
            view = self._engine.create_engine_cheque(
                CreateEngineCheque(
                    company=CompanyRef(engine_id=cheque.company_engine_id),
                    kind=cheque.kind.value,
                    purpose=cheque.purpose.value,
                    sayad_id=cheque.sayad_id,
                    amount=MoneyIRR(cheque.amount_irr),
                    issue_date=cheque.issue_date,
                    due_date=cheque.due_date,
                    issuer_name=cheque.issuer_name,
                    issuer_national_id=cheque.issuer_national_id,
                    issuer_bank=cheque.issuer_bank,
                    payee_name=cheque.payee_name,
                    payee_national_id=cheque.payee_national_id,
                    order_id=cheque.order_id,
                    customer_ref=cheque.customer_ref,
                    sayad_registered=cheque.sayad_registered,
                    source=cheque.source,
                    leaf_number=cheque.leaf_number,
                    issuer_branch=cheque.issuer_branch,
                    issuer_account_number=cheque.issuer_account_number,
                    reason=cheque.reason,
                    invoice_engine_id=_invoice_engine_id(cheque.invoice_id),
                    installment_leg_id=cheque.installment_leg_id,
                    scan_ref=cheque.scan_ref,
                    checkbook_engine_id=cheque.checkbook_engine_id,
                    secured_obligation=cheque.secured_obligation,
                )
            )
        except Exception as exc:
            if isinstance(exc, ChequeEngineError):
                raise
            raise ChequeEngineError(
                f"Engine cheque registration failed: {exc}",
                details={"cheque_id": cheque.id, "sayad_id": cheque.sayad_id},
            ) from exc
        cheque.engine_id = view.ref.engine_id
        cheque.touch()
        try:
            return self._repository.save(cheque)
        except Exception as exc:
            self._file_review(
                tenant_id=cheque.tenant_id,
                cheque=cheque,
                subtype="registry_divergence",
                severity=ReviewSeverity.CRITICAL,
                reason=(
                    f"Engine cheque {view.ref.engine_id} was created for "
                    f"cheque {cheque.id} but the registry link write failed."
                ),
                suggested_action=(
                    "Set the registry row's engine_id to "
                    f"{view.ref.engine_id} (do NOT create a second engine cheque)."
                ),
                actor=actor,
            )
            raise ChequeRegistryDivergenceError(
                "Engine cheque created but the registry link write failed; "
                "a divergence review item was filed.",
                cheque_id=cheque.id,
                engine_id=view.ref.engine_id,
                engine_state=view.state,
            ) from exc

    # -- confirm receipt: registered -> held (سند ۱) -------------------------

    def confirm_receipt(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        cheque_id: str,
        actor: str,
        items_matched: bool,
        sayad_accepted: bool,
        color_code: int | None = None,
        back_cheques_count: int | None = None,
        in_transit_commitments_irr: int | None = None,
        manager_approval: str | None = None,
        inquiry_receipt: dict[str, object] | None = None,
        storage_location: StorageLocation | None = None,
    ) -> Cheque:
        """Confirm the physical leaf arrived + Sayyad confirmed: registered -> held.

        Pre-acceptance (§2.1) runs first for a COMMERCIAL cheque:
        the four Sayyad items must match (a mismatch is never
        confirmed — use ``reject_mismatch``), the issuer colour +
        back-cheques are evaluated against the installation policy,
        and a white colour always carries its warning. A Sayyad
        acceptance (``sayad_accepted``) is required — the leaf must
        be accepted in Sayyad (bank app / SMS / Fintech), not just
        seen. For a COLLATERAL cheque the same item match + Sayyad
        acceptance are required, but the colour/policy verdict does
        not apply — no payment is being accepted, only a memorandum
        (سند انتظامی ۷); the colour is recorded when provided.
        """
        cheque = self._require_cheque(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        assert_transition_scope(
            cheque.kind, cheque.purpose, cheque.state, TRANSITION_CONFIRM_RECEIPT
        )
        target_state(cheque.state, TRANSITION_CONFIRM_RECEIPT)
        if not sayad_accepted:
            raise ChequeValidationError(
                "The leaf must be accepted in Sayyad before it can be held.",
                field_errors={
                    "sayad_accepted": "Sayyad acceptance is required for registered -> held."
                },
            )
        settings = self._settings_for(installation_id)
        if cheque.purpose == ChequePurpose.PAYMENT:
            verdict = evaluate_pre_acceptance(
                PreAcceptanceInput(
                    items_matched=items_matched,
                    color_code=color_code,
                    back_cheques_count=back_cheques_count,
                    in_transit_commitments_irr=in_transit_commitments_irr,
                ),
                red_brown_policy=settings.red_brown_policy,
                max_in_transit_commitments_irr=settings.max_in_transit_commitments_irr,
            )
            if verdict.decision == "reject":
                raise ChequePreAcceptanceError(
                    "Pre-acceptance refused this cheque: " + "; ".join(verdict.reasons),
                    verdict="reject",
                    details={"reasons": list(verdict.reasons)},
                )
            if verdict.decision == "manager_approval_required" and not (manager_approval or "").strip():
                raise ChequePreAcceptanceError(
                    "Pre-acceptance requires a manager's approval for this cheque: "
                    + "; ".join(verdict.reasons),
                    verdict="manager_approval_required",
                    details={"reasons": list(verdict.reasons)},
                )
        else:
            # Collateral: the item match is still never confirmed on
            # mismatch — the colour policy is a payment decision and
            # does not gate a memorandum.
            verdict = None
            if not items_matched:
                raise ChequePreAcceptanceError(
                    "Pre-acceptance refused this cheque: Sayyad items do not "
                    "match the leaf — never confirm on mismatch (§2.1).",
                    verdict="reject",
                    details={"reasons": ["Sayyad items do not match the leaf."]},
                )
        receipt: InquiryReceipt | None = None
        if inquiry_receipt is not None:
            fields = _coerce_receipt_fields(inquiry_receipt)
            cost = fields.get("cost_irr")
            receipt = self.record_inquiry_receipt(
                tenant_id=tenant_id,
                installation_id=installation_id,
                cheque_id=cheque.id,
                actor=actor,
                channel=str(fields["channel"]),
                inquiry_type=str(fields["inquiry_type"]),
                result_code=str(fields["result_code"]),
                result_summary=str(fields["result_summary"]),
                reference=str(fields["reference"]),
                cost_irr=int(cost) if isinstance(cost, int) else 0,
                requested_at=fields.get("requested_at"),  # type: ignore[arg-type]
                responded_at=fields.get("responded_at"),  # type: ignore[arg-type]
            )
        storage = storage_location or settings.default_storage_location
        auto_post = self._gate_auto_post(
            installation_id=installation_id,
            transition=TRANSITION_CONFIRM_RECEIPT,
            company=CompanyRef(engine_id=cheque.company_engine_id),
            amount_irr=cheque.amount_irr,
            event_date=self._clock_date(),
        )
        engine_ref = self._require_engine_link(cheque)
        view = self._engine_transition(
            cheque,
            TransitionEngineCheque(
                cheque=engine_ref,
                company=CompanyRef(engine_id=cheque.company_engine_id),
                transition=TRANSITION_CONFIRM_RECEIPT,
                auto_post=auto_post,
                storage_location=storage.value,
            ),
        )
        cheque.issuer_color_code = color_code
        if color_code is not None:
            cheque.issuer_color_inquired_at = datetime.datetime.now(datetime.UTC)
        cheque.issuer_back_cheques = back_cheques_count
        cheque.issuer_in_transit_commitments_irr = in_transit_commitments_irr
        if manager_approval:
            cheque.manager_approval_by = manager_approval.strip()
            cheque.manager_approval_at = datetime.datetime.now(datetime.UTC)
        cheque.receipt_missing_warning = receipt is None
        if verdict is not None and verdict.warnings:
            logger.info(
                "Cheque %s held with pre-acceptance warnings: %s",
                cheque.id,
                "; ".join(verdict.warnings),
            )
        return self._apply_engine_result(
            cheque,
            view,
            TRANSITION_CONFIRM_RECEIPT,
            actor=actor,
            custody_to=storage,
            custody_reason="physical leaf received; registered -> held (سند ۱)",
        )

    # -- deposit: held -> in_clearing (سند ۲) --------------------------------

    def deposit(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        cheque_id: str,
        actor: str,
        destination_account_id: int,
        destination_bank_name: str | None = None,
    ) -> Cheque:
        """Hand the leaf to the bank for clearing: held -> in_clearing.

        The destination bank account is chosen in this transition and
        recorded; the leaf's storage moves to ``bank`` with a custody
        entry.
        """
        cheque = self._require_cheque(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        assert_transition_scope(
            cheque.kind, cheque.purpose, cheque.state, TRANSITION_DEPOSIT
        )
        target_state(cheque.state, TRANSITION_DEPOSIT)
        auto_post = self._gate_auto_post(
            installation_id=installation_id,
            transition=TRANSITION_DEPOSIT,
            company=CompanyRef(engine_id=cheque.company_engine_id),
            amount_irr=cheque.amount_irr,
            event_date=self._clock_date(),
        )
        view = self._engine_transition(
            cheque,
            TransitionEngineCheque(
                cheque=self._require_engine_link(cheque),
                company=CompanyRef(engine_id=cheque.company_engine_id),
                transition=TRANSITION_DEPOSIT,
                auto_post=auto_post,
                destination_account=AccountRef(engine_id=destination_account_id),
            ),
        )
        cheque.destination_bank_account_id = destination_account_id
        cheque.destination_bank_name = destination_bank_name
        # US-534: the deposit date starts the clearing date window
        # (§1.5) — recorded here, at the held -> in_clearing
        # transition, and never backfilled afterwards.
        cheque.deposit_date = FiscalDate(self._clock_date())
        return self._apply_engine_result(
            cheque,
            view,
            TRANSITION_DEPOSIT,
            actor=actor,
            custody_to=StorageLocation.BANK,
            custody_reason="leaf delivered to the bank for clearing (سند ۲)",
        )

    # -- clear: in_clearing -> cleared (سند ۳) --------------------------------

    def clear(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        cheque_id: str,
        actor: str,
        statement_reference: str | None = None,
    ) -> Cheque:
        """Confirm clearing (manual confirmation; statement matching is US-534).

        Received: in_clearing -> cleared (سند ۳). Issued: issued ->
        cleared (سند ۱۱ — the bank account recorded at issue), or
        called_in -> cleared for an issued collateral cheque whose
        commercial journal the accountant has posted. The linked
        installment leg (when any, received only) is marked paid by
        the leg-status hook; the ۵ج module consumes the persisted
        effect, and the leg-outcome hook fires (US-542) so the
        installment module records the collection.
        """
        cheque = self._require_cheque(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        assert_transition_scope(
            cheque.kind, cheque.purpose, cheque.state, TRANSITION_CLEAR
        )
        target_state(cheque.state, TRANSITION_CLEAR)
        auto_post = self._gate_auto_post(
            installation_id=installation_id,
            transition=TRANSITION_CLEAR,
            company=CompanyRef(engine_id=cheque.company_engine_id),
            amount_irr=cheque.amount_irr,
            event_date=self._clock_date(),
        )
        view = self._engine_transition(
            cheque,
            TransitionEngineCheque(
                cheque=self._require_engine_link(cheque),
                company=CompanyRef(engine_id=cheque.company_engine_id),
                transition=TRANSITION_CLEAR,
                auto_post=auto_post,
                statement_reference=statement_reference,
                kind=cheque.kind.value,
            ),
        )
        if cheque.kind == ChequeKind.RECEIVED and cheque.installment_leg_id is not None:
            cheque.installment_leg_status = leg_status_effect(TRANSITION_CLEAR)
        saved = self._apply_engine_result(cheque, view, TRANSITION_CLEAR, actor=actor)
        self._fire_leg_hook(
            saved, InstallmentLegStatus.PAID, self._clock_date(), actor=actor
        )
        return saved

    # -- bounce: in_clearing -> bounced (سند ۴) / issued -> bounced (سند ۱۲) ---

    def bounce(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        cheque_id: str,
        actor: str,
        bounce_reason: str,
        bounce_date: str,
        certificate_reference: str | None = None,
        bounce_destination: BounceDestination | None = None,
    ) -> Cheque:
        """Record a bounce.

        Received (in_clearing -> bounced, سند ۴): a bounce NEVER
        happens automatically — it requires the bank's non-payment
        certificate (or an inquiry receipt) plus the reason and date,
        all recorded on the cheque. The reversal is dated in the
        CURRENT period (the engine enforces the open period; a closed
        period fails the transition). The claim returns to the
        customer, a dunning notification is filed to the review
        queue, and the linked installment leg (when any) is marked
        unpaid by the leg-status hook; the leg-outcome hook fires
        (US-542) so the installment module observes the bounce.
        Issued (issued -> bounced, سند ۱۲): our own leaf bounced
        (signature mismatch and the like) — the obligation returns
        to the supplier. No bank certificate exists for our own
        cheque, so reason + date are the evidence; the destination
        does not apply.
        """
        cheque = self._require_cheque(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        assert_transition_scope(
            cheque.kind, cheque.purpose, cheque.state, TRANSITION_BOUNCE
        )
        target_state(cheque.state, TRANSITION_BOUNCE)
        bounce_day = parse_jalali_date(bounce_date)
        settings = self._settings_for(installation_id)
        destination = bounce_destination or settings.bounce_destination
        if cheque.kind == ChequeKind.ISSUED:
            certificate = (certificate_reference or "").strip() or None
        elif not (certificate_reference or "").strip():
            raise ChequeValidationError(
                "A received bounce needs the bank's non-payment certificate.",
                field_errors={
                    "certificate_reference": "Required for a received cheque bounce."
                },
            )
        else:
            certificate = (certificate_reference or "").strip()
        auto_post = self._gate_auto_post(
            installation_id=installation_id,
            transition=TRANSITION_BOUNCE,
            company=CompanyRef(engine_id=cheque.company_engine_id),
            amount_irr=cheque.amount_irr,
            event_date=self._clock_date(),
        )
        view = self._engine_transition(
            cheque,
            TransitionEngineCheque(
                cheque=self._require_engine_link(cheque),
                company=CompanyRef(engine_id=cheque.company_engine_id),
                transition=TRANSITION_BOUNCE,
                auto_post=auto_post,
                bounce_destination=destination.value
                if cheque.kind == ChequeKind.RECEIVED
                else None,
                bounce_reason=bounce_reason.strip(),
                bounce_date=bounce_day,
                certificate_reference=certificate,
                kind=cheque.kind.value,
            ),
        )
        cheque.bounce_reason = bounce_reason.strip()
        cheque.bounce_date = bounce_day
        cheque.bounce_certificate_reference = certificate
        if cheque.kind == ChequeKind.RECEIVED:
            cheque.dunning_notified_at = datetime.datetime.now(datetime.UTC)
            if cheque.installment_leg_id is not None:
                cheque.installment_leg_status = leg_status_effect(TRANSITION_BOUNCE)
        saved = self._apply_engine_result(cheque, view, TRANSITION_BOUNCE, actor=actor)
        self._fire_leg_hook(
            saved, InstallmentLegStatus.UNPAID, bounce_day.date, actor=actor
        )
        if cheque.kind == ChequeKind.RECEIVED:
            self._file_review(
                tenant_id=tenant_id,
                cheque=saved,
                subtype="cheque_bounced",
                severity=ReviewSeverity.WARNING,
                reason=(
                    f"Cheque {saved.sayad_id} bounced on "
                    f"{bounce_day.date.isoformat()}: {bounce_reason.strip()}. "
                    "The claim returns to the customer; follow up (dunning, "
                    "article 23 enforcement where applicable)."
                ),
                suggested_action=(
                    "Start dunning for the customer; the bounced cheque's "
                    "Sayyad id and inquiry receipts support direct enforcement "
                    "(article 23). A replacement payment takes cash or a new cheque — "
                    "re-presentment is disabled."
                ),
                actor=actor,
            )
        else:
            self._file_review(
                tenant_id=tenant_id,
                cheque=saved,
                subtype="issued_cheque_bounced",
                severity=ReviewSeverity.WARNING,
                reason=(
                    f"Issued cheque {saved.sayad_id} bounced on "
                    f"{bounce_day.date.isoformat()}: {bounce_reason.strip()}. "
                    "The obligation returns to the beneficiary (سند ۱۲)."
                ),
                suggested_action=(
                    "Contact the bank for the bounce cause and re-issue or "
                    "settle with the beneficiary; the leaf is not re-presented."
                ),
                actor=actor,
            )
        return saved

    # -- endorse: held -> endorsed (سند ۵) --------------------------------------

    def endorse(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        cheque_id: str,
        actor: str,
        endorsee_name: str,
        endorsee_type: str,
        sayad_transfer_reference: str,
    ) -> Cheque:
        """Endorse the cheque on (spend it): held -> endorsed.

        The Sayyad transfer registration is REQUIRED — a handwritten
        endorsement has no legal effect (article 21 repeated). For an
        endorsee who is NOT a supplier the journal design is
        accountant-gated (§9.1 #12): the state moves and the transfer
        is registered, but NO journal is guessed — the journal is
        deferred and a review item is filed for the accountant.
        """
        cheque = self._require_cheque(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        assert_transition_scope(
            cheque.kind, cheque.purpose, cheque.state, TRANSITION_ENDORSE
        )
        target_state(cheque.state, TRANSITION_ENDORSE)
        if endorsee_type not in ("supplier", "other"):
            raise ChequeValidationError(
                "endorsee_type must be 'supplier' or 'other'.",
                field_errors={"endorsee_type": "Must be 'supplier' or 'other'."},
            )
        defer_journal = endorsee_type == "other"
        auto_post = self._gate_auto_post(
            installation_id=installation_id,
            transition=TRANSITION_ENDORSE,
            company=CompanyRef(engine_id=cheque.company_engine_id),
            amount_irr=cheque.amount_irr,
            event_date=self._clock_date(),
        )
        view = self._engine_transition(
            cheque,
            TransitionEngineCheque(
                cheque=self._require_engine_link(cheque),
                company=CompanyRef(engine_id=cheque.company_engine_id),
                transition=TRANSITION_ENDORSE,
                auto_post=auto_post,
                endorsee_name=endorsee_name.strip(),
                endorsee_type=endorsee_type,
                sayad_transfer_reference=sayad_transfer_reference.strip(),
                defer_journal=defer_journal,
            ),
        )
        cheque.endorsee_name = endorsee_name.strip()
        cheque.endorsee_type = endorsee_type
        cheque.sayad_transfer_reference = sayad_transfer_reference.strip()
        cheque.journal_pending_review = defer_journal
        saved = self._apply_engine_result(
            cheque,
            view,
            TRANSITION_ENDORSE,
            actor=actor,
            custody_to=StorageLocation.WITH_ENDORSEE,
            custody_reason="leaf endorsed on to the endorsee (سند ۵)",
        )
        if defer_journal:
            self._file_review(
                tenant_id=tenant_id,
                cheque=saved,
                subtype="endorsement_to_other_journal",
                severity=ReviewSeverity.WARNING,
                reason=(
                    f"Cheque {saved.sayad_id} was endorsed to a non-supplier "
                    f"({endorsee_name.strip()}); the journal design for this "
                    "endorsement is accountant-gated (§9.1 #12) and was NOT guessed."
                ),
                suggested_action=(
                    "Ask the accountant for the debit account of the "
                    "endorsement-to-other journal, then post it against the "
                    "endorsed cheque."
                ),
                actor=actor,
            )
        return saved

    # -- return: held -> returned (سند ۶) / bounced -> returned (no journal) ---

    def return_cheque(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        cheque_id: str,
        actor: str,
    ) -> Cheque:
        """Return the leaf without clearing.

        Received, from ``held``: سند ۶ (commercial) or سند انتظامی ۸
        (collateral — memorandum reversal); from ``bounced`` it
        carries NO journal (the claim was re-established by سند ۴ and
        stays until paid). Issued collateral, from ``issued``: the
        counterparty returns our leaf after the obligation ends —
        memorandum سند ۸ in the issued direction, and the leaf comes
        back into our vault (the custody entry's origin is empty:
        the leaf was outside our custody, with the counterparty —
        the engine log cannot record an empty origin, so this move
        exists only in the registry log).
        """
        cheque = self._require_cheque(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        assert_transition_scope(
            cheque.kind, cheque.purpose, cheque.state, TRANSITION_RETURN
        )
        target_state(cheque.state, TRANSITION_RETURN)
        auto_post = self._gate_auto_post(
            installation_id=installation_id,
            transition=TRANSITION_RETURN,
            company=CompanyRef(engine_id=cheque.company_engine_id),
            amount_irr=cheque.amount_irr,
            event_date=self._clock_date(),
        )
        view = self._engine_transition(
            cheque,
            TransitionEngineCheque(
                cheque=self._require_engine_link(cheque),
                company=CompanyRef(engine_id=cheque.company_engine_id),
                transition=TRANSITION_RETURN,
                auto_post=auto_post,
                kind=cheque.kind.value,
            ),
        )
        if cheque.kind == ChequeKind.ISSUED:
            return self._apply_engine_result(
                cheque,
                view,
                TRANSITION_RETURN,
                actor=actor,
                custody_to=StorageLocation.VAULT,
                custody_reason="issued collateral leaf returned by the counterparty",
            )
        return self._apply_engine_result(
            cheque,
            view,
            TRANSITION_RETURN,
            actor=actor,
            custody_to=StorageLocation.RETURNED_TO_ISSUER,
            custody_reason="leaf returned to the issuer without clearing",
        )

    # -- issue: registered -> issued (سند ۱۰ / memorandum) -------------------

    def issue_cheque(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        cheque_id: str,
        actor: str,
        destination_account_id: int,
        destination_bank_name: str | None = None,
    ) -> Cheque:
        """Issue a cheque from our checkbook: registered -> issued.

        The leaf leaves the checkbook for the beneficiary (the
        payee — the engine party). Commercial (سند ۱۰: Dr Trade
        Payables / Cr Notes Payable); collateral issues the
        memorandum in the issued direction (Dr اسناد تضمینی نزد
        دیگران / Cr طرف حساب اسناد تضمینی نزد دیگران — never a
        commercial account). The bank account the cheque will clear
        against is recorded now (used by سند ۱۱ at clear); the
        checkbook itself was fixed at registration. The leaf is
        registered in Sayyad at issue (Fintech ac-sayad-issue-cheque
        for legal entities, bank app/branch otherwise).
        """
        cheque = self._require_cheque(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        assert_transition_scope(
            cheque.kind, cheque.purpose, cheque.state, TRANSITION_ISSUE
        )
        target_state(cheque.state, TRANSITION_ISSUE)
        if cheque.checkbook_engine_id is None:
            raise ChequeEngineError(
                "Cheque has no checkbook — an issued cheque must come "
                "from one of the company's checkbooks.",
                details={"cheque_id": cheque.id},
            )
        auto_post = self._gate_auto_post(
            installation_id=installation_id,
            transition=TRANSITION_ISSUE,
            company=CompanyRef(engine_id=cheque.company_engine_id),
            amount_irr=cheque.amount_irr,
            event_date=self._clock_date(),
        )
        view = self._engine_transition(
            cheque,
            TransitionEngineCheque(
                cheque=self._require_engine_link(cheque),
                company=CompanyRef(engine_id=cheque.company_engine_id),
                transition=TRANSITION_ISSUE,
                auto_post=auto_post,
                destination_account=AccountRef(engine_id=destination_account_id),
                kind=cheque.kind.value,
            ),
        )
        cheque.destination_bank_account_id = destination_account_id
        cheque.destination_bank_name = destination_bank_name
        return self._apply_engine_result(cheque, view, TRANSITION_ISSUE, actor=actor)

    # -- void: issued -> voided (سند ۱۳) ---------------------------------------

    def void_issued(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        cheque_id: str,
        actor: str,
        void_reason: str,
    ) -> Cheque:
        """Void an issued cheque: issued -> voided (سند ۱۳).

        The obligation returns to the beneficiary; the leaf is
        marked voided in its checkbook (the checkbook counts it)
        and appears in report 8. The reason is required — a void
        without a reason is not auditable.
        """
        cheque = self._require_cheque(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        assert_transition_scope(
            cheque.kind, cheque.purpose, cheque.state, TRANSITION_VOID
        )
        target_state(cheque.state, TRANSITION_VOID)
        reason = (void_reason or "").strip()
        if not reason:
            raise ChequeValidationError(
                "Voiding an issued cheque needs a reason.",
                field_errors={"void_reason": "This field is required."},
            )
        auto_post = self._gate_auto_post(
            installation_id=installation_id,
            transition=TRANSITION_VOID,
            company=CompanyRef(engine_id=cheque.company_engine_id),
            amount_irr=cheque.amount_irr,
            event_date=self._clock_date(),
        )
        view = self._engine_transition(
            cheque,
            TransitionEngineCheque(
                cheque=self._require_engine_link(cheque),
                company=CompanyRef(engine_id=cheque.company_engine_id),
                transition=TRANSITION_VOID,
                auto_post=auto_post,
                void_reason=reason,
                kind=cheque.kind.value,
            ),
        )
        cheque.void_reason = reason
        saved = self._apply_engine_result(cheque, view, TRANSITION_VOID, actor=actor)
        self._file_review(
            tenant_id=tenant_id,
            cheque=saved,
            subtype="cheque_voided",
            severity=ReviewSeverity.INFO,
            reason=(
                f"Issued cheque {saved.sayad_id} was voided: {reason} "
                "(سند ۱۳; the leaf is voided in its checkbook)."
            ),
            suggested_action=(
                "No action — the obligation returned to the beneficiary "
                "and the leaf is marked voided. Keep the voided leaf on file."
            ),
            actor=actor,
        )
        return saved

    # -- call_in: held -> called_in (سند ۹الف + ۹ب) ----------------------------

    def call_in(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        cheque_id: str,
        actor: str,
        accountant_approval: str,
    ) -> Cheque:
        """Call in a received collateral cheque: held -> called_in.

        The counterparty defaulted and the accountant decided to
        collect: the cheque leaves the memorandum and becomes a
        commercial cheque — TWO simultaneous journals in ONE engine
        transaction (سند ۹الف memorandum reversal + سند ۹ب commercial
        recognition, identical to سند ۱) — and then follows §2.1
        (deposit / endorse / return from ``called_in``). The design
        is the §2.4 proposal and the transition is accountant-gated
        (§9.1 #4, §5 §4): the accountant's per-case approval is
        REQUIRED here, is recorded on the cheque, and is filed to
        the review queue with the gated-design note.
        """
        cheque = self._require_cheque(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        assert_transition_scope(
            cheque.kind, cheque.purpose, cheque.state, TRANSITION_CALL_IN
        )
        target_state(cheque.state, TRANSITION_CALL_IN)
        approval = (accountant_approval or "").strip()
        if not approval:
            raise ChequeValidationError(
                "Calling in a collateral cheque needs the accountant's "
                "approval for this case (§9.1 #4).",
                field_errors={
                    "accountant_approval": "The approving accountant is required."
                },
            )
        auto_post = self._gate_auto_post(
            installation_id=installation_id,
            transition=TRANSITION_CALL_IN,
            company=CompanyRef(engine_id=cheque.company_engine_id),
            amount_irr=cheque.amount_irr,
            event_date=self._clock_date(),
        )
        view = self._engine_transition(
            cheque,
            TransitionEngineCheque(
                cheque=self._require_engine_link(cheque),
                company=CompanyRef(engine_id=cheque.company_engine_id),
                transition=TRANSITION_CALL_IN,
                auto_post=auto_post,
                accountant_approval=approval,
                kind=cheque.kind.value,
            ),
        )
        cheque.call_in_approved_by = approval
        cheque.call_in_approved_at = datetime.datetime.now(datetime.UTC)
        saved = self._apply_engine_result(cheque, view, TRANSITION_CALL_IN, actor=actor)
        self._file_review(
            tenant_id=tenant_id,
            cheque=saved,
            subtype="collateral_called_in",
            severity=ReviewSeverity.WARNING,
            reason=(
                f"Collateral cheque {saved.sayad_id} was called in with "
                f"accountant approval by {approval} (§9.1 #4): two journals "
                "in one transaction (سند ۹الف memorandum reversal + سند ۹ب "
                "commercial recognition). The cheque is commercial now and "
                "follows §2.1."
            ),
            suggested_action=(
                "The §2.4 call-in design is a proposal awaiting the "
                "accountant's confirmation — confirm with the accountant "
                "that سند ۹الف/۹ب as posted is the design they want, then "
                "collect the cheque through §2.1 (deposit / clear)."
            ),
            actor=actor,
        )
        return saved

    # -- unexpected_call_in: issued collateral presented without notice --------

    def unexpected_call_in(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        cheque_id: str,
        actor: str,
        statement_reference: str,
    ) -> Cheque:
        """Record an unexpected call-in of an ISSUED collateral cheque.

        The counterparty took our issued collateral cheque to the
        bank without notice and the money left our account: the
        withdrawal was matched from the bank statement (exact amount
        + leaf number — the statement reference is the evidence and
        is REQUIRED). This is the immediate ALERT path: the
        memorandum is reversed (mirror of سند ۷ in the issued
        direction) and the cheque becomes commercial, but the
        COMMERCIAL journal is accountant-gated (§9.1 #4) and is
        NEVER guessed here — the cheque is left in ``called_in``
        with its journal pending the accountant's review, and a
        CRITICAL alert is filed. Once the accountant posts the
        commercial journal, the cheque clears (سند ۱۱) from
        ``called_in``.
        """
        cheque = self._require_cheque(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        assert_transition_scope(
            cheque.kind, cheque.purpose, cheque.state, TRANSITION_UNEXPECTED_CALL_IN
        )
        target_state(cheque.state, TRANSITION_UNEXPECTED_CALL_IN)
        reference = (statement_reference or "").strip()
        if not reference:
            raise ChequeValidationError(
                "An unexpected call-in needs the bank-statement withdrawal "
                "reference (matched by leaf number).",
                field_errors={
                    "statement_reference": "The matched withdrawal reference is required."
                },
            )
        auto_post = self._gate_auto_post(
            installation_id=installation_id,
            transition=TRANSITION_UNEXPECTED_CALL_IN,
            company=CompanyRef(engine_id=cheque.company_engine_id),
            amount_irr=cheque.amount_irr,
            event_date=self._clock_date(),
        )
        view = self._engine_transition(
            cheque,
            TransitionEngineCheque(
                cheque=self._require_engine_link(cheque),
                company=CompanyRef(engine_id=cheque.company_engine_id),
                transition=TRANSITION_UNEXPECTED_CALL_IN,
                auto_post=auto_post,
                statement_reference=reference,
                kind=cheque.kind.value,
            ),
        )
        cheque.unexpected_call_in = True
        cheque.call_in_statement_reference = reference
        cheque.journal_pending_review = True
        saved = self._apply_engine_result(
            cheque,
            view,
            TRANSITION_UNEXPECTED_CALL_IN,
            actor=actor,
            custody_to=StorageLocation.BANK,
            custody_reason="unexpected call-in — leaf presented at the bank without notice",
        )
        self._file_review(
            tenant_id=tenant_id,
            cheque=saved,
            subtype="issued_collateral_called_in_unexpected",
            severity=ReviewSeverity.CRITICAL,
            reason=(
                f"ALERT: issued collateral cheque {saved.sayad_id} was "
                f"presented at the bank without notice — withdrawal "
                f"matched from the bank statement ({reference}). The "
                "memorandum was reversed; the commercial journal is "
                "accountant-gated (§9.1 #4) and has NOT been posted."
            ),
            suggested_action=(
                "Ask the accountant for the commercial journal of this "
                "called-in issued collateral cheque, post it, then clear "
                "the cheque (سند ۱۱) from called_in. Check the bank "
                "balance — the money has already left the account."
            ),
            actor=actor,
        )
        return saved

    # -- endorsed_bounce: endorsed -> bounced (NO journal, §9.1 #3) -----------

    def record_endorsed_bounce(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        cheque_id: str,
        actor: str,
        bounce_reason: str,
        bounce_date: str,
        certificate_reference: str,
    ) -> Cheque:
        """Record that a cheque we endorsed away has bounced.

        The cheque is no longer our asset; its bounce means we owe
        the next beneficiary — a separate flow with a
        reverse-of-endorsement journal. That journal's design is
        accountant-gated (§9.1 #3): NO journal is guessed here. The
        state moves to ``bounced`` (the evidence — reason + date +
        bank certificate — is recorded for the accountant) and a
        CRITICAL review item is filed for the journal design.
        """
        cheque = self._require_cheque(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        assert_transition_scope(
            cheque.kind, cheque.purpose, cheque.state, TRANSITION_ENDORSED_BOUNCE
        )
        target_state(cheque.state, TRANSITION_ENDORSED_BOUNCE)
        bounce_day = parse_jalali_date(bounce_date)
        if not (bounce_reason or "").strip():
            raise ChequeValidationError(
                "A bounced endorsed cheque needs its bounce reason.",
                field_errors={"bounce_reason": "This field is required."},
            )
        if not (certificate_reference or "").strip():
            raise ChequeValidationError(
                "A bounced endorsed cheque needs the bank certificate reference.",
                field_errors={
                    "certificate_reference": "This field is required."
                },
            )
        view = self._engine_transition(
            cheque,
            TransitionEngineCheque(
                cheque=self._require_engine_link(cheque),
                company=CompanyRef(engine_id=cheque.company_engine_id),
                transition=TRANSITION_ENDORSED_BOUNCE,
                auto_post=False,
                bounce_reason=bounce_reason.strip(),
                bounce_date=bounce_day,
                certificate_reference=certificate_reference.strip(),
                kind=cheque.kind.value,
            ),
        )
        cheque.bounce_reason = bounce_reason.strip()
        cheque.bounce_date = bounce_day
        cheque.bounce_certificate_reference = certificate_reference.strip()
        cheque.journal_pending_review = True
        saved = self._apply_engine_result(
            cheque, view, TRANSITION_ENDORSED_BOUNCE, actor=actor
        )
        self._file_review(
            tenant_id=tenant_id,
            cheque=saved,
            subtype="endorsed_cheque_bounced",
            severity=ReviewSeverity.CRITICAL,
            reason=(
                f"Cheque {saved.sayad_id} was endorsed to "
                f"{saved.endorsee_name or 'an endorsee'} and has bounced "
                f"({bounce_reason.strip()}, {bounce_day.date.isoformat()}). "
                "The cheque is no longer our asset — we owe the next "
                "beneficiary, and the reverse-of-endorsement journal is "
                "accountant-gated (§9.1 #3)."
            ),
            suggested_action=(
                "Ask the accountant for the reverse-of-endorsement journal "
                "design, post it against the bounced endorsed cheque, and "
                "settle with the next beneficiary."
            ),
            actor=actor,
        )
        return saved

    # -- expire / reject: registered -> terminal (no journal) -------------------

    def expire_registered(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        cheque_id: str,
        actor: str = SYSTEM_ACTOR,
    ) -> Cheque:
        """Expire a never-arrived registration: registered -> expired_unreceived.

        No journal. The record NEVER revives — a re-registration with
        the same sayad_id creates a NEW record.
        """
        cheque = self._require_cheque(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        assert_transition_scope(
            cheque.kind, cheque.purpose, cheque.state, TRANSITION_EXPIRE
        )
        target_state(cheque.state, TRANSITION_EXPIRE)
        view = self._engine_transition(
            cheque,
            TransitionEngineCheque(
                cheque=self._require_engine_link(cheque),
                company=CompanyRef(engine_id=cheque.company_engine_id),
                transition=TRANSITION_EXPIRE,
            ),
        )
        return self._apply_engine_result(cheque, view, TRANSITION_EXPIRE, actor=actor)

    def reject_mismatch(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        cheque_id: str,
        actor: str,
        mismatch_detail: str,
    ) -> Cheque:
        """Reject a registered cheque whose leaf disagrees with Sayyad.

        Never confirm on mismatch (§2.1). No journal; the leaf goes
        back to the issuer.
        """
        cheque = self._require_cheque(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        assert_transition_scope(
            cheque.kind, cheque.purpose, cheque.state, TRANSITION_REJECT_MISMATCH
        )
        target_state(cheque.state, TRANSITION_REJECT_MISMATCH)
        view = self._engine_transition(
            cheque,
            TransitionEngineCheque(
                cheque=self._require_engine_link(cheque),
                company=CompanyRef(engine_id=cheque.company_engine_id),
                transition=TRANSITION_REJECT_MISMATCH,
                mismatch_detail=mismatch_detail.strip(),
            ),
        )
        cheque.mismatch_detail = mismatch_detail.strip()
        return self._apply_engine_result(
            cheque, view, TRANSITION_REJECT_MISMATCH, actor=actor
        )

    # -- three-way mismatch matrix (US-533) ----------------------------------

    def verify_leaf_against_sources(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        cheque_id: str,
        actor: str,
        leaf_amount_irr: int,
        leaf_due_date: str,
        sayad_amount_irr: int | None = None,
        sayad_due_date: str | None = None,
    ) -> Cheque:
        """Run the three-way mismatch matrix on a registered cheque.

        Compares the SITE registration (the stored record) against
        the LEAF (what is written on the paper) and SAYYAD (what the
        issuer registered) on amount and due date. NEVER confirm on
        mismatch: a mismatch files a ``three_way_mismatch`` review
        item and rejects the intake (``registered ->
        rejected_mismatch``); the cheque is returned, never held.
        When all supplied readings agree, the cheque is returned
        unchanged — the caller then confirms receipt
        (:meth:`confirm_receipt`) with ``items_matched=True``.
        A Sayyad reading that was not supplied is unknown, not a
        mismatch — the operator is expected to supply it before
        confirming.
        """
        cheque = self._require_cheque(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        target_state(cheque.state, TRANSITION_REJECT_MISMATCH)
        leaf_due = parse_jalali_date(leaf_due_date)
        sayad_due = (
            parse_jalali_date(sayad_due_date) if sayad_due_date is not None else None
        )
        result = evaluate_three_way_mismatch(
            site=SourceReading(
                amount_irr=cheque.amount_irr, due_date=cheque.due_date.date
            ),
            leaf=SourceReading(amount_irr=leaf_amount_irr, due_date=leaf_due.date),
            sayad=SourceReading(
                amount_irr=sayad_amount_irr,
                due_date=sayad_due.date if sayad_due is not None else None,
            ),
        )
        if not result.has_mismatch:
            return cheque
        detail = result.describe()
        self._file_review(
            tenant_id=tenant_id,
            cheque=cheque,
            subtype="three_way_mismatch",
            severity=ReviewSeverity.CRITICAL,
            reason=(
                f"Cheque {cheque.sayad_id} does not agree across its three "
                f"sources. {detail} The leaf was NOT confirmed."
            ),
            suggested_action=(
                "Return the leaf to the issuer and ask for a corrected "
                "cheque (or a corrected Sayyad registration). The intake "
                "is rejected; a corrected leaf registers as a new intake."
            ),
            actor=actor,
        )
        return self.reject_mismatch(
            tenant_id=tenant_id,
            installation_id=installation_id,
            cheque_id=cheque.id,
            actor=actor,
            mismatch_detail=detail,
        )

    def file_ambiguous_matching(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        cheque_id: str,
        actor: str,
        candidate_cheque_ids: tuple[str, ...],
    ) -> Cheque:
        """File an ``ambiguous_matching`` review item (US-533 subtype).

        The clearing matcher (US-534) calls this when several
        same-amount cheques could match one bank deposit: it NEVER
        guesses — the item carries the candidate list for a human.
        This story only defines the filing + subtype; the matcher
        itself lands with US-534.
        """
        cheque = self._require_cheque(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        self._file_review(
            tenant_id=tenant_id,
            cheque=cheque,
            subtype="ambiguous_matching",
            severity=ReviewSeverity.WARNING,
            reason=(
                f"Several same-amount cheques could match one bank deposit "
                f"for cheque {cheque.sayad_id} (candidates: "
                f"{', '.join(candidate_cheque_ids) or 'none listed'}). No "
                "cheque was cleared automatically."
            ),
            suggested_action=(
                "Pick the matching cheque from the candidate list using "
                "the leaf number / deposit date, then clear it manually."
            ),
            actor=actor,
        )
        return cheque

    # -- inquiry receipts + custody (manual entry, §5) -------------------------

    def record_inquiry_receipt(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        cheque_id: str,
        actor: str,
        channel: str,
        inquiry_type: str,
        result_code: str,
        result_summary: str,
        reference: str,
        cost_irr: int = 0,
        requested_at: datetime.datetime | None = None,
        responded_at: datetime.datetime | None = None,
    ) -> InquiryReceipt:
        """Record a manual Sayyad inquiry result as a structured receipt.

        Only verdict/code/time/reference are stored — never the raw
        response, never full national IDs (§3.5).
        """
        cheque = self._require_cheque(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        try:
            channel_value = InquiryChannel(channel)
        except ValueError as exc:
            raise ChequeValidationError(
                f"Unknown inquiry channel {channel!r}.",
                field_errors={"channel": f"Unknown inquiry channel {channel!r}."},
            ) from exc
        try:
            type_value = InquiryType(inquiry_type)
        except ValueError as exc:
            raise ChequeValidationError(
                f"Unknown inquiry type {inquiry_type!r}.",
                field_errors={"inquiry_type": f"Unknown inquiry type {inquiry_type!r}."},
            ) from exc
        receipt = InquiryReceipt(
            cheque_id=cheque.id,
            channel=channel_value,
            inquiry_type=type_value,
            result_code=result_code.strip(),
            result_summary=result_summary.strip(),
            reference=reference.strip(),
            recorded_by=actor,
            cost_irr=cost_irr,
            requested_at=requested_at,
            responded_at=responded_at,
        )
        return self._repository.add_inquiry_receipt(receipt)

    def move_custody(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        cheque_id: str,
        actor: str,
        to_location: StorageLocation,
        reason: str,
    ) -> Cheque:
        """Move the leaf between cashbox and vault (no journal).

        A custody move is a custody event, NEVER a journal. Only a
        ``held`` (or ``called_in`` — a called-in received collateral
        cheque's leaf is still with us) cheque's leaf is with us to
        move (in clearing it is at the bank; returned/endorsed it is
        gone).
        """
        cheque = self._require_cheque(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        if cheque.state not in (ChequeState.HELD, ChequeState.CALLED_IN):
            raise ChequeValidationError(
                "Custody moves are only possible for a held or called-in cheque.",
                field_errors={
                    "state": f"Cannot move custody of a {cheque.state.value} cheque."
                },
            )
        if to_location not in (StorageLocation.CASHBOX, StorageLocation.VAULT):
            raise ChequeValidationError(
                "A manual custody move is only between cashbox and vault.",
                field_errors={
                    "to_location": "Must be 'cashbox' or 'vault' for a manual move."
                },
            )
        previous = cheque.storage_location
        cheque.storage_location = to_location
        cheque.touch()
        saved = self._repository.save(cheque)
        self._repository.add_custody_entry(
            CustodyLogEntry(
                cheque_id=cheque.id,
                from_location=previous,
                to_location=to_location,
                actor=actor,
                reason=reason.strip(),
            )
        )
        return saved

    # -- daily job: reminders + expiry (§2.1) ---------------------------------

    def run_daily_cheque_job(self, today: datetime.date) -> ChequeDailyReport:
        """Run the daily cheque job: day-3 + day-(N-1) reminders, then expiry.

        Idempotent: a reminder is filed once per cheque per milestone
        (tracked on the row); expiry is a one-way state transition.
        A cheque that fails to expire (engine down) is counted as
        skipped, never as expired, and the next run retries it.
        Expiry also files a ``never_arrived_cheque`` review item
        (US-533 subtype) so the never-arrived cheque stays visible
        after its record goes terminal. Afterwards the job scans
        in-clearing cheques for staleness: a cheque past its due
        date + the installation's clearing window (when the window
        is set) is filed once as ``stale_in_clearing`` — it is NEVER
        auto-bounced; only a bank document bounces a cheque.
        """
        reminders = 0
        expired = 0
        skipped = 0
        stale_filed = 0
        settings_by_installation: dict[str, ChequeSettings] = {}
        for cheque in self._repository.find_registered_cheques():
            if cheque.kind != ChequeKind.RECEIVED:
                # An issued cheque sitting in ``registered`` is a
                # manual draft awaiting issue, not site intake
                # awaiting a leaf — the leaf-arrival deadline does
                # not apply to it.
                continue
            settings = settings_by_installation.get(cheque.installation_id)
            if settings is None:
                settings = self._settings_for(cheque.installation_id)
                settings_by_installation[cheque.installation_id] = settings
            age_days = (today - cheque.created_at.date()).days
            deadline = settings.leaf_arrival_deadline_days
            now = datetime.datetime.now(datetime.UTC)
            if age_days >= deadline:
                try:
                    expired_cheque = self.expire_registered(
                        tenant_id=cheque.tenant_id,
                        installation_id=cheque.installation_id,
                        cheque_id=cheque.id,
                    )
                    expired += 1
                    self._file_review(
                        tenant_id=expired_cheque.tenant_id,
                        cheque=expired_cheque,
                        subtype="never_arrived_cheque",
                        severity=ReviewSeverity.WARNING,
                        reason=(
                            f"Cheque {expired_cheque.sayad_id} was registered "
                            f"{age_days} day(s) ago and its leaf never arrived "
                            f"(deadline {deadline} days); the registration "
                            "expired."
                        ),
                        suggested_action=(
                            "The order/invoice stays unpaid. Chase the leaf "
                            "from the customer; a late leaf registers as a "
                            "NEW intake with the same sayad_id."
                        ),
                        actor=SYSTEM_ACTOR,
                    )
                except Exception:  # noqa: BLE001 - one cheque must not stop the job
                    logger.exception("Daily cheque job: expiry failed for %s", cheque.id)
                    skipped += 1
                continue
            milestone: str | None = None
            if age_days == 3 and cheque.reminder_day3_sent_at is None:
                milestone = "day3"
            elif age_days == deadline - 1 and cheque.reminder_final_sent_at is None:
                milestone = "final"
            if milestone is None:
                continue
            self._file_review(
                tenant_id=cheque.tenant_id,
                cheque=cheque,
                subtype=f"never_arrived_reminder_{milestone}",
                severity=ReviewSeverity.INFO,
                reason=(
                    f"Cheque {cheque.sayad_id} was registered "
                    f"{age_days} day(s) ago and its leaf has not arrived "
                    f"(deadline {deadline} days)."
                ),
                suggested_action=(
                    "Chase the leaf from the customer; if it does not arrive "
                    f"by day {deadline} the registration expires automatically."
                ),
                actor=SYSTEM_ACTOR,
            )
            if milestone == "day3":
                cheque.reminder_day3_sent_at = now
            else:
                cheque.reminder_final_sent_at = now
            cheque.touch()
            self._repository.save(cheque)
            reminders += 1
        stale_filed = self._file_stale_in_clearing(today, settings_by_installation)
        return ChequeDailyReport(
            reminders_sent=reminders,
            expired=expired,
            skipped=skipped,
            stale_in_clearing_filed=stale_filed,
        )

    def _file_stale_in_clearing(
        self,
        today: datetime.date,
        settings_by_installation: dict[str, ChequeSettings],
    ) -> int:
        """File ``stale_in_clearing`` items for overdue in-clearing cheques.

        Stale = past due date + the installation's clearing window.
        With no clearing window configured the staleness cannot be
        judged (US-534 defines behaviour while it is unset), so
        those cheques are skipped. Filed once per cheque
        (``stale_in_clearing_filed_at``); a filing failure for one
        cheque never stops the scan.
        """
        filed = 0
        for cheque in self._repository.find_in_clearing_cheques():
            if cheque.stale_in_clearing_filed_at is not None:
                continue
            settings = settings_by_installation.get(cheque.installation_id)
            if settings is None:
                settings = self._settings_for(cheque.installation_id)
                settings_by_installation[cheque.installation_id] = settings
            window = settings.clearing_window_days
            if window is None:
                continue
            stale_after = cheque.due_date.date + datetime.timedelta(days=window)
            if today <= stale_after:
                continue
            try:
                self._file_review(
                    tenant_id=cheque.tenant_id,
                    cheque=cheque,
                    subtype="stale_in_clearing",
                    severity=ReviewSeverity.WARNING,
                    reason=(
                        f"Cheque {cheque.sayad_id} has been in clearing since "
                        f"before its due date "
                        f"{cheque.due_date.date.isoformat()} (clearing window "
                        f"{window} days) with no bank document. It stays "
                        "in clearing — it is never auto-bounced."
                    ),
                    suggested_action=(
                        "Chase the bank for the clearing outcome or the "
                        "non-payment certificate; bounce only with a bank "
                        "document, clear only on a matched deposit."
                    ),
                    actor=SYSTEM_ACTOR,
                )
            except Exception:  # noqa: BLE001 - one cheque must not stop the job
                logger.exception(
                    "Daily cheque job: stale filing failed for %s", cheque.id
                )
                continue
            cheque.stale_in_clearing_filed_at = datetime.datetime.now(datetime.UTC)
            cheque.touch()
            self._repository.save(cheque)
            filed += 1
        return filed


def _sayad_status_after(
    current: SayadStatus, transition: str
) -> SayadStatus:
    """The Sayyad axis after a transition (kept separate from ``state``)."""
    if transition == TRANSITION_CONFIRM_RECEIPT:
        return SayadStatus.CONFIRMED
    if transition == TRANSITION_ENDORSE:
        return SayadStatus.TRANSFERRED
    if transition == TRANSITION_ISSUE:
        # Issuing registers the leaf in Sayyad (Fintech
        # ac-sayad-issue-cheque for legal entities, bank app/branch
        # otherwise) — the leaf is registered from here on.
        return SayadStatus.REGISTERED
    return current


def _invoice_engine_id(invoice_id: str | None) -> int | None:
    """Parse an engine invoice id from the registry's invoice link.

    The registry link is the HesabYar/Tryton invoice id as a string;
    only a plain integer travels to the engine (anything else stays
    HesabYar-side until US-533 wires the mapping).
    """
    if invoice_id is None:
        return None
    try:
        return int(invoice_id)
    except ValueError:
        return None


def _coerce_receipt_fields(
    receipt: dict[str, object],
) -> dict[str, object]:
    """Validate the shape of an inline inquiry receipt (confirm-receipt)."""
    allowed = {
        "channel",
        "inquiry_type",
        "result_code",
        "result_summary",
        "reference",
        "cost_irr",
        "requested_at",
        "responded_at",
    }
    unknown = sorted(set(receipt) - allowed)
    if unknown:
        raise ChequeValidationError(
            "Unknown inquiry receipt field(s).",
            field_errors={name: "Unknown inquiry receipt field." for name in unknown},
        )
    missing = sorted({"channel", "inquiry_type", "result_code", "result_summary", "reference"} - set(receipt))
    if missing:
        raise ChequeValidationError(
            "Inquiry receipt is missing required field(s).",
            field_errors={name: "This field is required." for name in missing},
        )
    return dict(receipt)
