"""Recurring templates + the daily generator (US-563, Phase 5-7 ۵خ W3).

Two services:

- :class:`RecurringTemplateService` — template CRUD, preview,
  pause/resume. The template is a standing instruction; its
  payload is validated against the same accounting invariants as
  the live draft flows (US-202/US-204), so a template that could
  never produce a valid draft is refused at creation, not at its
  first firing.
- :class:`RecurringGenerationService` — the scheduled job's work
  (``scheduler.recurring_generation``, daily): for every active
  template, generate each due occurrence as a DRAFT in the engine
  and record the occurrence row that links the document to its
  template + occurrence number.

The generator's contract:

- **Draft by default.** Auto-post needs BOTH the template's
  explicit opt-in AND the three-layer policy gate (F3) evaluated
  for event type ``recurring.generate`` — a denied gate leaves a
  draft, never an error.
- **Idempotent.** One occurrence per template per scheduled date:
  the (template, scheduled date) unique constraint is the
  backstop, and an already-generated occurrence is skipped, never
  re-created. A failed occurrence is retried on the next run; a
  draft whose post failed is NOT retried (the draft exists — an
  accountant posts it by hand).
- **Catch-up, not skip.** The generator looks at every occurrence
  with ``scheduled_date <= today`` — a Friday due date (the daily
  job skips Friday) generates on Saturday, and a template resumed
  after a pause generates its missed occurrences. The occurrence
  keeps its scheduled date either way.
- **Edits never rewrite history.** Each occurrence stores the
  payload snapshot it was generated from; a template edit only
  changes future occurrences. The schedule shape is immutable
  once the template has an occurrence, so an occurrence number
  never changes meaning.
- **One template never blocks the rest.** A failure is recorded
  on the occurrence + filed as a review item (deduped per
  template per date), and the run continues.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from hesabyar.application.commands.integrations import PolicyGateService
from hesabyar.application.commands.invoices import InvoiceDraftService
from hesabyar.application.commands.journals import JournalDraftService
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.commands.scheduler import file_job_failure_item
from hesabyar.application.errors import (
    RecurringTemplateInUseError,
    RecurringTemplateNotFoundError,
)
from hesabyar.domain.accounting.refs import (
    AccountRef,
    CompanyRef,
    InvoiceRef,
    JournalDraftRef,
    JournalRef,
    PartyAddressRef,
    PartyRef,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import HesabYarError
from hesabyar.domain.recurring.errors import (
    InvalidRecurringTemplateError,
    RecurringOccurrenceConflictError,
)
from hesabyar.domain.recurring.models import (
    RECURRING_GATE_EVENT_TYPE,
    InvoiceTemplatePayload,
    JournalTemplatePayload,
    OccurrencePreview,
    OccurrenceStatus,
    RecurringOccurrence,
    RecurringTemplate,
    TemplateKind,
    TemplatePayload,
    TemplateStatus,
    occurrence_note,
    snapshot_for_template,
)
from hesabyar.domain.recurring.ports import RecurringRepositoryPort
from hesabyar.domain.recurring.schedules import (
    OccurrenceDate,
    RecurringSchedule,
    occurrence_dates,
)
from hesabyar.domain.review_queue.ports import ReviewQueuePort
from hesabyar.domain.scheduler.models import RECURRING_GENERATION_JOB_ID

#: Actor recorded on review items filed by the recurring generator.
RECURRING_ACTOR = "scheduler"

#: Max preview occurrences in one call (the preview is a pure
#: computation, but an unbounded request is still a bug).
MAX_PREVIEW_COUNT = 120


@dataclass(frozen=True)
class RecurringGenerationFailure:
    """One template/date that failed inside a generator run."""

    template_id: str
    tenant_id: str
    scheduled_date: str
    error: str
    review_item_id: str | None = None

    def summary(self) -> dict[str, object]:
        """JSON-serializable form for the run record."""
        return {
            "template_id": self.template_id,
            "tenant_id": self.tenant_id,
            "scheduled_date": self.scheduled_date,
            "error": self.error,
            "review_item_id": self.review_item_id,
        }


@dataclass(frozen=True)
class RecurringGenerationReport:
    """Outcome of one recurring-generation run across all templates."""

    as_of: datetime.date
    templates_scanned: int
    occurrences_due: int
    generated: int
    posted: int
    skipped_existing: int
    failed: int
    completed_templates: int
    failures: tuple[RecurringGenerationFailure, ...] = ()

    def summary(self) -> dict[str, object]:
        """JSON-serializable form for the run record."""
        return {
            "as_of": self.as_of.isoformat(),
            "templates_scanned": self.templates_scanned,
            "occurrences_due": self.occurrences_due,
            "generated": self.generated,
            "posted": self.posted,
            "skipped_existing": self.skipped_existing,
            "failed": self.failed,
            "completed_templates": self.completed_templates,
            "failures": [failure.summary() for failure in self.failures],
        }


class RecurringTemplateService:
    """Template CRUD, preview, pause/resume (no engine calls)."""

    def __init__(
        self,
        repository: RecurringRepositoryPort,
        *,
        now: datetime.datetime | None = None,
    ) -> None:
        self._repository = repository
        self._now = now

    def _clock(self) -> datetime.datetime:
        if self._now is not None:
            return self._now
        return datetime.datetime.now(datetime.UTC)

    # -- create / read ----------------------------------------------------

    def create_template(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        company_id: str,
        company_engine_id: int,
        kind: TemplateKind,
        name: str,
        schedule: RecurringSchedule,
        payload: TemplatePayload,
        auto_post: bool = False,
    ) -> RecurringTemplate:
        """Create a template (payload + schedule validated at construction).

        The company is resolved by the caller (the REST layer maps
        the API company id to its engine id); this service trusts
        the pair it is given. The template starts ACTIVE — its first
        occurrence is generated by the next daily run (or a manual
        trigger of ``scheduler.recurring_generation``).
        """
        now = self._clock()
        template = RecurringTemplate(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            installation_id=installation_id,
            company_id=company_id,
            company_engine_id=company_engine_id,
            kind=kind,
            name=name,
            status=TemplateStatus.ACTIVE,
            schedule=schedule,
            payload=payload,
            auto_post=auto_post,
            total_amount_irr=payload.total_amount_irr,
            created_at=now,
            updated_at=now,
        )
        return self._repository.save_template(template)

    def get_template(self, tenant_id: str, template_id: str) -> RecurringTemplate:
        """Return one template of this tenant, or 404."""
        template = self._repository.get_template(tenant_id, template_id)
        if template is None:
            raise RecurringTemplateNotFoundError(
                f"Recurring template {template_id!r} does not exist.",
                details={"template_id": template_id},
            )
        return template

    def list_templates(
        self,
        tenant_id: str,
        *,
        company_id: str | None = None,
        kind: TemplateKind | None = None,
        status: TemplateStatus | None = None,
    ) -> Sequence[RecurringTemplate]:
        """A tenant's templates, newest first, optionally filtered."""
        return self._repository.list_templates(
            tenant_id, company_id=company_id, kind=kind, status=status
        )

    # -- update / pause / resume / delete ----------------------------------

    def update_template(
        self,
        tenant_id: str,
        template_id: str,
        *,
        name: str | None = None,
        schedule: RecurringSchedule | None = None,
        payload: TemplatePayload | None = None,
        auto_post: bool | None = None,
    ) -> RecurringTemplate:
        """Update a template; only the given fields change.

        - A COMPLETED template cannot be edited (its end was
          reached; create a new template instead).
        - The schedule SHAPE (kind / Jalali day / month / interval /
          start date) cannot change once the template has an
          occurrence — it would renumber history. End conditions
          (end date / max occurrences) stay editable: they only
          truncate or extend the future.
        - The payload must match the template's kind; a payload edit
          only affects FUTURE occurrences (each occurrence keeps the
          snapshot it was generated from).
        """
        template = self.get_template(tenant_id, template_id)
        if template.status is TemplateStatus.COMPLETED:
            raise InvalidRecurringTemplateError(
                f"Template {template_id!r} is completed and cannot be edited.",
                details={"template_id": template_id, "status": template.status.value},
            )
        if payload is not None:
            expected = (
                InvoiceTemplatePayload
                if template.kind is TemplateKind.INVOICE
                else JournalTemplatePayload
            )
            if not isinstance(payload, expected):
                raise InvalidRecurringTemplateError(
                    f"A {template.kind.value} template needs a "
                    f"{expected.__name__}, got {type(payload).__name__}.",
                    details={"template_id": template_id},
                )
        new_schedule = schedule if schedule is not None else template.schedule
        if schedule is not None and schedule.shape() != template.schedule.shape():
            if self._repository.count_occurrences(template_id) > 0:
                raise InvalidRecurringTemplateError(
                    f"Template {template_id!r} already has occurrences: its "
                    "schedule shape (kind / Jalali day / month / interval / "
                    "start date) cannot change. Only the end date and "
                    "occurrence count stay editable.",
                    details={"template_id": template_id},
                )
        new_payload = payload if payload is not None else template.payload
        updated = dataclasses.replace(
            template,
            name=name if name is not None else template.name,
            schedule=new_schedule,
            payload=new_payload,
            auto_post=auto_post if auto_post is not None else template.auto_post,
            total_amount_irr=new_payload.total_amount_irr,
            updated_at=self._clock(),
        )
        return self._repository.save_template(updated)

    def pause_template(self, tenant_id: str, template_id: str) -> RecurringTemplate:
        """Pause a template (idempotent; a completed template cannot pause).

        While paused, the generator skips the template entirely —
        its due occurrences wait, and generate (catch-up) when it
        resumes. Pausing never deletes or rewrites anything.
        """
        template = self.get_template(tenant_id, template_id)
        if template.status is TemplateStatus.COMPLETED:
            raise InvalidRecurringTemplateError(
                f"Template {template_id!r} is completed and cannot be paused.",
                details={"template_id": template_id, "status": template.status.value},
            )
        if template.status is TemplateStatus.PAUSED:
            return template
        paused = dataclasses.replace(
            template, status=TemplateStatus.PAUSED, updated_at=self._clock()
        )
        return self._repository.save_template(paused)

    def resume_template(self, tenant_id: str, template_id: str) -> RecurringTemplate:
        """Resume a paused template (idempotent; completed is terminal)."""
        template = self.get_template(tenant_id, template_id)
        if template.status is TemplateStatus.COMPLETED:
            raise InvalidRecurringTemplateError(
                f"Template {template_id!r} is completed and cannot be resumed.",
                details={"template_id": template_id, "status": template.status.value},
            )
        if template.status is TemplateStatus.ACTIVE:
            return template
        resumed = dataclasses.replace(
            template, status=TemplateStatus.ACTIVE, updated_at=self._clock()
        )
        return self._repository.save_template(resumed)

    def delete_template(self, tenant_id: str, template_id: str) -> None:
        """Delete a template that never fired.

        A template with any occurrence (even a failed one) cannot be
        deleted — its generated documents keep their link to it.
        Pause it instead.
        """
        template = self.get_template(tenant_id, template_id)
        if self._repository.count_occurrences(template.id) > 0:
            raise RecurringTemplateInUseError(
                f"Template {template_id!r} already has occurrences and cannot "
                "be deleted; pause it instead.",
                details={"template_id": template_id},
            )
        self._repository.delete_template(tenant_id, template_id)

    # -- preview / occurrences ----------------------------------------------

    def preview(
        self,
        *,
        schedule: RecurringSchedule,
        total_amount_irr: int,
        count: int = 12,
        after: datetime.date | None = None,
    ) -> list[OccurrencePreview]:
        """Preview the next ``count`` occurrences of a schedule + amount.

        Pure computation — nothing is saved. Used both for the
        pre-save preview (an unsaved template body) and for an
        existing template's upcoming occurrences.
        """
        if count < 1 or count > MAX_PREVIEW_COUNT:
            raise InvalidRecurringTemplateError(
                f"Preview count must be in 1..{MAX_PREVIEW_COUNT}, got {count}.",
                details={"count": count},
            )
        candidates = occurrence_dates(schedule, count=count, after=after)
        return [
            OccurrencePreview.from_date(candidate, total_amount_irr)
            for candidate in candidates
        ]

    def preview_template(
        self,
        tenant_id: str,
        template_id: str,
        *,
        count: int = 12,
        after: datetime.date | None = None,
    ) -> list[OccurrencePreview]:
        """Preview an existing template's next ``count`` occurrences."""
        template = self.get_template(tenant_id, template_id)
        return self.preview(
            schedule=template.schedule,
            total_amount_irr=template.total_amount_irr,
            count=count,
            after=after,
        )

    def list_occurrences(
        self, tenant_id: str, template_id: str
    ) -> Sequence[RecurringOccurrence]:
        """A template's generated occurrences, in occurrence-number order."""
        template = self.get_template(tenant_id, template_id)
        return self._repository.list_occurrences(template.id)


