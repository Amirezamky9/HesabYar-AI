"""MCP prompt tests (US-604, spec §13.6).

Two layers:

- **Unit tests** (pure renderers, no DB, no engine): every template
  renders against FIXTURE tool results (the US-602/US-603 tools are
  built in parallel — the prompts are designed against the ``prd.json``
  tool contracts, not their code) and the figure-containment property
  is proven: every digit-token in the rendered prompt appears in the
  arguments it was given. A render against empty results contains no
  figures at all beyond the company/period slots, proving the
  boilerplate itself holds no hardcoded figures.
- **Harness tests** (real SDK client + server over in-memory
  streams, real test Postgres): prompts/list shape, scope denial per
  prompt, the cross-company SECURITY property (a client scoped to
  company A cannot render a prompt for company B), and end-to-end
  renders via ``prompts/get``.

Tests are sync functions driving the async harness with
``asyncio.run()`` (no pytest-asyncio in this repo).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import Any

import pytest
from mcp.shared.exceptions import MCPError
from sqlalchemy import select
from sqlalchemy.orm import Session

from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.auth.scopes import KNOWN_SCOPES
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.persistence.postgres.models import (
    AiAccessAuditModel,
    ApiClientModel,
)
from hesabyar.interfaces.mcp import McpContext, create_mcp_server
from hesabyar.interfaces.mcp.errors import (
    MCP_ERROR_FORBIDDEN,
    MCP_ERROR_INTERNAL,
    MCP_ERROR_UNAUTHENTICATED,
)
from hesabyar.interfaces.mcp.prompts import (
    PROMPT_DEFINITIONS,
    PromptDefinition,
    _parse_tool_result,
    register_prompts,
    render_compare_month_over_month,
    render_explain_unusual_balance_movement,
    render_monthly_close_review,
    render_review_unreconciled_bank_activity,
    render_summarize_overdue_receivables,
    render_validate_financial_statements,
)
from hesabyar.interfaces.mcp.resources import register_resources
from tests.mcp.conftest import INSTALLATION_ID, World
from tests.mcp.harness import authenticate_secret, connected_client

#: Every scope any US-604 resource or prompt needs (plus the base scope).
ALL_US604_SCOPES: tuple[str, ...] = (
    "mcp:connect",
    "account:read",
    "banking:read",
    "company:read",
    "compliance:read",
    "integrations:read",
    "ledger:read",
    "reports:read",
    "review:read",
    "standards:read",
    "standards:validate",
    "tax:read",
)

#: ASCII + Persian digit runs. The containment test tokenizes both the
#: rendered prompt and the given arguments with this pattern.
_DIGIT_RUN = re.compile(r"[0-9\u06f0-\u06f9]+")


def _digit_tokens(text: str) -> set[str]:
    return set(_DIGIT_RUN.findall(text))


def _assert_cites_only_given(rendered: str, given: list[str]) -> None:
    """Every figure in ``rendered`` appears in the ``given`` arguments."""
    allowed: set[str] = set()
    for text in given:
        allowed |= _digit_tokens(text)
    extra = _digit_tokens(rendered) - allowed
    assert not extra, f"rendered figures not in the given data: {sorted(extra)}"


# -- fixture tool results (prd.json contracts; distinctive figures) ------------

_PROVENANCE = {
    "company_id": 301,
    "company_name": "Test Co",
    "period": {"label": "1405-08", "from": "2025-10-23", "to": "2025-11-21"},
    "currency": "IRR",
    "canonical_unit": "Rial",
    "source_engine": "Tryton",
    "report_type": "trial_balance",
    "generated_at": "2025-11-21T10:00:00+00:00",
    "snapshot_ref": "snap-12345",
    "ruleset_version": "1.0",
}

TRIAL_BALANCE: dict[str, Any] = {
    "balanced": True,
    "total_debit": 123456789,
    "total_credit": 123456789,
    "accounts": [
        {"code": "1000", "name": "Cash", "balance": 98765432},
        {"code": "2000", "name": "Payables", "balance": 24691357},
    ],
    "provenance": _PROVENANCE,
}

TRIAL_BALANCE_OTHER: dict[str, Any] = {
    "balanced": False,
    "total_debit": 777888999,
    "total_credit": 777888000,
    "accounts": [],
    "provenance": dict(_PROVENANCE, snapshot_ref="snap-99999"),
}

INCOME_STATEMENT: dict[str, Any] = {
    "total_revenue": 555111222,
    "total_expenses": 444000111,
    "net_income": 111111111,
    "provenance": dict(_PROVENANCE, report_type="income_statement"),
}

INCOME_STATEMENT_PRIOR: dict[str, Any] = {
    "total_revenue": 400000000,
    "total_expenses": 300000000,
    "net_income": 100000000,
    "provenance": dict(
        _PROVENANCE,
        report_type="income_statement",
        period={"label": "1405-07", "from": "2025-09-23", "to": "2025-10-22"},
    ),
}

BALANCE_SHEET: dict[str, Any] = {
    "total_assets": 888777666,
    "total_liabilities": 333222111,
    "total_equity": 555555555,
    "provenance": dict(_PROVENANCE, report_type="balance_sheet"),
}

BALANCE_SHEET_PRIOR: dict[str, Any] = {
    "total_assets": 800000000,
    "total_liabilities": 300000000,
    "total_equity": 500000000,
    "provenance": dict(
        _PROVENANCE,
        report_type="balance_sheet",
        period={"label": "1405-07", "from": "2025-09-23", "to": "2025-10-22"},
    ),
}

PENDING_REVIEW_ITEMS: dict[str, Any] = {
    "items": [
        {"id": "rev-42", "kind": "cheque", "amount": 13579246},
        {"id": "rev-43", "kind": "settlement", "amount": 86420975},
    ],
    "provenance": dict(_PROVENANCE, report_type="review_queue"),
}

GENERAL_LEDGER: dict[str, Any] = {
    "account": {"code": "1000", "name": "Cash"},
    "opening_balance": 111222333,
    "closing_balance": 98765432,
    "lines": [
        {"date": "2025-11-02", "debit": 50000000, "credit": 0},
        {"date": "2025-11-15", "debit": 0, "credit": 62457901},
    ],
    "provenance": dict(_PROVENANCE, report_type="general_ledger"),
}

RECEIVABLES_AGING: dict[str, Any] = {
    "as_of": "1405-08-30",
    "total_overdue": 24680135,
    "buckets": [
        {"label": "overdue_30", "amount": 10000000},
        {"label": "overdue_90", "amount": 14680135},
    ],
    "largest_debtors": [{"name": "Debtor A", "amount": 14680135}],
    "provenance": dict(_PROVENANCE, report_type="receivables_aging"),
}

BANK_RECONCILIATION_STATUS: dict[str, Any] = {
    "unmatched_lines": 7,
    "unmatched_total": 36925814,
    "oldest_unmatched_date": "2025-10-05",
    "journals": [{"name": "Melli", "unmatched": 5}, {"name": "Mellat", "unmatched": 2}],
    "provenance": dict(_PROVENANCE, report_type="bank_reconciliation"),
}

VALIDATION: dict[str, Any] = {
    "is_valid": False,
    "overall_outcome": "fail",
    "rules_failed": 2,
    "failed_rules": [
        {"rule_id": "TB001", "message": "Trial balance is unbalanced."},
        {"rule_id": "BS004", "message": "Assets do not equal liabilities plus equity."},
    ],
    "provenance": dict(_PROVENANCE, report_type="validation"),
}


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False)


# -- harness helpers (mirroring test_mcp_resources.py) ---------------------------


def _issue_key(
    session_factory: Callable[[], Session],
    settings: AppSettings,
    world: World,
    scopes: list[str],
    *,
    engine_company_id: str | None = None,
) -> tuple[str, str]:
    """Issue a key under the world's client; return (secret, key id)."""
    session = session_factory()
    try:
        service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
        client = session.get(ApiClientModel, world.client_id)
        assert client is not None
        issued = service.issue_key(
            session,
            client=client,
            name="us604 prompt test key",
            scopes=scopes,
            installation_id=INSTALLATION_ID,
            engine_company_id=engine_company_id,
        )
        session.commit()
        return issued.secret, issued.record.id
    finally:
        session.close()


