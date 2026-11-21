"""Unit tests for the recurring template + generation services (US-563).

In-memory fakes behind the ports — no database, no engine. Pins the
contract: draft-by-default, opt-in auto-post through the policy
gate, idempotent generation (one occurrence per template per
scheduled date), snapshot immutability (editing a template never
rewrites an already-generated draft), and completion.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from decimal import Decimal

import pytest

from hesabyar.application.commands.recurring import (
    RecurringGenerationService,
    RecurringTemplateService,
)
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.errors import (
    RecurringTemplateInUseError,
    RecurringTemplateNotFoundError,
)
from hesabyar.domain.accounting.refs import InvoiceRef, JournalDraftRef
from hesabyar.domain.common.errors import HesabYarError, UnbalancedJournalError
from hesabyar.domain.integrations.policy_gate import GateLayerResult, PolicyGateDecision
from hesabyar.domain.recurring.errors import (
    InvalidRecurringTemplateError,
    RecurringOccurrenceConflictError,
)
from hesabyar.domain.recurring.models import (
    InvoiceTemplateLine,
    InvoiceTemplatePayload,
    JournalTemplateLine,
    JournalTemplatePayload,
    OccurrenceStatus,
    RecurringOccurrence,
    RecurringTemplate,
    TemplateKind,
    TemplatePayload,
    TemplateStatus,
)
from hesabyar.domain.recurring.ports import RecurringRepositoryPort
from hesabyar.domain.recurring.schedules import (
    RecurringSchedule,
    RecurringScheduleKind,
)
from tests.unit.test_scheduler_service import FakeAudit, FakeReviewQueue

NOW = datetime.datetime(2026, 11, 21, 10, 0, tzinfo=datetime.UTC)  # Saturday
TODAY = datetime.date(2026, 11, 21)  # 1405/08/30
TENANT_ID = "tenant-recurring-tests"
INSTALLATION_ID = "00000000-0000-0000-0000-0000000000aa"
COMPANY_ID = "company-1"
COMPANY_ENGINE_ID = 1
OTHER_TENANT = "tenant-other"


def _invoice_payload(unit_price: int = 500_000) -> InvoiceTemplatePayload:
    return InvoiceTemplatePayload(
        invoice_type="out",
        party_engine_id=7,
        lines=(
            InvoiceTemplateLine(
                account_engine_id=11,
                unit_price_irr=unit_price,
                quantity=Decimal(1),
                description="اشتراک ماهانه",
            ),
        ),
        description="صورت‌حساب ماهانه",
    )


def _journal_payload() -> JournalTemplatePayload:
    return JournalTemplatePayload(
        description="استهلاک ماهانه",
        lines=(
            JournalTemplateLine(account_engine_id=21, debit_irr=1_000_000),
            JournalTemplateLine(account_engine_id=22, credit_irr=1_000_000),
        ),
    )


def _monthly_day_15(
    *,
    start: datetime.date = datetime.date(2026, 10, 23),  # 1405/08/01
    end: datetime.date | None = None,
    max_occurrences: int | None = None,
) -> RecurringSchedule:
    return RecurringSchedule(
        kind=RecurringScheduleKind.MONTHLY,
        jalali_day=15,
        start_date=start,
        end_date=end,
        max_occurrences=max_occurrences,
    )


class FakeRecurringRepository(RecurringRepositoryPort):
    """In-memory recurring store, enforcing the unique constraints."""

    def __init__(self) -> None:
        self.templates: dict[str, RecurringTemplate] = {}
        self.occurrences: dict[str, RecurringOccurrence] = {}

    def save_template(self, template: RecurringTemplate) -> RecurringTemplate:
        self.templates[template.id] = template
        return template

    def get_template(
        self, tenant_id: str, template_id: str
    ) -> RecurringTemplate | None:
        template = self.templates.get(template_id)
        if template is None or template.tenant_id != tenant_id:
            return None
        return template

    def list_templates(
        self,
        tenant_id: str,
        *,
        company_id: str | None = None,
        kind: TemplateKind | None = None,
        status: TemplateStatus | None = None,
    ) -> Sequence[RecurringTemplate]:
        return [
            t
            for t in self.templates.values()
            if t.tenant_id == tenant_id
            and (company_id is None or t.company_id == company_id)
            and (kind is None or t.kind == kind)
            and (status is None or t.status == status)
        ]

    def list_active_templates(self) -> Sequence[RecurringTemplate]:
        return [
            t
            for t in self.templates.values()
            if t.status == TemplateStatus.ACTIVE
        ]

    def delete_template(self, tenant_id: str, template_id: str) -> bool:
        template = self.templates.get(template_id)
        if template is None or template.tenant_id != tenant_id:
            return False
        del self.templates[template_id]
        return True

    def count_occurrences(self, template_id: str) -> int:
        return sum(
            1 for o in self.occurrences.values() if o.template_id == template_id
        )

    def save_occurrence(self, occurrence: RecurringOccurrence) -> RecurringOccurrence:
        for other in self.occurrences.values():
            if other.template_id != occurrence.template_id:
                continue
            if other.id == occurrence.id:
                continue
            if other.occurrence_number == occurrence.occurrence_number:
                raise RecurringOccurrenceConflictError(
                    f"Occurrence number {occurrence.occurrence_number} "
                    f"already exists for template {occurrence.template_id}.",
                    details={"template_id": occurrence.template_id},
                )
            if other.scheduled_date == occurrence.scheduled_date:
                raise RecurringOccurrenceConflictError(
                    f"An occurrence for {occurrence.scheduled_date} "
                    f"already exists for template {occurrence.template_id}.",
                    details={"template_id": occurrence.template_id},
                )
        self.occurrences[occurrence.id] = occurrence
        return occurrence

    def get_occurrence_by_date(
        self, template_id: str, scheduled_date: datetime.date
    ) -> RecurringOccurrence | None:
        return next(
            (
                o
                for o in self.occurrences.values()
                if o.template_id == template_id
                and o.scheduled_date == scheduled_date
            ),
            None,
        )

    def list_occurrences(self, template_id: str) -> Sequence[RecurringOccurrence]:
        return sorted(
            (o for o in self.occurrences.values() if o.template_id == template_id),
            key=lambda o: o.occurrence_number,
        )


class FakeInvoices:
    """Invoice draft stub: records drafts, assigns engine ids, can fail."""

    def __init__(self) -> None:
        self.drafts: list[dict] = []
        self.finalized: list[InvoiceRef] = []
        self.posted: list[InvoiceRef] = []
        self.fail_create: str | None = None
        self.fail_post: str | None = None
        self._next = 1

    def create_draft(  # type: ignore[no-untyped-def]
        self, company, invoice_type, party, date, lines, description="",
        reference="", journal=None, account=None, invoice_address=None,
    ):
        if self.fail_create is not None:
            raise HesabYarError(self.fail_create, code="ENGINE_UNAVAILABLE")
        ref = InvoiceRef(self._next)
        self._next += 1
        self.drafts.append(
            {
                "ref": ref,
                "company": company,
                "invoice_type": invoice_type,
                "party": party,
                "date": date,
                "lines": list(lines),
                "description": description,
                "reference": reference,
            }
        )
        return ref

    def finalize_invoice(self, invoice):  # type: ignore[no-untyped-def]
        self.finalized.append(invoice)
        return invoice

    def post_invoice(self, invoice):  # type: ignore[no-untyped-def]
        if self.fail_post is not None:
            raise HesabYarError(self.fail_post, code="ENGINE_POST_FAILED")
        self.posted.append(invoice)
        return invoice


class FakeJournals:
    """Journal draft stub: records drafts, assigns engine ids, can fail."""

    def __init__(self) -> None:
        self.drafts: list[dict] = []
        self.posted: list[JournalDraftRef] = []
        self.fail_create: str | None = None
        self.fail_post: str | None = None
        self._next = 1

    def create_draft(self, company, date, lines, description="", journal=None):  # type: ignore[no-untyped-def]
        if self.fail_create is not None:
            raise HesabYarError(self.fail_create, code="ENGINE_UNAVAILABLE")
        ref = JournalDraftRef(self._next)
        self._next += 1
        self.drafts.append(
            {
                "ref": ref,
                "company": company,
                "date": date,
                "lines": list(lines),
                "description": description,
            }
        )
        return ref

    def post_draft(self, draft):  # type: ignore[no-untyped-def]
        if self.fail_post is not None:
            raise HesabYarError(self.fail_post, code="ENGINE_POST_FAILED")
        self.posted.append(draft)
        return draft


def _allowed_gate() -> PolicyGateDecision:
    return PolicyGateDecision(
        allowed=True,
        action="auto_post",
        layers=(
            GateLayerResult(layer="policy", passed=True, detail="policy allows"),
        ),
        policy_config_id="cfg-1",
        contract_id=None,
        contract_version=None,
        reason="policy allows recurring auto-post",
        evaluated_at=NOW,
    )


def _denied_gate() -> PolicyGateDecision:
    return PolicyGateDecision(
        allowed=False,
        action="draft",
        layers=(
            GateLayerResult(
                layer="policy", passed=False, detail="auto-post disabled"
            ),
        ),
        policy_config_id="cfg-1",
        contract_id=None,
        contract_version=None,
        reason="auto-post disabled for this installation",
        evaluated_at=NOW,
    )


class FakeGate:
    """Policy gate stub: allowed/denied, records evaluations."""

    def __init__(self, decision: PolicyGateDecision) -> None:
        self.decision = decision
        self.calls: list[dict] = []

    def evaluate(self, installation_id, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append({"installation_id": installation_id, **kwargs})
        return self.decision


def _template_service(
    repo: FakeRecurringRepository | None = None,
) -> tuple[RecurringTemplateService, FakeRecurringRepository]:
    repo = repo or FakeRecurringRepository()
    return RecurringTemplateService(repo, now=NOW), repo


def _generation_service(
    repo: FakeRecurringRepository,
    invoices: FakeInvoices | None = None,
    journals: FakeJournals | None = None,
    gate: FakeGate | None = None,
    review: ReviewQueueCommandService | None = None,
    queue: FakeReviewQueue | None = None,
) -> RecurringGenerationService:
    return RecurringGenerationService(
        repo,
        invoices=invoices or FakeInvoices(),  # type: ignore[arg-type]
        journals=journals or FakeJournals(),  # type: ignore[arg-type]
        gate=gate,  # type: ignore[arg-type]
        review=review,
        queue=queue,
        now=NOW,
    )


def _make_template(
    service: RecurringTemplateService,
    *,
    name: str = "اشتراک ماهانه",
    kind: TemplateKind = TemplateKind.INVOICE,
    payload: TemplatePayload | None = None,
    schedule: RecurringSchedule | None = None,
    auto_post: bool = False,
    tenant_id: str = TENANT_ID,
    company_id: str = COMPANY_ID,
) -> RecurringTemplate:
    return service.create_template(
        tenant_id=tenant_id,
        installation_id=INSTALLATION_ID,
        company_id=company_id,
        company_engine_id=COMPANY_ENGINE_ID,
        kind=kind,
        name=name,
        schedule=schedule or _monthly_day_15(),
        payload=payload
        or (_invoice_payload() if kind == TemplateKind.INVOICE else _journal_payload()),
        auto_post=auto_post,
    )


# ---------------------------------------------------------------------------
# Template CRUD
# ---------------------------------------------------------------------------


def test_create_invoice_template() -> None:
    service, repo = _template_service()
    template = _make_template(service)
    assert template.status == TemplateStatus.ACTIVE
    assert template.total_amount_irr == 500_000
    assert repo.get_template(TENANT_ID, template.id) is template
    assert template.created_at == NOW


def test_create_journal_template() -> None:
    service, _ = _template_service()
    template = _make_template(service, kind=TemplateKind.JOURNAL)
    assert template.total_amount_irr == 1_000_000


def test_create_rejects_kind_payload_mismatch() -> None:
    service, _ = _template_service()
    with pytest.raises(
        InvalidRecurringTemplateError, match="needs a InvoiceTemplatePayload"
    ):
        _make_template(service, kind=TemplateKind.INVOICE, payload=_journal_payload())
    with pytest.raises(
        InvalidRecurringTemplateError, match="needs a JournalTemplatePayload"
    ):
        _make_template(service, kind=TemplateKind.JOURNAL, payload=_invoice_payload())


def test_create_rejects_unbalanced_journal() -> None:
    service, _ = _template_service()
    payload = JournalTemplatePayload(
        lines=(
            JournalTemplateLine(account_engine_id=21, debit_irr=100),
            JournalTemplateLine(account_engine_id=22, credit_irr=99),
        )
    )
    with pytest.raises(UnbalancedJournalError):
        _make_template(service, kind=TemplateKind.JOURNAL, payload=payload)


def test_create_rejects_zero_total_invoice() -> None:
    service, _ = _template_service()
    payload = InvoiceTemplatePayload(
        invoice_type="out", party_engine_id=7, lines=()
    )
    with pytest.raises(InvalidRecurringTemplateError, match="at least one line"):
        _make_template(service, payload=payload)


def test_get_template_is_tenant_scoped() -> None:
    service, _ = _template_service()
    template = _make_template(service)
    assert service.get_template(TENANT_ID, template.id).id == template.id
    with pytest.raises(RecurringTemplateNotFoundError):
        service.get_template(OTHER_TENANT, template.id)
    with pytest.raises(RecurringTemplateNotFoundError):
        service.get_template(TENANT_ID, "missing")


def test_list_templates_filters() -> None:
    service, _ = _template_service()
    _make_template(service, name="inv-1")
    _make_template(service, name="jrnl-1", kind=TemplateKind.JOURNAL)
    _make_template(service, name="inv-other-company", company_id="company-2")
    assert len(service.list_templates(TENANT_ID)) == 3
    assert len(service.list_templates(TENANT_ID, kind=TemplateKind.JOURNAL)) == 1
    assert len(service.list_templates(TENANT_ID, company_id="company-2")) == 1
    assert service.list_templates(OTHER_TENANT) == ()


def test_preview_computes_dates_amounts_and_clamps() -> None:
    service, _ = _template_service()
    template = _make_template(
        service,
        schedule=RecurringSchedule(
            kind=RecurringScheduleKind.MONTHLY,
            jalali_day=31,
            start_date=datetime.date(2026, 10, 23),
        ),
    )
    preview = service.preview_template(TENANT_ID, template.id, count=3)
    assert [(p.scheduled_date, p.jalali_date, p.clamped, p.total_amount_irr) for p in preview] == [
        (datetime.date(2026, 11, 21), "1405-08-30", True, 500_000),
        (datetime.date(2026, 12, 21), "1405-09-30", True, 500_000),
        (datetime.date(2027, 1, 20), "1405-10-30", True, 500_000),
    ]
    assert [p.occurrence_number for p in preview] == [1, 2, 3]


def test_preview_is_bounded() -> None:
    service, _ = _template_service()
    template = _make_template(service)
    with pytest.raises(InvalidRecurringTemplateError, match="count"):
        service.preview_template(TENANT_ID, template.id, count=0)
    with pytest.raises(InvalidRecurringTemplateError, match="count"):
        service.preview_template(TENANT_ID, template.id, count=121)


def test_pause_resume_are_idempotent() -> None:
    service, _ = _template_service()
    template = _make_template(service)
    assert service.pause_template(TENANT_ID, template.id).status == TemplateStatus.PAUSED
    assert service.pause_template(TENANT_ID, template.id).status == TemplateStatus.PAUSED
    assert service.resume_template(TENANT_ID, template.id).status == TemplateStatus.ACTIVE
    assert service.resume_template(TENANT_ID, template.id).status == TemplateStatus.ACTIVE


def test_completed_template_cannot_pause_or_resume() -> None:
    service, repo = _template_service()
    template = _make_template(
        service, schedule=_monthly_day_15(max_occurrences=1)
    )
    _generation_service(repo).generate_due(today=datetime.date(2027, 1, 10))
    stored = repo.get_template(TENANT_ID, template.id)
    assert stored is not None
    assert stored.status == TemplateStatus.COMPLETED
    with pytest.raises(InvalidRecurringTemplateError, match="completed"):
        service.pause_template(TENANT_ID, template.id)
    with pytest.raises(InvalidRecurringTemplateError, match="completed"):
        service.resume_template(TENANT_ID, template.id)


def test_update_edits_name_payload_and_auto_post() -> None:
    service, _ = _template_service()
    template = _make_template(service)
    updated = service.update_template(
        TENANT_ID,
        template.id,
        name="نام جدید",
        payload=_invoice_payload(unit_price=700_000),
        auto_post=True,
    )
    assert updated.name == "نام جدید"
    assert updated.total_amount_irr == 700_000
    assert updated.auto_post is True


def test_update_rejects_schedule_shape_change_once_occurrences_exist() -> None:
    service, repo = _template_service()
    template = _make_template(service)
    _generation_service(repo).generate_due(today=TODAY)
    with pytest.raises(InvalidRecurringTemplateError, match="schedule shape"):
        service.update_template(
            TENANT_ID,
            template.id,
            schedule=RecurringSchedule(
                kind=RecurringScheduleKind.MONTHLY,
                jalali_day=20,
                start_date=datetime.date(2026, 10, 23),
            ),
        )


def test_update_allows_end_condition_change_once_occurrences_exist() -> None:
    service, repo = _template_service()
    template = _make_template(service)
    _generation_service(repo).generate_due(today=TODAY)
    updated = service.update_template(
        TENANT_ID,
        template.id,
        schedule=_monthly_day_15(end=datetime.date(2027, 6, 30)),
    )
    assert updated.schedule.end_date == datetime.date(2027, 6, 30)


def test_update_rejects_payload_kind_mismatch() -> None:
    service, _ = _template_service()
    template = _make_template(service)
    with pytest.raises(
        InvalidRecurringTemplateError, match="needs a InvoiceTemplatePayload"
    ):
        service.update_template(TENANT_ID, template.id, payload=_journal_payload())


def test_delete_without_occurrences() -> None:
    service, repo = _template_service()
    template = _make_template(service)
    service.delete_template(TENANT_ID, template.id)
    assert repo.get_template(TENANT_ID, template.id) is None


def test_delete_with_occurrences_is_blocked() -> None:
    service, repo = _template_service()
    template = _make_template(service)
    _generation_service(repo).generate_due(today=TODAY)
    with pytest.raises(RecurringTemplateInUseError, match="already has occurrences"):
        service.delete_template(TENANT_ID, template.id)


def test_list_occurrences_requires_the_template() -> None:
    service, _ = _template_service()
    with pytest.raises(RecurringTemplateNotFoundError):
        service.list_occurrences(TENANT_ID, "missing")


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def test_generate_due_creates_drafts_linked_to_template_and_number() -> None:
    service, repo = _template_service()
    invoices = FakeInvoices()
    journals = FakeJournals()
    generator = _generation_service(repo, invoices, journals)
    invoice_template = _make_template(service)
    journal_template = _make_template(service, kind=TemplateKind.JOURNAL)

    report = generator.generate_due(today=TODAY)

    assert report.generated == 2
    assert report.skipped_existing == 0
    assert report.failed == 0
    assert len(invoices.drafts) == 1
    assert len(journals.drafts) == 1
    # Drafts are created, never posted, by default.
    assert invoices.finalized == [] and invoices.posted == []
    assert journals.posted == []
    # The draft carries the recurrence marker.
    assert "[اشتراک ماهانه — تکرار 1]" in invoices.drafts[0]["description"]
    assert invoices.drafts[0]["date"].date == datetime.date(2026, 11, 6)
    # Occurrences link template + number + engine document.
    occurrences = service.list_occurrences(TENANT_ID, invoice_template.id)
    assert len(occurrences) == 1
    occ = occurrences[0]
    assert occ.template_id == invoice_template.id
    assert occ.occurrence_number == 1
    assert occ.scheduled_date == datetime.date(2026, 11, 6)
    assert occ.jalali_date == "1405-08-15"
    assert occ.status == OccurrenceStatus.DRAFT
    assert occ.engine_document_id == 1
    assert occ.total_amount_irr == 500_000
    journal_occ = service.list_occurrences(TENANT_ID, journal_template.id)[0]
    assert journal_occ.engine_document_id == 1


def test_generate_due_is_idempotent_across_runs() -> None:
    service, repo = _template_service()
    invoices = FakeInvoices()
    generator = _generation_service(repo, invoices)

    template = _make_template(service)
    first = generator.generate_due(today=TODAY)
    second = generator.generate_due(today=TODAY)

    assert first.generated == 1
    assert second.generated == 0
    assert second.skipped_existing == 1
    assert len(invoices.drafts) == 1
    assert len(service.list_occurrences(TENANT_ID, template.id)) == 1


def test_generate_due_catches_up_all_due_occurrences() -> None:
    service, repo = _template_service()
    invoices = FakeInvoices()
    generator = _generation_service(repo, invoices)
    template = _make_template(service)

    report = generator.generate_due(today=datetime.date(2027, 1, 10))

    assert report.generated == 3
    occurrences = service.list_occurrences(TENANT_ID, template.id)
    assert [o.scheduled_date for o in occurrences] == [
        datetime.date(2026, 11, 6),
        datetime.date(2026, 12, 6),
        datetime.date(2027, 1, 5),
    ]
    assert [o.occurrence_number for o in occurrences] == [1, 2, 3]


def test_generate_due_skips_paused_templates_and_catches_up_on_resume() -> None:
    service, repo = _template_service()
    invoices = FakeInvoices()
    generator = _generation_service(repo, invoices)
    template = _make_template(service)
    service.pause_template(TENANT_ID, template.id)

    report = generator.generate_due(today=TODAY)
    assert report.templates_scanned == 0
    assert report.generated == 0
    assert len(invoices.drafts) == 0

    service.resume_template(TENANT_ID, template.id)
    report = generator.generate_due(today=TODAY)
    assert report.generated == 1  # the missed occurrence catches up


def test_generate_due_clamps_day_31() -> None:
    service, repo = _template_service()
    invoices = FakeInvoices()
    generator = _generation_service(repo, invoices)
    template = _make_template(
        service,
        schedule=RecurringSchedule(
            kind=RecurringScheduleKind.MONTHLY,
            jalali_day=31,
            start_date=datetime.date(2026, 10, 23),
        ),
    )
    generator.generate_due(today=TODAY)
    occ = service.list_occurrences(TENANT_ID, template.id)[0]
    assert occ.scheduled_date == datetime.date(2026, 11, 21)  # 1405/08/30
    assert occ.clamped is True
    assert occ.requested_jalali_day == 31
    assert occ.actual_jalali_day == 30
    assert invoices.drafts[0]["date"].date == datetime.date(2026, 11, 21)


def test_auto_post_denied_by_gate_leaves_a_draft() -> None:
    service, repo = _template_service()
    journals = FakeJournals()
    gate = FakeGate(_denied_gate())
    generator = _generation_service(repo, None, journals, gate)
    template = _make_template(
        service, kind=TemplateKind.JOURNAL, auto_post=True
    )
    generator.generate_due(today=TODAY)
    assert len(journals.drafts) == 1
    assert journals.posted == []
    occ = service.list_occurrences(TENANT_ID, template.id)[0]
    assert occ.status == OccurrenceStatus.DRAFT
    assert occ.gate_decision is not None
    assert occ.gate_decision["allowed"] is False
    assert occ.gate_decision["action"] == "draft"
    assert gate.calls[0]["event_type"] == "recurring.generate"
    assert gate.calls[0]["amount_irr"] == 1_000_000


def test_auto_post_allowed_posts_the_document() -> None:
    service, repo = _template_service()
    invoices = FakeInvoices()
    journals = FakeJournals()
    gate = FakeGate(_allowed_gate())
    generator = _generation_service(repo, invoices, journals, gate)
    invoice_template = _make_template(service, auto_post=True)
    journal_template = _make_template(
        service, kind=TemplateKind.JOURNAL, auto_post=True
    )
    report = generator.generate_due(today=TODAY)
    assert report.posted == 2
    # Invoice auto-post finalizes THEN posts (the events.py order).
    assert [ref.engine_id for ref in invoices.finalized] == [1]
    assert [ref.engine_id for ref in invoices.posted] == [1]
    assert [ref.engine_id for ref in journals.posted] == [1]
    invoice_occ = service.list_occurrences(TENANT_ID, invoice_template.id)[0]
    journal_occ = service.list_occurrences(TENANT_ID, journal_template.id)[0]
    assert invoice_occ.status == OccurrenceStatus.POSTED
    assert journal_occ.status == OccurrenceStatus.POSTED


def test_auto_post_without_a_wired_gate_leaves_a_draft() -> None:
    service, repo = _template_service()
    journals = FakeJournals()
    generator = _generation_service(repo, None, journals, gate=None)
    template = _make_template(
        service, kind=TemplateKind.JOURNAL, auto_post=True
    )
    generator.generate_due(today=TODAY)
    assert journals.posted == []
    occ = service.list_occurrences(TENANT_ID, template.id)[0]
    assert occ.status == OccurrenceStatus.DRAFT
    assert occ.gate_decision is not None
    assert occ.gate_decision["allowed"] is False
    assert occ.gate_decision["action"] == "draft"


def test_post_failure_keeps_the_draft_and_is_not_retried() -> None:
    service, repo = _template_service()
    journals = FakeJournals()
    journals.fail_post = "engine rejected the post"
    gate = FakeGate(_allowed_gate())
    generator = _generation_service(repo, None, journals, gate)
    template = _make_template(
        service, kind=TemplateKind.JOURNAL, auto_post=True
    )
    first = generator.generate_due(today=TODAY)
    assert first.generated == 1
    assert first.posted == 0
    occ = service.list_occurrences(TENANT_ID, template.id)[0]
    assert occ.status == OccurrenceStatus.DRAFT
    assert occ.error == "engine rejected the post"
    # The draft exists; a second run must NOT create another one.
    second = generator.generate_due(today=TODAY)
    assert second.generated == 0
    assert second.skipped_existing == 1
    assert len(journals.drafts) == 1


def test_draft_creation_failure_is_recorded_filed_and_retried() -> None:
    service, repo = _template_service()
    invoices = FakeInvoices()
    invoices.fail_create = "engine unavailable"
    queue = FakeReviewQueue()
    review = ReviewQueueCommandService(queue, FakeAudit())
    generator = _generation_service(repo, invoices, None, None, review, queue)
    template = _make_template(service)

    first = generator.generate_due(today=TODAY)
    assert first.generated == 0
    assert first.failed == 1
    assert len(first.failures) == 1
    assert first.failures[0].error == "engine unavailable"
    assert first.failures[0].review_item_id is not None
    occ = service.list_occurrences(TENANT_ID, template.id)[0]
    assert occ.status == OccurrenceStatus.FAILED
    assert occ.engine_document_id is None
    assert occ.error == "engine unavailable"
    assert len(queue.items) == 1
    assert queue.items[0].source_event_ref == (
        f"scheduled-job:scheduler.recurring_generation:{template.id}:2026-11-06"
    )

    # Still failing: no duplicate review item.
    second = generator.generate_due(today=TODAY)
    assert second.failed == 1
    assert len(queue.items) == 1

    # Healed: the retry succeeds under the SAME occurrence row/number.
    invoices.fail_create = None
    third = generator.generate_due(today=TODAY)
    assert third.generated == 1
    assert len(invoices.drafts) == 1
    occurrences = service.list_occurrences(TENANT_ID, template.id)
    assert len(occurrences) == 1
    assert occurrences[0].status == OccurrenceStatus.DRAFT
    assert occurrences[0].occurrence_number == 1
    assert occurrences[0].error is None


def test_failed_occurrence_retry_uses_the_fixed_template() -> None:
    """Editing the template after a failure heals the retry (snapshot
    refreshes — nothing was generated yet)."""
    service, repo = _template_service()
    invoices = FakeInvoices()
    invoices.fail_create = "party account missing"
    generator = _generation_service(repo, invoices)
    template = _make_template(service)
    generator.generate_due(today=TODAY)
    assert service.list_occurrences(TENANT_ID, template.id)[0].status == OccurrenceStatus.FAILED

    service.update_template(
        TENANT_ID, template.id, payload=_invoice_payload(unit_price=700_000)
    )
    invoices.fail_create = None
    generator.generate_due(today=TODAY)
    occ = service.list_occurrences(TENANT_ID, template.id)[0]
    assert occ.status == OccurrenceStatus.DRAFT
    assert occ.total_amount_irr == 700_000
    assert occ.snapshot["payload"]["lines"][0]["unit_price_irr"] == 700_000
    assert invoices.drafts[0]["lines"][0].unit_price.amount == 700_000


def test_editing_a_template_does_not_rewrite_generated_drafts() -> None:
    """The key snapshot proof: occurrence #1 keeps the old payload
    and its draft is untouched; occurrence #2 uses the new payload."""
    service, repo = _template_service()
    invoices = FakeInvoices()
    generator = _generation_service(repo, invoices)
    template = _make_template(service)

    generator.generate_due(today=TODAY)
    first_occ = service.list_occurrences(TENANT_ID, template.id)[0]
    assert first_occ.snapshot["payload"]["lines"][0]["unit_price_irr"] == 500_000

    service.update_template(
        TENANT_ID,
        template.id,
        name="اشتراک ماهانه (جدید)",
        payload=_invoice_payload(unit_price=700_000),
    )

    # The generated occurrence and its draft are untouched.
    first_occ_after = service.list_occurrences(TENANT_ID, template.id)[0]
    assert first_occ_after.snapshot == first_occ.snapshot
    assert first_occ_after.total_amount_irr == 500_000
    assert invoices.drafts[0]["lines"][0].unit_price.amount == 500_000
    assert invoices.drafts[0]["description"].startswith("صورت‌حساب ماهانه")

    # The next occurrence uses the edited template.
    generator.generate_due(today=datetime.date(2026, 12, 10))
    second_occ = service.list_occurrences(TENANT_ID, template.id)[1]
    assert second_occ.total_amount_irr == 700_000
    assert second_occ.snapshot["payload"]["lines"][0]["unit_price_irr"] == 700_000
    assert invoices.drafts[1]["lines"][0].unit_price.amount == 700_000
    assert "[اشتراک ماهانه (جدید) — تکرار 2]" in invoices.drafts[1]["description"]