class RecurringGenerationService:
    """Generate due occurrences as drafts (the daily job's work)."""

    def __init__(
        self,
        repository: RecurringRepositoryPort,
        *,
        invoices: InvoiceDraftService,
        journals: JournalDraftService,
        gate: PolicyGateService | None = None,
        review: ReviewQueueCommandService | None = None,
        queue: ReviewQueuePort | None = None,
        now: datetime.datetime | None = None,
    ) -> None:
        self._repository = repository
        self._invoices = invoices
        self._journals = journals
        self._gate = gate
        self._review = review
        self._queue = queue
        self._now = now

    def _clock(self) -> datetime.datetime:
        if self._now is not None:
            return self._now
        return datetime.datetime.now(datetime.UTC)

    def _file_failure(
        self,
        *,
        template: RecurringTemplate,
        scheduled_date: datetime.date,
        error: str,
        today: datetime.date,
    ) -> str | None:
        """File one dead-letter item for a failed occurrence (deduped per date)."""
        if self._review is None or self._queue is None:
            return None
        return file_job_failure_item(
            review=self._review,
            queue=self._queue,
            tenant_id=template.tenant_id,
            job_id=RECURRING_GENERATION_JOB_ID,
            ref=(
                f"scheduled-job:{RECURRING_GENERATION_JOB_ID}:"
                f"{template.id}:{scheduled_date.isoformat()}"
            ),
            reason=(
                f"Recurring template {template.name!r} failed to generate "
                f"its {scheduled_date.isoformat()} occurrence: {error}"
            ),
            actor=RECURRING_ACTOR,
        )

    # -- the run ---------------------------------------------------------------

    def generate_due(
        self,
        *,
        today: datetime.date | None = None,
        actor: str = RECURRING_ACTOR,
    ) -> RecurringGenerationReport:
        """Generate every due occurrence of every active template.

        One template's failure never blocks the others. A template
        whose end condition is reached (and whose due occurrences
        are all in a terminal state) is marked COMPLETED.
        """
        day = today or self._clock().date()
        now = self._clock()
        templates_scanned = 0
        occurrences_due = 0
        generated = 0
        posted = 0
        skipped_existing = 0
        failed = 0
        completed_templates = 0
        failures: list[RecurringGenerationFailure] = []
        for template in self._repository.list_active_templates():
            templates_scanned += 1
            try:
                due = occurrence_dates(template.schedule, until=day)
            except InvalidRecurringTemplateError as exc:
                # A stored schedule that no longer enumerates (corrupt
                # data, never a user error at this point): record it
                # and move on — the template stays active for repair.
                error = str(exc) or exc.__class__.__name__
                item_id = self._file_failure(
                    template=template, scheduled_date=day, error=error, today=day
                )
                failures.append(
                    RecurringGenerationFailure(
                        template_id=template.id,
                        tenant_id=template.tenant_id,
                        scheduled_date=day.isoformat(),
                        error=error,
                        review_item_id=item_id,
                    )
                )
                failed += 1
                continue
            occurrences_due += len(due)
            for candidate in due:
                outcome, failure = self._generate_one_due(
                    template, candidate, day=day, now=now
                )
                if outcome == "generated":
                    generated += 1
                elif outcome == "posted":
                    generated += 1
                    posted += 1
                elif outcome == "skipped":
                    skipped_existing += 1
                else:  # "failed" — recorded on the occurrence + review queue
                    failed += 1
                    assert failure is not None
                    failures.append(failure)
            if self._maybe_complete(template, day=day, now=now):
                completed_templates += 1
        return RecurringGenerationReport(
            as_of=day,
            templates_scanned=templates_scanned,
            occurrences_due=occurrences_due,
            generated=generated,
            posted=posted,
            skipped_existing=skipped_existing,
            failed=failed,
            completed_templates=completed_templates,
            failures=tuple(failures),
        )

    def _generate_one_due(
        self,
        template: RecurringTemplate,
        candidate: OccurrenceDate,
        *,
        day: datetime.date,
        now: datetime.datetime,
    ) -> tuple[str, RecurringGenerationFailure | None]:
        """Generate one due occurrence.

        Returns ``("generated" | "posted" | "skipped", None)``, or
        ``("failed", failure)`` — the failure is already recorded on
        the occurrence row + filed to the review queue.
        """
        existing = self._repository.get_occurrence_by_date(
            template.id, candidate.scheduled_date
        )
        if existing is not None and existing.status in (
            OccurrenceStatus.DRAFT,
            OccurrenceStatus.POSTED,
        ):
            return "skipped", None
        if existing is not None:
            # A failed (or interrupted pending) occurrence is retried
            # under its own row and number — never a second row. Its
            # snapshot refreshes from the CURRENT template: nothing
            # was generated yet, so fixing the template is exactly
            # how a failed occurrence heals.
            occurrence = dataclasses.replace(
                existing,
                status=OccurrenceStatus.PENDING,
                total_amount_irr=template.total_amount_irr,
                snapshot=snapshot_for_template(template),
                error=None,
                gate_decision=None,
                updated_at=now,
            )
        else:
            occurrence = RecurringOccurrence.new_pending(
                id=str(uuid.uuid4()),
                template=template,
                candidate=candidate,
                now=now,
            )
        try:
            occurrence = self._repository.save_occurrence(occurrence)
        except RecurringOccurrenceConflictError:
            # Another run claimed this date first: it is generated,
            # not failed — the idempotency backstop did its job.
            return "skipped", None
        try:
            engine_id, was_posted, gate_decision, post_error = self._create_document(
                template, occurrence, now=now
            )
        except HesabYarError as exc:
            error = str(exc) or exc.__class__.__name__
            self._repository.save_occurrence(
                dataclasses.replace(
                    occurrence,
                    status=OccurrenceStatus.FAILED,
                    error=error,
                    updated_at=now,
                )
            )
            item_id = self._file_failure(
                template=template,
                scheduled_date=candidate.scheduled_date,
                error=error,
                today=day,
            )
            return "failed", RecurringGenerationFailure(
                template_id=template.id,
                tenant_id=template.tenant_id,
                scheduled_date=candidate.scheduled_date.isoformat(),
                error=error,
                review_item_id=item_id,
            )
        status = OccurrenceStatus.POSTED if was_posted else OccurrenceStatus.DRAFT
        self._repository.save_occurrence(
            dataclasses.replace(
                occurrence,
                status=status,
                engine_document_id=engine_id,
                gate_decision=gate_decision,
                error=post_error,
                generated_at=now,
                updated_at=now,
            )
        )
        return ("posted", None) if was_posted else ("generated", None)

    def _create_document(
        self,
        template: RecurringTemplate,
        occurrence: RecurringOccurrence,
        *,
        now: datetime.datetime,
    ) -> tuple[int, bool, dict[str, Any] | None, str | None]:
        """Create one occurrence's draft (and maybe post it).

        Returns ``(engine_document_id, was_posted, gate_decision,
        post_error)``. Draft-creation failures raise (the caller
        records them as ``failed``); a post failure does NOT raise —
        the draft exists, so the occurrence is a draft with the
        failure noted, never retried into a duplicate.
        """
        payload = occurrence.snapshot_payload()
        company = CompanyRef(template.company_engine_id)
        date = FiscalDate(occurrence.scheduled_date)
        note = occurrence_note(template, occurrence.occurrence_number)
        if isinstance(payload, InvoiceTemplatePayload):
            description = f"{payload.description} {note}".strip()
            ref = self._invoices.create_draft(
                company,
                payload.invoice_type,
                PartyRef(payload.party_engine_id),
                date,
                [line.to_line() for line in payload.lines],
                description=description,
                reference=payload.reference,
                journal=(
                    JournalRef(payload.journal_engine_id)
                    if payload.journal_engine_id is not None
                    else None
                ),
                account=(
                    AccountRef(payload.account_engine_id)
                    if payload.account_engine_id is not None
                    else None
                ),
                invoice_address=(
                    PartyAddressRef(payload.invoice_address_engine_id)
                    if payload.invoice_address_engine_id is not None
                    else None
                ),
            )
            engine_id = ref.engine_id
        else:
            assert isinstance(payload, JournalTemplatePayload)
            description = f"{payload.description} {note}".strip()
            ref = self._journals.create_draft(
                company,
                date,
                [line.to_line() for line in payload.lines],
                description=description,
                journal=(
                    JournalRef(payload.journal_engine_id)
                    if payload.journal_engine_id is not None
                    else None
                ),
            )
            engine_id = ref.engine_id
        if not template.auto_post:
            return engine_id, False, None, None
        gate_decision = self._evaluate_gate(template, occurrence)
        if not gate_decision["allowed"]:
            # Draft by default: a denied gate is not a failure.
            return engine_id, False, gate_decision, None
        try:
            if template.kind is TemplateKind.INVOICE:
                self._invoices.finalize_invoice(InvoiceRef(engine_id))
                self._invoices.post_invoice(InvoiceRef(engine_id))
            else:
                self._journals.post_draft(JournalDraftRef(engine_id))
        except HesabYarError as exc:
            # The draft landed; only its posting failed. Record the
            # failure on the occurrence and let an accountant post it
            # by hand — retrying would create a second draft.
            return engine_id, False, gate_decision, str(exc) or exc.__class__.__name__
        return engine_id, True, gate_decision, None

    def _evaluate_gate(
        self,
        template: RecurringTemplate,
        occurrence: RecurringOccurrence,
    ) -> dict[str, Any]:
        """Evaluate the policy gate for one auto-post attempt.

        Fail-closed at this layer too: with no gate wired, auto-post
        is denied (the draft stays a draft) instead of erroring.
        """
        if self._gate is None:
            return {
                "allowed": False,
                "action": "draft",
                "reason": (
                    "No policy gate is wired in this process; auto-post "
                    "is unavailable, so the draft stays a draft."
                ),
                "layers": [
                    {
                        "layer": "gate_wiring",
                        "passed": False,
                        "detail": "No policy gate is wired in this process.",
                    }
                ],
            }
        decision = self._gate.evaluate(
            template.installation_id,
            event_type=RECURRING_GATE_EVENT_TYPE,
            company=CompanyRef(template.company_engine_id),
            event_date=occurrence.scheduled_date,
            amount_irr=occurrence.total_amount_irr,
        )
        return {
            "allowed": decision.allowed,
            "action": decision.action,
            "reason": decision.reason,
            "policy_config_id": decision.policy_config_id,
            "layers": [
                {"layer": layer.layer, "passed": layer.passed, "detail": layer.detail}
                for layer in decision.layers
            ],
        }

    def _maybe_complete(
        self,
        template: RecurringTemplate,
        *,
        day: datetime.date,
        now: datetime.datetime,
    ) -> bool:
        """Mark a template COMPLETED when its end was reached.

        Completed when no occurrence falls after today AND no due
        occurrence is still open (pending/failed) — a failed last
        occurrence keeps the template active so the next run retries
        it instead of silently dropping it.
        """
        future = occurrence_dates(
            template.schedule, count=1, after=day + datetime.timedelta(days=1)
        )
        if future:
            return False
        due = occurrence_dates(template.schedule, until=day)
        for candidate in due:
            existing = self._repository.get_occurrence_by_date(
                template.id, candidate.scheduled_date
            )
            if existing is None or existing.status in (
                OccurrenceStatus.PENDING,
                OccurrenceStatus.FAILED,
            ):
                return False
        completed = dataclasses.replace(
            template, status=TemplateStatus.COMPLETED, updated_at=now
        )
        self._repository.save_template(completed)
        return True