def _make_us604_server(
    session_factory: Callable[[], Session],
    settings: AppSettings,
    *,
    bootstrap_secret: str | None = None,
) -> tuple[Any, McpContext]:
    """Build a server (+ context) with US-604 resources + prompts."""
    context = McpContext(
        settings=settings,
        session_factory=session_factory,
        transport="memory",
    )
    if bootstrap_secret is not None:
        context.bootstrap_client = authenticate_secret(
            session_factory, settings, bootstrap_secret
        )
    server = create_mcp_server(context)
    register_resources(server, context)
    register_prompts(server, context)
    return server, context


def _audit_rows(
    session_factory: Callable[[], Session], *, key_id: str, tool: str
) -> list[AiAccessAuditModel]:
    """This key's audit rows for ``tool``, oldest first."""
    session = session_factory()
    try:
        return list(
            session.execute(
                select(AiAccessAuditModel)
                .where(
                    AiAccessAuditModel.key_id == key_id,
                    AiAccessAuditModel.tool == tool,
                )
                .order_by(AiAccessAuditModel.created_at)
            )
            .scalars()
            .all()
        )
    finally:
        session.close()


def _get_prompt_text(
    session_factory: Callable[[], Session],
    settings: AppSettings,
    *,
    bootstrap_secret: str,
    name: str,
    arguments: dict[str, str],
) -> str:
    """Get a prompt; return its first message's text."""

    async def _run() -> str:
        server, _ = _make_us604_server(
            session_factory, settings, bootstrap_secret=bootstrap_secret
        )
        async with connected_client(server) as session:
            result: Any = await session.get_prompt(name, arguments)
            assert result.messages, "prompt has no messages"
            return str(result.messages[0].content.text)

    return asyncio.run(_run())