def test_template_completes_when_max_occurrences_reached() -> None:
    service, repo = _template_service()
    generator = _generation_service(repo)
    template = _make_template(
        service, schedule=_monthly_day_15(max_occurrences=2)
    )
    report = generator.generate_due(today=datetime.date(2027, 1, 10))
    assert report.completed_templates == 1
    stored = repo.get_template(TENANT_ID, template.id)
    assert stored is not None
    assert stored.status == TemplateStatus.COMPLETED
    # A completed template generates nothing more.
    report = generator.generate_due(today=datetime.date(2027, 3, 10))
    assert report.generated == 0


def test_template_completes_when_end_date_passed() -> None:
    service, repo = _template_service()
    generator = _generation_service(repo)
    template = _make_template(
        service, schedule=_monthly_day_15(end=datetime.date(2026, 12, 6))
    )
    generator.generate_due(today=datetime.date(2027, 1, 10))
    stored = repo.get_template(TENANT_ID, template.id)
    assert stored is not None
    assert stored.status == TemplateStatus.COMPLETED


def test_failed_last_occurrence_blocks_completion() -> None:
    service, repo = _template_service()
    invoices = FakeInvoices()
    invoices.fail_create = "engine unavailable"
    generator = _generation_service(repo, invoices)
    template = _make_template(
        service, schedule=_monthly_day_15(max_occurrences=1)
    )
    generator.generate_due(today=datetime.date(2027, 1, 10))
    stored = repo.get_template(TENANT_ID, template.id)
    assert stored is not None
    assert stored.status == TemplateStatus.ACTIVE

    invoices.fail_create = None
    generator.generate_due(today=datetime.date(2027, 1, 10))
    stored = repo.get_template(TENANT_ID, template.id)
    assert stored is not None
    assert stored.status == TemplateStatus.COMPLETED


def test_one_templates_failure_does_not_block_others() -> None:
    service, repo = _template_service()
    invoices = FakeInvoices()
    invoices.fail_create = "engine unavailable"
    journals = FakeJournals()
    generator = _generation_service(repo, invoices, journals)
    _make_template(service, name="broken-invoice")
    _make_template(service, name="good-journal", kind=TemplateKind.JOURNAL)
    report = generator.generate_due(today=TODAY)
    assert report.generated == 1
    assert report.failed == 1
    assert len(journals.drafts) == 1
