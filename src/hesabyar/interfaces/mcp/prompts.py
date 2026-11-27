"""MCP prompts (US-604, spec §13.6).

Six deterministic prompt templates over the read-only tools. A prompt
never invents figures: it renders the tool results the HOST passes in
as JSON arguments, verbatim, inside a Persian framing that tells the
model what to do with them and which provenance to cite. The template
itself contains no figures at all — no counts, no deltas, no
percentages, no numbered steps, no digits in the boilerplate — so a
render against fixture tool results can be proven to cite only
tool-provided data (``test_prompt_cites_only_tool_data``).

The host flow is: call the read-only tools first (US-602/US-603),
then pass their results — unchanged — to ``prompts/get``. The prompt
handler resolves the ``company`` argument against the authenticated
grant (a prompt for another company's data fails closed) and checks
the prompt's scopes; it never calls the tools itself, so prompt
rendering needs no engine access and no live tools. The sibling tools
are built in parallel, so the prompts are designed against the
``prd.json`` tool contracts; prompt↔tool integration is verified by
the coordinator at merge time / in CI.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from hesabyar.infrastructure.persistence.postgres.models import EngineCompanyModel
from hesabyar.interfaces.mcp.auth import resolve_company_row
from hesabyar.interfaces.mcp.context import McpContext
from hesabyar.interfaces.mcp.errors import CompanyAccessError, forbidden, internal_error
from hesabyar.interfaces.mcp.guards import authenticated_client, require_mcp_context
from hesabyar.interfaces.mcp.registry import ToolRegistry


@dataclass(frozen=True)
class PromptArgumentDef:
    """One prompt argument: name, required flag, description."""

    name: str
    required: bool
    description: str


@dataclass(frozen=True)
class PromptDefinition:
    """One prompt: name, scopes, description, arguments, source tools."""

    name: str
    scopes: tuple[str, ...]
    description: str
    arguments: tuple[PromptArgumentDef, ...]
    #: US-602/US-603 tools whose results the template renders.
    tools: tuple[str, ...]


PROMPT_DEFINITIONS: tuple[PromptDefinition, ...] = (
    PromptDefinition(
        name="monthly-close-review",
        scopes=("reports:read", "review:read"),
        description=(
            "Monthly close review: check the trial balance and income "
            "statement for a period and list what blocks the close."
        ),
        arguments=(
            PromptArgumentDef("company", True, "Company row id or engine id."),
            PromptArgumentDef("period", True, "Period label, e.g. 1405-08."),
            PromptArgumentDef(
                "trial_balance", True, "get_trial_balance result, as JSON."
            ),
            PromptArgumentDef(
                "income_statement", True, "get_income_statement result, as JSON."
            ),
            PromptArgumentDef(
                "pending_review_items",
                False,
                "list_pending_review_items result, as JSON.",
            ),
        ),
        tools=("get_trial_balance", "get_income_statement", "list_pending_review_items"),
    ),
    PromptDefinition(
        name="explain-unusual-balance-movement",
        scopes=("ledger:read", "reports:read"),
        description=(
            "Explain an unusual balance movement for one account in a "
            "period, from the general ledger and trial balance."
        ),
        arguments=(
            PromptArgumentDef("company", True, "Company row id or engine id."),
            PromptArgumentDef("account", True, "Account code or name."),
            PromptArgumentDef("period", True, "Period label, e.g. 1405-08."),
            PromptArgumentDef(
                "general_ledger", True, "get_general_ledger result, as JSON."
            ),
            PromptArgumentDef(
                "trial_balance", False, "get_trial_balance result, as JSON."
            ),
        ),
        tools=("get_general_ledger", "get_trial_balance"),
    ),
    PromptDefinition(
        name="summarize-overdue-receivables",
        scopes=("reports:read",),
        description=(
            "Summarize overdue receivables as of a date, from the "
            "receivables aging report."
        ),
        arguments=(
            PromptArgumentDef("company", True, "Company row id or engine id."),
            PromptArgumentDef("as_of", True, "As-of date, e.g. 1405-08-30."),
            PromptArgumentDef(
                "receivables_aging", True, "get_receivables_aging result, as JSON."
            ),
        ),
        tools=("get_receivables_aging",),
    ),
    PromptDefinition(
        name="review-unreconciled-bank-activity",
        scopes=("banking:read",),
        description=(
            "Review unreconciled bank activity for a period, from the bank "
            "reconciliation status."
        ),
        arguments=(
            PromptArgumentDef("company", True, "Company row id or engine id."),
            PromptArgumentDef("period", True, "Period label, e.g. 1405-08."),
            PromptArgumentDef(
                "bank_reconciliation_status",
                True,
                "get_bank_reconciliation_status result, as JSON.",
            ),
        ),
        tools=("get_bank_reconciliation_status",),
    ),
    PromptDefinition(
        name="validate-financial-statements",
        scopes=("reports:read", "standards:validate"),
        description=(
            "Validate the financial statements for a period: summarize the "
            "validation result and what must be fixed."
        ),
        arguments=(
            PromptArgumentDef("company", True, "Company row id or engine id."),
            PromptArgumentDef("period", True, "Period label, e.g. 1405-08."),
            PromptArgumentDef(
                "validation", True, "validate_financial_statement result, as JSON."
            ),
            PromptArgumentDef(
                "balance_sheet", False, "get_balance_sheet result, as JSON."
            ),
            PromptArgumentDef(
                "income_statement", False, "get_income_statement result, as JSON."
            ),
        ),
        tools=(
            "validate_financial_statement",
            "get_balance_sheet",
            "get_income_statement",
        ),
    ),
    PromptDefinition(
        name="compare-month-over-month",
        scopes=("reports:read",),
        description=(
            "Compare month-over-month performance: current vs prior period "
            "income statement (and balance sheet, when given)."
        ),
        arguments=(
            PromptArgumentDef("company", True, "Company row id or engine id."),
            PromptArgumentDef("current_period", True, "Current period, e.g. 1405-08."),
            PromptArgumentDef("prior_period", True, "Prior period, e.g. 1405-07."),
            PromptArgumentDef(
                "current_income_statement",
                True,
                "get_income_statement result for the current period, as JSON.",
            ),
            PromptArgumentDef(
                "prior_income_statement",
                True,
                "get_income_statement result for the prior period, as JSON.",
            ),
            PromptArgumentDef(
                "current_balance_sheet",
                False,
                "get_balance_sheet result for the current period, as JSON.",
            ),
            PromptArgumentDef(
                "prior_balance_sheet",
                False,
                "get_balance_sheet result for the prior period, as JSON.",
            ),
        ),
        tools=("get_income_statement", "get_balance_sheet"),
    ),
)

_PROMPT_BY_NAME: dict[str, PromptDefinition] = {
    definition.name: definition for definition in PROMPT_DEFINITIONS
}


# -- rendering ------------------------------------------------------------------

#: Rules appended to every prompt. Deliberately digit-free: the only
#: figures in a rendered prompt come from the tool results it is given.
_RULES = (
    "قوانین:\n"
    "• فقط از ارقام و اطلاعاتی استفاده کن که در بخش داده‌های ورودی آمده است؛ هیچ رقمی را حدس نزن و نساز.\n"
    "• هر رقمی را که نقل می‌کنی، همراه با منشأ آن (provenance) از همان نتیجهٔ ابزار ذکر کن.\n"
    "• همهٔ مبلغ‌ها به ریال هستند.\n"
    "• اگر داده‌ای کم است، صریحاً بگو کدام داده کم است و کدام ابزار باید فراخوانی شود.\n"
    "• مدل این گزارش را تفسیر می‌کند؛ حقیقت مالی نمی‌سازد. پیش از هر اقدامی، ارقام را با پیشخوان حساب‌یار بررسی کن."
)


def _tool_block(label: str, result: dict[str, Any] | None) -> str:
    """Render one tool result verbatim as a JSON block, or 'not supplied'.

    The result is dumped unchanged (no reformatting, no rounding, no
    computed figures): every figure in the block already appears in
    the argument the host passed.
    """
    if result is None:
        return f"{label}\nارائه نشده است."
    return f"{label}\n```json\n{json.dumps(result, ensure_ascii=False, indent=2)}\n```"


def render_monthly_close_review(
    *,
    company_name: str,
    period: str,
    trial_balance: dict[str, Any],
    income_statement: dict[str, Any],
    pending_review_items: dict[str, Any] | None,
) -> str:
    """Render the monthly-close-review template (pure function)."""
    return (
        f"بازبینی بستن ماهانه — {company_name} — دورهٔ {period}\n\n"
        "هدف: بازبینی بستن ماهانهٔ شرکت برای دورهٔ یادشده و مشخص کردن مواردی که پیش از بستن نیاز به رسیدگی دارند.\n\n"
        "داده‌های ورودی:\n"
        f"{_tool_block('تراز آزمایشی (get_trial_balance):', trial_balance)}\n\n"
        f"{_tool_block('صورت سود و زیان (get_income_statement):', income_statement)}\n\n"
        f"{_tool_block('موارد در انتظار بازبینی (list_pending_review_items):', pending_review_items)}\n\n"
        "کارهایی که باید انجام دهی:\n"
        "• بررسی کن که آیا تراز آزمایشی متوازن است و جمع بدهکار با جمع بستانکار برابر است.\n"
        "• اقلامی را که مانع بستن ماهانه می‌شوند فهرست کن.\n"
        "• موارد در انتظار بازبینی را مرور کن و بگو کدام‌یک پیش از بستن باید حل شوند.\n\n"
        f"{_RULES}"
    )


def render_explain_unusual_balance_movement(
    *,
    company_name: str,
    account: str,
    period: str,
    general_ledger: dict[str, Any],
    trial_balance: dict[str, Any] | None,
) -> str:
    """Render the explain-unusual-balance-movement template (pure function)."""
    return (
        f"توضیح حرکت غیرعادی مانده — {company_name} — حساب {account} — دورهٔ {period}\n\n"
        "هدف: توضیح این‌که چرا ماندهٔ حساب یادشده در دورهٔ یادشده حرکت غیرعادی داشته است.\n\n"
        "داده‌های ورودی:\n"
        f"{_tool_block('دفتر کل (get_general_ledger):', general_ledger)}\n\n"
        f"{_tool_block('تراز آزمایشی (get_trial_balance):', trial_balance)}\n\n"
        "کارهایی که باید انجام دهی:\n"
        "• بزرگ‌ترین تراکنش‌های دوره را از دفتر کل پیدا کن و بگو هرکدام چه اثری بر مانده گذاشته‌اند.\n"
        "• اگر حرکت مانده با تراکنش‌های دفتر کل قابل توضیح نیست، صریحاً بگو چه داده‌ای کم است.\n\n"
        f"{_RULES}"
    )


def render_summarize_overdue_receivables(
    *,
    company_name: str,
    as_of: str,
    receivables_aging: dict[str, Any],
) -> str:
    """Render the summarize-overdue-receivables template (pure function)."""
    return (
        f"خلاصهٔ مطالبات معوق — {company_name} — تا تاریخ {as_of}\n\n"
        "هدف: خلاصه کردن مطالبات معوق شرکت و مشخص کردن کهنه‌ترین و بزرگ‌ترین آن‌ها.\n\n"
        "داده‌های ورودی:\n"
        f"{_tool_block('سررسید مطالبات (get_receivables_aging):', receivables_aging)}\n\n"
        "کارهایی که باید انجام دهی:\n"
        "• مطالبات معوق را بر اساس قدمت سررسید گروه‌بندی کن.\n"
        "• بزرگ‌ترین بدهکاران معوق را نام ببر.\n"
        "• بگو کدام مطالبات نیاز به پیگیری فوری دارند و چرا.\n\n"
        f"{_RULES}"
    )


def render_review_unreconciled_bank_activity(
    *,
    company_name: str,
    period: str,
    bank_reconciliation_status: dict[str, Any],
) -> str:
    """Render the review-unreconciled-bank-activity template (pure function)."""
    return (
        f"بازبینی فعالیت بانکی مغایرت‌دار — {company_name} — دورهٔ {period}\n\n"
        "هدف: بازبینی تراکنش‌های بانکی که هنوز با دفاتر مغایرت‌گیری نشده‌اند.\n\n"
        "داده‌های ورودی:\n"
        f"{_tool_block('وضعیت مغایرت بانکی (get_bank_reconciliation_status):', bank_reconciliation_status)}\n\n"
        "کارهایی که باید انجام دهی:\n"
        "• تراکنش‌های مغایرت‌دار را فهرست کن و برای هرکدام علت احتمالی مغایرت را بگو.\n"
        "• بگو کدام مغایرت‌ها قدیمی‌تر هستند و باید اول رسیدگی شوند.\n\n"
        f"{_RULES}"
    )


def render_validate_financial_statements(
    *,
    company_name: str,
    period: str,
    validation: dict[str, Any],
    balance_sheet: dict[str, Any] | None,
    income_statement: dict[str, Any] | None,
) -> str:
    """Render the validate-financial-statements template (pure function)."""
    return (
        f"اعتبارسنجی صورت‌های مالی — {company_name} — دورهٔ {period}\n\n"
        "هدف: بررسی این‌که صورت‌های مالی دورهٔ یادشده با استانداردهای حسابداری سازگار هستند یا نه.\n\n"
        "داده‌های ورودی:\n"
        f"{_tool_block('نتیجهٔ اعتبارسنجی (validate_financial_statement):', validation)}\n\n"
        f"{_tool_block('ترازنامه (get_balance_sheet):', balance_sheet)}\n\n"
        f"{_tool_block('صورت سود و زیان (get_income_statement):', income_statement)}\n\n"
        "کارهایی که باید انجام دهی:\n"
        "• نتیجهٔ اعتبارسنجی را خلاصه کن: کدام قاعده‌ها رد شده‌اند و پیامد هرکدام چیست.\n"
        "• اگر صورت‌ها معتبر نیستند، بگو چه اصلاحاتی لازم است.\n"
        "• هیچ قاعده‌ای را که در نتیجهٔ اعتبارسنجی نیامده است، خودت ارزیابی نکن.\n\n"
        f"{_RULES}"
    )


def render_compare_month_over_month(
    *,
    company_name: str,
    current_period: str,
    prior_period: str,
    current_income_statement: dict[str, Any],
    prior_income_statement: dict[str, Any],
    current_balance_sheet: dict[str, Any] | None,
    prior_balance_sheet: dict[str, Any] | None,
) -> str:
    """Render the compare-month-over-month template (pure function).

    The template presents the two periods side by side and asks the
    model to compare them — it never computes a delta itself, so no
    figure in the output is absent from the tool results.
    """
    return (
        f"مقایسهٔ عملکرد ماه به ماه — {company_name} — دورهٔ جاری {current_period} در برابر دورهٔ پیشین {prior_period}\n\n"
        "هدف: مقایسهٔ عملکرد مالی دورهٔ جاری با دورهٔ پیشین و مشخص کردن مهم‌ترین تغییرها.\n\n"
        "داده‌های ورودی:\n"
        f"{_tool_block('صورت سود و زیان دورهٔ جاری (get_income_statement):', current_income_statement)}\n\n"
        f"{_tool_block('صورت سود و زیان دورهٔ پیشین (get_income_statement):', prior_income_statement)}\n\n"
        f"{_tool_block('ترازنامهٔ دورهٔ جاری (get_balance_sheet):', current_balance_sheet)}\n\n"
        f"{_tool_block('ترازنامهٔ دورهٔ پیشین (get_balance_sheet):', prior_balance_sheet)}\n\n"
        "کارهایی که باید انجام دهی:\n"
        "• درآمدها و هزینه‌های دو دوره را کنار هم بگذار و مهم‌ترین تغییرها را نام ببر.\n"
        "• برای هر تغییر مهم، بگو آیا از داده‌های ورودی قابل توضیح است یا نیاز به بررسی بیشتر دارد.\n"
        "• نتیجه‌گیری را فقط بر اساس ارقام داده‌های ورودی بنویس.\n\n"
        f"{_RULES}"
    )


# -- handlers -------------------------------------------------------------------

def _parse_tool_result(name: str, raw: str | None) -> dict[str, Any] | None:
    """Parse one tool-result argument (JSON object) or fail clearly.

    The host must pass the tool result unchanged: a malformed argument
    is a client error, reported as such instead of rendering a prompt
    around invented data.
    """
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise internal_error(
            f"Argument {name!r} is not valid JSON: pass the tool result unchanged."
        ) from None
    if not isinstance(parsed, dict):
        raise internal_error(
            f"Argument {name!r} must be a JSON object: pass the tool result unchanged."
        )
    return parsed


async def _resolve_prompt_company(
    ctx: Context | None, *, name: str, company: str
) -> EngineCompanyModel:
    """Enforce a prompt's scopes and resolve its ``company`` argument.

    The scopes come from the prompt's definition (single source of
    truth); the company is resolved against the grant, never trusted.
    Returns the company row (its name fills the template's company
    slot); prompts need no application services beyond this check.
    """
    mcp_context = require_mcp_context(ctx)
    definition = _PROMPT_BY_NAME[name]
    client = authenticated_client(mcp_context, definition.scopes)

    def _resolve() -> EngineCompanyModel:
        session = mcp_context.session_factory()
        try:
            try:
                return resolve_company_row(session, client, company)
            except CompanyAccessError as exc:
                raise forbidden(exc.message) from None
        finally:
            session.close()

    return await asyncio.to_thread(_resolve)


def _company_display_name(row: EngineCompanyModel) -> str:
    """Template company slot: the row's name, or its engine id when unnamed."""
    return row.company_name or row.external_company_id