def _get_prompt_denied_code(
    session_factory: Callable[[], Session],
    settings: AppSettings,
    *,
    bootstrap_secret: str | None,
    name: str,
    arguments: dict[str, str],
) -> int:
    """Get a prompt; return the MCPError code (the call must fail)."""

    async def _run() -> int:
        server, _ = _make_us604_server(
            session_factory, settings, bootstrap_secret=bootstrap_secret
        )
        async with connected_client(server) as session:
            with pytest.raises(MCPError) as excinfo:
                await session.get_prompt(name, arguments)
            return excinfo.value.code

    return asyncio.run(_run())


# -- registration -----------------------------------------------------------------


def test_prompt_definitions_are_well_formed() -> None:
    """Six prompts, each company-scoped, scoped, and tool-backed."""
    assert len(PROMPT_DEFINITIONS) == 6
    for definition in PROMPT_DEFINITIONS:
        assert definition.scopes, definition.name
        assert set(definition.scopes) <= KNOWN_SCOPES, definition.name
        assert definition.tools, definition.name
        company_args = [a for a in definition.arguments if a.name == "company"]
        assert len(company_args) == 1 and company_args[0].required, definition.name


def test_register_prompts_records_scopes() -> None:
    """Registration records every prompt's scopes in the registry."""

    def _no_session() -> Session:
        raise RuntimeError("no session in a registration-only test")

    context = McpContext(settings=AppSettings(), session_factory=_no_session)
    server = create_mcp_server(context)
    register_prompts(server, context)
    for definition in PROMPT_DEFINITIONS:
        assert context.registry.scopes_for_prompt(definition.name) == frozenset(
            definition.scopes
        )


# -- unit tests: renderers cite only tool-provided data ---------------------------


def test_render_monthly_close_review() -> None:
    rendered = render_monthly_close_review(
        company_name="Test Co",
        period="1405-08",
        trial_balance=TRIAL_BALANCE,
        income_statement=INCOME_STATEMENT,
        pending_review_items=PENDING_REVIEW_ITEMS,
    )
    assert "Test Co" in rendered
    assert "123456789" in rendered  # trial balance total
    assert "555111222" in rendered  # revenue
    assert "13579246" in rendered  # review item amount
    assert "snap-12345" in rendered  # provenance cited
    _assert_cites_only_given(
        rendered,
        [
            "Test Co",
            "1405-08",
            _json(TRIAL_BALANCE),
            _json(INCOME_STATEMENT),
            _json(PENDING_REVIEW_ITEMS),
        ],
    )


def test_render_explain_unusual_balance_movement() -> None:
    rendered = render_explain_unusual_balance_movement(
        company_name="Test Co",
        account="1000",
        period="1405-08",
        general_ledger=GENERAL_LEDGER,
        trial_balance=TRIAL_BALANCE,
    )
    assert "98765432" in rendered  # closing balance
    assert "62457901" in rendered  # largest credit line
    _assert_cites_only_given(
        rendered,
        ["Test Co", "1000", "1405-08", _json(GENERAL_LEDGER), _json(TRIAL_BALANCE)],
    )


def test_render_summarize_overdue_receivables() -> None:
    rendered = render_summarize_overdue_receivables(
        company_name="Test Co",
        as_of="1405-08-30",
        receivables_aging=RECEIVABLES_AGING,
    )
    assert "24680135" in rendered  # total overdue
    assert "Debtor A" in rendered
    _assert_cites_only_given(
        rendered, ["Test Co", "1405-08-30", _json(RECEIVABLES_AGING)]
    )


def test_render_review_unreconciled_bank_activity() -> None:
    rendered = render_review_unreconciled_bank_activity(
        company_name="Test Co",
        period="1405-08",
        bank_reconciliation_status=BANK_RECONCILIATION_STATUS,
    )
    assert "36925814" in rendered  # unmatched total
    assert "Mellat" in rendered
    _assert_cites_only_given(
        rendered, ["Test Co", "1405-08", _json(BANK_RECONCILIATION_STATUS)]
    )


def test_render_validate_financial_statements() -> None:
    rendered = render_validate_financial_statements(
        company_name="Test Co",
        period="1405-08",
        validation=VALIDATION,
        balance_sheet=BALANCE_SHEET,
        income_statement=INCOME_STATEMENT,
    )
    assert "TB001" in rendered  # failed rule
    assert "888777666" in rendered  # total assets
    _assert_cites_only_given(
        rendered,
        ["Test Co", "1405-08", _json(VALIDATION), _json(BALANCE_SHEET), _json(INCOME_STATEMENT)],
    )


def test_render_compare_month_over_month() -> None:
    rendered = render_compare_month_over_month(
        company_name="Test Co",
        current_period="1405-08",
        prior_period="1405-07",
        current_income_statement=INCOME_STATEMENT,
        prior_income_statement=INCOME_STATEMENT_PRIOR,
        current_balance_sheet=BALANCE_SHEET,
        prior_balance_sheet=BALANCE_SHEET_PRIOR,
    )
    assert "555111222" in rendered  # current revenue
    assert "400000000" in rendered  # prior revenue
    assert "888777666" in rendered  # current assets
    # No computed delta: the revenue difference (155111222) must NOT
    # appear — the template presents both periods and lets the model
    # compare.
    assert "155111222" not in rendered
    _assert_cites_only_given(
        rendered,
        [
            "Test Co",
            "1405-08",
            "1405-07",
            _json(INCOME_STATEMENT),
            _json(INCOME_STATEMENT_PRIOR),
            _json(BALANCE_SHEET),
            _json(BALANCE_SHEET_PRIOR),
        ],
    )


def test_render_with_empty_results_has_no_figures() -> None:
    """Empty tool results → no figures beyond the company/period slots.

    This proves the boilerplate holds no hardcoded figures: with
    nothing supplied, the render contains no digits at all except the
    period's own digits.
    """
    rendered = render_monthly_close_review(
        company_name="Test Co",
        period="1405-08",
        trial_balance={},
        income_statement={},
        pending_review_items=None,
    )
    assert "ارائه نشده است" in rendered
    assert _digit_tokens(rendered) == {"1405", "08"}


def test_renderers_are_deterministic() -> None:
    """The same inputs render byte-identical output, twice."""
    kwargs: dict[str, Any] = {
        "company_name": "Test Co",
        "period": "1405-08",
        "trial_balance": TRIAL_BALANCE,
        "income_statement": INCOME_STATEMENT,
        "pending_review_items": PENDING_REVIEW_ITEMS,
    }
    assert render_monthly_close_review(**kwargs) == render_monthly_close_review(**kwargs)


def test_render_follows_the_figures_it_is_given() -> None:
    """A different fixture changes the figures — nothing is hardcoded."""
    first = render_monthly_close_review(
        company_name="Test Co",
        period="1405-08",
        trial_balance=TRIAL_BALANCE,
        income_statement=INCOME_STATEMENT,
        pending_review_items=None,
    )
    second = render_monthly_close_review(
        company_name="Test Co",
        period="1405-08",
        trial_balance=TRIAL_BALANCE_OTHER,
        income_statement=INCOME_STATEMENT,
        pending_review_items=None,
    )
    assert "123456789" in first
    assert "123456789" not in second
    assert "777888999" in second
    assert "777888999" not in first