async def monthly_close_review(
    company: str,
    period: str,
    trial_balance: str,
    income_statement: str,
    pending_review_items: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Monthly close review for one company and period.

    Args:
        company: Company row id or engine id.
        period: Period label, e.g. 1405-08.
        trial_balance: get_trial_balance result, as JSON.
        income_statement: get_income_statement result, as JSON.
        pending_review_items: list_pending_review_items result, as JSON.
    """
    row = await _resolve_prompt_company(ctx, name="monthly-close-review", company=company)
    return render_monthly_close_review(
        company_name=_company_display_name(row),
        period=period,
        trial_balance=_parse_tool_result("trial_balance", trial_balance) or {},
        income_statement=_parse_tool_result("income_statement", income_statement) or {},
        pending_review_items=_parse_tool_result("pending_review_items", pending_review_items),
    )


async def explain_unusual_balance_movement(
    company: str,
    account: str,
    period: str,
    general_ledger: str,
    trial_balance: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Explain an unusual balance movement for one account.

    Args:
        company: Company row id or engine id.
        account: Account code or name.
        period: Period label, e.g. 1405-08.
        general_ledger: get_general_ledger result, as JSON.
        trial_balance: get_trial_balance result, as JSON.
    """
    row = await _resolve_prompt_company(
        ctx, name="explain-unusual-balance-movement", company=company
    )
    return render_explain_unusual_balance_movement(
        company_name=_company_display_name(row),
        account=account,
        period=period,
        general_ledger=_parse_tool_result("general_ledger", general_ledger) or {},
        trial_balance=_parse_tool_result("trial_balance", trial_balance),
    )


async def summarize_overdue_receivables(
    company: str,
    as_of: str,
    receivables_aging: str,
    ctx: Context | None = None,
) -> str:
    """Summarize overdue receivables as of a date.

    Args:
        company: Company row id or engine id.
        as_of: As-of date, e.g. 1405-08-30.
        receivables_aging: get_receivables_aging result, as JSON.
    """
    row = await _resolve_prompt_company(
        ctx, name="summarize-overdue-receivables", company=company
    )
    return render_summarize_overdue_receivables(
        company_name=_company_display_name(row),
        as_of=as_of,
        receivables_aging=_parse_tool_result("receivables_aging", receivables_aging) or {},
    )


async def review_unreconciled_bank_activity(
    company: str,
    period: str,
    bank_reconciliation_status: str,
    ctx: Context | None = None,
) -> str:
    """Review unreconciled bank activity for a period.

    Args:
        company: Company row id or engine id.
        period: Period label, e.g. 1405-08.
        bank_reconciliation_status: get_bank_reconciliation_status result, as JSON.
    """
    row = await _resolve_prompt_company(
        ctx, name="review-unreconciled-bank-activity", company=company
    )
    return render_review_unreconciled_bank_activity(
        company_name=_company_display_name(row),
        period=period,
        bank_reconciliation_status=_parse_tool_result(
            "bank_reconciliation_status", bank_reconciliation_status
        )
        or {},
    )


async def validate_financial_statements(
    company: str,
    period: str,
    validation: str,
    balance_sheet: str | None = None,
    income_statement: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Validate the financial statements for a period.

    Args:
        company: Company row id or engine id.
        period: Period label, e.g. 1405-08.
        validation: validate_financial_statement result, as JSON.
        balance_sheet: get_balance_sheet result, as JSON.
        income_statement: get_income_statement result, as JSON.
    """
    row = await _resolve_prompt_company(
        ctx, name="validate-financial-statements", company=company
    )
    return render_validate_financial_statements(
        company_name=_company_display_name(row),
        period=period,
        validation=_parse_tool_result("validation", validation) or {},
        balance_sheet=_parse_tool_result("balance_sheet", balance_sheet),
        income_statement=_parse_tool_result("income_statement", income_statement),
    )


async def compare_month_over_month(
    company: str,
    current_period: str,
    prior_period: str,
    current_income_statement: str,
    prior_income_statement: str,
    current_balance_sheet: str | None = None,
    prior_balance_sheet: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Compare month-over-month performance: current vs prior period.

    Args:
        company: Company row id or engine id.
        current_period: Current period, e.g. 1405-08.
        prior_period: Prior period, e.g. 1405-07.
        current_income_statement: get_income_statement result, current period, as JSON.
        prior_income_statement: get_income_statement result, prior period, as JSON.
        current_balance_sheet: get_balance_sheet result, current period, as JSON.
        prior_balance_sheet: get_balance_sheet result, prior period, as JSON.
    """
    row = await _resolve_prompt_company(ctx, name="compare-month-over-month", company=company)
    return render_compare_month_over_month(
        company_name=_company_display_name(row),
        current_period=current_period,
        prior_period=prior_period,
        current_income_statement=_parse_tool_result(
            "current_income_statement", current_income_statement
        )
        or {},
        prior_income_statement=_parse_tool_result("prior_income_statement", prior_income_statement)
        or {},
        current_balance_sheet=_parse_tool_result("current_balance_sheet", current_balance_sheet),
        prior_balance_sheet=_parse_tool_result("prior_balance_sheet", prior_balance_sheet),
    )


_HANDLERS: dict[str, Callable[..., Any]] = {
    "monthly-close-review": monthly_close_review,
    "explain-unusual-balance-movement": explain_unusual_balance_movement,
    "summarize-overdue-receivables": summarize_overdue_receivables,
    "review-unreconciled-bank-activity": review_unreconciled_bank_activity,
    "validate-financial-statements": validate_financial_statements,
    "compare-month-over-month": compare_month_over_month,
}


# -- registration ---------------------------------------------------------------

def register_hesabyar_prompt(
    server: MCPServer[Any],
    registry: ToolRegistry,
    fn: Callable[..., Any],
    *,
    name: str,
    scopes: Iterable[str],
    description: str,
) -> None:
    """Register a prompt on ``server`` AND record its scopes in ``registry``.

    The two registrations are one operation on purpose: a prompt that
    is renderable but has no scope record would bypass the
    middleware's scope check (the middleware matches prompts by exact
    name, so the record is enforced centrally for prompts).
    """
    registry.register_prompt(name, scopes)
    server.prompt(name=name, description=description)(fn)


def register_prompts(server: MCPServer[Any], context: McpContext) -> None:
    """Register all six US-604 prompts on ``server``."""
    for definition in PROMPT_DEFINITIONS:
        register_hesabyar_prompt(
            server,
            context.registry,
            _HANDLERS[definition.name],
            name=definition.name,
            scopes=definition.scopes,
            description=definition.description,
        )