def test_parse_tool_result_rejects_malformed_json() -> None:
    """A malformed tool-result argument fails clearly (no invented data)."""
    with pytest.raises(MCPError) as excinfo:
        _parse_tool_result("trial_balance", "{not json")
    assert excinfo.value.code == MCP_ERROR_INTERNAL
    with pytest.raises(MCPError) as excinfo:
        _parse_tool_result("trial_balance", "[1, 2, 3]")
    assert excinfo.value.code == MCP_ERROR_INTERNAL
    assert _parse_tool_result("trial_balance", None) is None
    assert _parse_tool_result("trial_balance", '{"a": 1}') == {"a": 1}


# -- harness: list ------------------------------------------------------------------


def test_prompts_listed(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """prompts/list returns all six prompts with their arguments."""
    secret, _ = _issue_key(session_factory, settings, world, list(ALL_US604_SCOPES))

    async def _run() -> dict[str, dict[str, bool]]:
        server, _ = _make_us604_server(
            session_factory, settings, bootstrap_secret=secret
        )
        async with connected_client(server) as session:
            prompts = await session.list_prompts()
            return {
                p.name: {a.name: bool(a.required) for a in (p.arguments or [])}
                for p in prompts.prompts
            }

    listed = asyncio.run(_run())
    assert set(listed) == {d.name for d in PROMPT_DEFINITIONS}
    for definition in PROMPT_DEFINITIONS:
        expected = {a.name: a.required for a in definition.arguments}
        assert listed[definition.name] == expected, definition.name


# -- harness: scope denial per prompt -----------------------------------------------


@pytest.mark.parametrize(
    "definition", PROMPT_DEFINITIONS, ids=[d.name for d in PROMPT_DEFINITIONS]
)
def test_prompt_scope_denied(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    definition: PromptDefinition,
) -> None:
    """A key missing one required scope cannot render the prompt."""
    missing = definition.scopes[0]
    scopes = [scope for scope in ALL_US604_SCOPES if scope != missing]
    secret, key_id = _issue_key(session_factory, settings, world, scopes)
    arguments = {"company": world.company_a_id}
    code = _get_prompt_denied_code(
        session_factory,
        settings,
        bootstrap_secret=secret,
        name=definition.name,
        arguments=arguments,
    )
    assert code == MCP_ERROR_FORBIDDEN
    rows = _audit_rows(session_factory, key_id=key_id, tool=definition.name)
    assert len(rows) == 1
    assert rows[0].outcome == "denied"
    assert rows[0].kind == "prompt"
    # Prompt scopes are enforced by the middleware (exact name match),
    # so the denial is recorded as "missing_scope".
    assert rows[0].error_code == "missing_scope"


def test_prompt_without_identity_fails_unauthenticated(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """No bootstrap client and no bearer token: the prompt is refused."""
    code = _get_prompt_denied_code(
        session_factory,
        settings,
        bootstrap_secret=None,
        name="monthly-close-review",
        arguments={"company": world.company_a_id},
    )
    assert code == MCP_ERROR_UNAUTHENTICATED


# -- harness: the cross-company SECURITY property -------------------------------------


def _minimal_arguments(definition: PromptDefinition, company: str) -> dict[str, str]:
    """The smallest valid argument set for a prompt (empty tool results)."""
    arguments: dict[str, str] = {"company": company}
    for argument in definition.arguments:
        if argument.required and argument.name != "company":
            # Slot arguments get a label; tool-result arguments get {}.
            arguments[argument.name] = (
                "1405-08" if argument.name in ("period", "as_of", "current_period", "prior_period")
                else "1000" if argument.name == "account"
                else "{}"
            )
    return arguments


@pytest.mark.parametrize(
    "definition", PROMPT_DEFINITIONS, ids=[d.name for d in PROMPT_DEFINITIONS]
)
def test_prompt_cross_company_denied(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    definition: PromptDefinition,
) -> None:
    """SECURITY: a client scoped to company A cannot render a prompt for company B."""
    secret, key_id = _issue_key(
        session_factory,
        settings,
        world,
        list(ALL_US604_SCOPES),
        engine_company_id=world.company_a_id,
    )
    arguments = _minimal_arguments(definition, world.company_b_id)
    code = _get_prompt_denied_code(
        session_factory,
        settings,
        bootstrap_secret=secret,
        name=definition.name,
        arguments=arguments,
    )
    assert code == MCP_ERROR_FORBIDDEN
    rows = _audit_rows(session_factory, key_id=key_id, tool=definition.name)
    assert len(rows) == 1
    assert rows[0].outcome == "denied"
    assert rows[0].kind == "prompt"
    # The company check runs in the prompt handler, so the middleware
    # records the handler-raised auth error as "forbidden".
    assert rows[0].error_code == "forbidden"


# -- harness: end-to-end renders --------------------------------------------------------


def _full_arguments(name: str, company: str) -> dict[str, str]:
    """Complete fixture arguments per prompt (company filled in)."""
    base = {
        "monthly-close-review": {
            "period": "1405-08",
            "trial_balance": _json(TRIAL_BALANCE),
            "income_statement": _json(INCOME_STATEMENT),
            "pending_review_items": _json(PENDING_REVIEW_ITEMS),
        },
        "explain-unusual-balance-movement": {
            "account": "1000",
            "period": "1405-08",
            "general_ledger": _json(GENERAL_LEDGER),
            "trial_balance": _json(TRIAL_BALANCE),
        },
        "summarize-overdue-receivables": {
            "as_of": "1405-08-30",
            "receivables_aging": _json(RECEIVABLES_AGING),
        },
        "review-unreconciled-bank-activity": {
            "period": "1405-08",
            "bank_reconciliation_status": _json(BANK_RECONCILIATION_STATUS),
        },
        "validate-financial-statements": {
            "period": "1405-08",
            "validation": _json(VALIDATION),
            "balance_sheet": _json(BALANCE_SHEET),
            "income_statement": _json(INCOME_STATEMENT),
        },
        "compare-month-over-month": {
            "current_period": "1405-08",
            "prior_period": "1405-07",
            "current_income_statement": _json(INCOME_STATEMENT),
            "prior_income_statement": _json(INCOME_STATEMENT_PRIOR),
            "current_balance_sheet": _json(BALANCE_SHEET),
            "prior_balance_sheet": _json(BALANCE_SHEET_PRIOR),
        },
    }
    return {"company": company, **base[name]}


@pytest.mark.parametrize(
    "definition", PROMPT_DEFINITIONS, ids=[d.name for d in PROMPT_DEFINITIONS]
)
def test_prompt_renders_end_to_end(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    definition: PromptDefinition,
) -> None:
    """prompts/get renders the template over the given fixture results.

    The containment check here is weaker than the unit tests' (the
    company argument is a uuid, whose digit runs widen the allowed
    set) — the strict proof lives in the unit tests; this proves the
    protocol path renders the same template.
    """
    secret, key_id = _issue_key(session_factory, settings, world, list(ALL_US604_SCOPES))
    arguments = _full_arguments(definition.name, world.company_a_id)
    rendered = _get_prompt_text(
        session_factory,
        settings,
        bootstrap_secret=secret,
        name=definition.name,
        arguments=arguments,
    )
    assert "Company 301" in rendered  # the resolved company name
    _assert_cites_only_given(rendered, [*arguments.values(), "Company 301"])
    rows = _audit_rows(session_factory, key_id=key_id, tool=definition.name)
    assert len(rows) == 1
    assert rows[0].outcome == "allowed"
    assert rows[0].kind == "prompt"


def test_prompt_renders_without_optional_arguments(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """Optional tool results may be omitted; the template says so."""
    secret, _ = _issue_key(session_factory, settings, world, list(ALL_US604_SCOPES))
    rendered = _get_prompt_text(
        session_factory,
        settings,
        bootstrap_secret=secret,
        name="monthly-close-review",
        arguments={
            "company": world.company_a_id,
            "period": "1405-08",
            "trial_balance": _json(TRIAL_BALANCE),
            "income_statement": _json(INCOME_STATEMENT),
        },
    )
    assert "ارائه نشده است" in rendered
    assert "123456789" in rendered


def test_prompt_malformed_tool_result_errors(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """A malformed tool-result argument is a clear error, not a render."""
    secret, key_id = _issue_key(session_factory, settings, world, list(ALL_US604_SCOPES))
    code = _get_prompt_denied_code(
        session_factory,
        settings,
        bootstrap_secret=secret,
        name="monthly-close-review",
        arguments={
            "company": world.company_a_id,
            "period": "1405-08",
            "trial_balance": "{not json",
            "income_statement": _json(INCOME_STATEMENT),
        },
    )
    assert code == MCP_ERROR_INTERNAL
    rows = _audit_rows(session_factory, key_id=key_id, tool="monthly-close-review")
    assert len(rows) == 1
    assert rows[0].outcome == "error"
