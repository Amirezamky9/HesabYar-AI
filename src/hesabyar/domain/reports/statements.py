"""Pure financial-statement builders (Phase 5-6, US-550).

These functions turn engine-read figures into statement sections. They
are pure (no engine access, no I/O): the adapter reads the figures, the
builders group, sign-normalize and total them, and the service stamps
freshness on the result. Unit tests exercise the builders directly with
hand-built figures — no Tryton needed.

Sign conventions
---------------
The engine's ``account.account`` balance Function field returns
debit-minus-credit. Statements present positive magnitudes per section:

- balance sheet: an account under Assets displays ``+balance`` (a debit
  balance is a positive asset); under Liabilities/Equity it displays
  ``-balance`` (a credit balance is a positive liability). The
  accounting identity is total assets == total liabilities + total
  equity, with the current-period result (the income accounts' net)
  carried inside equity as its own line — the profit/loss type sits
  under equity in the universal chart, so a balanced ledger always
  balances here.
- income statement: an income account's type is classified REVENUE when
  the type's net balance over the period is a net credit (balance < 0)
  and EXPENSE when it is a net debit (balance > 0). Revenue displays
  ``-balance``, expense ``+balance``; net income is revenue minus
  expense. The classification is data-driven (never by name), so it
  works on any chart, and it is fixed by the WHOLE period — the monthly
  breakdown reuses it, so monthly figures always sum to the statement
  totals.

Why account balances, not the type ``amount`` Function field: the
type-statement reference report (``account.account.type.statement``)
renders ``type.amount`` — but the universal chart's ``assets`` flag,
which flips the sign of ``amount``, is set inconsistently (set on
accounts-receivable, inventories and the non-current-asset types, but
NOT on cash, petty cash, current assets or the assets root), so raw
``amount`` values are sign-inconsistent across the tree. Account
balances have one convention (debit minus credit) and are the same
figures the type statement aggregates — reading them and grouping by
type gives the reference report's figures with a consistent sign rule.
See docs/financial-reports.md.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Sequence
from dataclasses import dataclass

import jdatetime

from hesabyar.domain.accounting.value_objects import (
    MoneyIRR,
    StatementLineView,
    StatementSectionView,
)
from hesabyar.domain.common.dates import FiscalDate

#: Version of every US-550 report definition (§11.1 report version).
REPORT_VERSION = "1.0.0"

#: Calculation versions (§11.1 calculation version), per report.
CALCULATION_VERSIONS = {
    "trial_balance": "tryton-posted-lines/1.0",
    "general_ledger": "tryton-posted-lines/1.0",
    "balance_sheet": "tryton-account-balance/1.0",
    "income_statement": "tryton-account-balance/1.0",
    # Tryton has no cash-flow reference report: the direct-method
    # derivation below, versioned so a rule change is visible.
    "cash_flow": "direct-method/1.0",
    # The aged-balance rule (maturity date, open lines only), applied
    # to the notes accounts — see aging_bucket().
    "notes_balance": "tryton-aged-balance-rule/1.0",
    # The cash forecast (US-553): registered records only —
    # settlement tranches, installment legs, cheques (report 14
    # rule), posted invoices, packaging purchase requests.
    "cash_forecast": "registered-records/1.0",
    # Cheque report 12 (US-553): the trend renders from the stored
    # monthly match history, never recomputed at read time.
    "monthly_match_trend": "stored-history/1.0",
}

#: Engine version recorded in reproducibility metadata.
ENGINE_VERSION = "8.2.x"

#: Universal-chart account-type names that identify cash accounts
#: (``account_type_template_cash_en`` / ``_petty_cash_en``). Bank
#: accounts use the cash type; the gateway-clearing account (1.1.6)
#: and petty cash (1.1.5) are cash too. Investments (1.1.2/1.1.4) use
#: other types and are NOT cash.
CASH_TYPE_NAMES = frozenset(
    {
        "Cash and Cash Equivalents",
        "Cash at bank, Petty Cash, Cash On Hand",
    }
)

#: Notes receivable accounts (HesabYar chart extension, US-502).
NOTES_RECEIVABLE_CODES = ("1.2.4", "1.2.5")

#: Notes payable account (HesabYar chart extension, US-502).
NOTES_PAYABLE_CODES = ("2.1.5",)

#: All notes accounts, in code order.
NOTES_ACCOUNT_CODES = NOTES_RECEIVABLE_CODES + NOTES_PAYABLE_CODES

#: Universal-chart section names -> stable section keys (the UI maps
#: keys to Persian labels; an unknown top-level type falls back to a
#: slug of its name).
_BALANCE_SECTION_KEYS = {
    "Assets": "assets",
    "Liabilities": "liabilities",
    "Equity / capital": "equity",
}


@dataclass(frozen=True)
class AccountTypeNode:
    """One ``account.account.type`` row: the tree the statements group by."""

    type_id: int
    name: str
    parent_id: int | None
    statement: str | None  # 'balance' | 'income' | 'off-balance' | None
    sequence: int


@dataclass(frozen=True)
class AccountBalanceRow:
    """One account's engine-computed balance (debit minus credit).

    ``balance`` is the figure for the report's period (a point in
    time for the balance sheet, a date range for the P&L);
    ``balance_cmp`` the same figure for the comparison period (None
    when no comparison was requested).
    """

    account_id: int
    code: str
    name: str
    type_id: int | None
    balance: MoneyIRR
    balance_cmp: MoneyIRR | None = None


@dataclass(frozen=True)
class BalanceSheetSections:
    """The balance sheet's sections, totals and balance checks."""

    sections: tuple[StatementSectionView, ...]
    total_assets: MoneyIRR
    total_liabilities: MoneyIRR
    total_equity: MoneyIRR
    total_assets_cmp: MoneyIRR | None
    total_liabilities_cmp: MoneyIRR | None
    total_equity_cmp: MoneyIRR | None
    balanced: bool
    balanced_cmp: bool | None


@dataclass(frozen=True)
class IncomeStatementSections:
    """The P&L's sections and totals."""

    sections: tuple[StatementSectionView, ...]
    total_revenue: MoneyIRR
    total_expense: MoneyIRR
    net_income: MoneyIRR
    total_revenue_cmp: MoneyIRR | None
    total_expense_cmp: MoneyIRR | None
    net_income_cmp: MoneyIRR | None


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "section"


def _children_by_parent(
    types: Sequence[AccountTypeNode],
) -> dict[int | None, list[AccountTypeNode]]:
    children: dict[int | None, list[AccountTypeNode]] = {}
    for node in types:
        children.setdefault(node.parent_id, []).append(node)
    for siblings in children.values():
        siblings.sort(key=lambda node: (node.sequence, node.name))
    return children


def _descendant_type_ids(
    root_id: int, children: dict[int | None, list[AccountTypeNode]]
) -> set[int]:
    found: set[int] = set()
    stack = [root_id]
    while stack:
        current = stack.pop()
        if current in found:
            continue
        found.add(current)
        stack.extend(child.type_id for child in children.get(current, []))
    return found


def _ancestors(
    type_id: int, by_id: dict[int, AccountTypeNode]
) -> set[int]:
    """All ancestor type ids of a type (excluding the type itself)."""
    found: set[int] = set()
    current = by_id.get(type_id)
    while current is not None and current.parent_id is not None:
        if current.parent_id in found:
            break
        found.add(current.parent_id)
        current = by_id.get(current.parent_id)
    return found


def _kept(amount: MoneyIRR, amount_cmp: MoneyIRR | None) -> bool:
    """A row is kept when either its figure or its comparison is non-zero."""
    if not amount.is_zero:
        return True
    return amount_cmp is not None and not amount_cmp.is_zero


def _flatten_type_subtree(
    *,
    type_node: AccountTypeNode,
    depth: int,
    children: dict[int | None, list[AccountTypeNode]],
    accounts_by_type: dict[int, list[AccountBalanceRow]],
    display: dict[int, MoneyIRR],
    display_cmp: dict[int, MoneyIRR | None],
) -> list[StatementLineView]:
    """Depth-first rows for one type's subtree (type subtotals + accounts).

    A type row is kept when its subtree holds at least one kept
    account row; an account row is kept when its figure (or its
    comparison) is non-zero. Zero-balance accounts are omitted, like
    the trial balance omits them.
    """
    rows: list[StatementLineView] = []

    def subtree_accounts(type_id: int) -> list[AccountBalanceRow]:
        found = list(accounts_by_type.get(type_id, []))
        for child in children.get(type_id, []):
            found.extend(subtree_accounts(child.type_id))
        return found

    def build(node: AccountTypeNode, node_depth: int) -> None:
        # Only accounts the caller put in ``display`` belong to this
        # section: the equity subtree holds the income types too
        # (profit/loss sits under Equity), and a P&L section's
        # subtree can hold a type classified into the other section.
        subtree = [
            row for row in subtree_accounts(node.type_id) if row.account_id in display
        ]
        if not any(
            _kept(display[row.account_id], display_cmp[row.account_id])
            for row in subtree
        ):
            return
        total = MoneyIRR(
            sum(display[row.account_id].amount for row in subtree)
        )
        total_cmp: MoneyIRR | None = None
        if any(display_cmp[row.account_id] is not None for row in subtree):
            total_cmp = MoneyIRR(
                sum(
                    (display_cmp[row.account_id] or MoneyIRR(0)).amount
                    for row in subtree
                )
            )
        rows.append(
            StatementLineView(
                kind="type",
                ref_id=node.type_id,
                label=node.name,
                code=None,
                depth=node_depth,
                amount=total,
                amount_cmp=total_cmp,
            )
        )
        for child in children.get(node.type_id, []):
            build(child, node_depth + 1)
        for row in sorted(subtree, key=lambda r: r.code):
            if row.type_id != node.type_id:
                continue
            if not _kept(display[row.account_id], display_cmp[row.account_id]):
                continue
            rows.append(
                StatementLineView(
                    kind="account",
                    ref_id=row.account_id,
                    label=row.name,
                    code=row.code,
                    depth=node_depth + 1,
                    amount=display[row.account_id],
                    amount_cmp=display_cmp[row.account_id],
                )
            )

    build(type_node, depth)
    return rows


def build_balance_sheet_sections(
    types: Sequence[AccountTypeNode],
    accounts: Sequence[AccountBalanceRow],
) -> BalanceSheetSections:
    """Build balance-sheet sections from account balances at a date.

    Sections are the top-level balance types (children of the
    statement-less root: Assets, Liabilities, Equity). Accounts are
    grouped under their leaf type; the current-period result (the
    income accounts' net) is added as a line inside equity, so the
    sheet balances whenever the ledger does. Off-balance (7.x
    memorandum) accounts are excluded — they net to zero by design
    and are not part of the balance sheet.
    """
    by_id = {node.type_id: node for node in types}
    children = _children_by_parent(types)
    accounts_by_type: dict[int, list[AccountBalanceRow]] = {}
    for row in accounts:
        if row.type_id is not None:
            accounts_by_type.setdefault(row.type_id, []).append(row)

    def is_top_level_balance(node: AccountTypeNode) -> bool:
        if node.statement != "balance":
            return False
        if node.parent_id is None:
            return True
        parent = by_id.get(node.parent_id)
        return parent is not None and parent.statement is None

    sections: list[StatementSectionView] = []
    totals: dict[str, MoneyIRR] = {}
    totals_cmp: dict[str, MoneyIRR | None] = {}

    # The current-period result: minus the income accounts' net
    # balance (positive = profit). It lands inside equity.
    income_rows = [
        row
        for row in accounts
        if row.type_id is not None
        and by_id.get(row.type_id) is not None
        and by_id[row.type_id].statement == "income"
    ]
    net_income = MoneyIRR(-sum(row.balance.amount for row in income_rows))
    net_income_cmp: MoneyIRR | None = None
    if any(row.balance_cmp is not None for row in income_rows):
        net_income_cmp = MoneyIRR(
            -sum((row.balance_cmp or MoneyIRR(0)).amount for row in income_rows)
        )

    for top in sorted(
        (node for node in types if is_top_level_balance(node)),
        key=lambda node: (node.sequence, node.name),
    ):
        key = _BALANCE_SECTION_KEYS.get(top.name, _slug(top.name))
        subtree_type_ids = _descendant_type_ids(top.type_id, children)
        section_accounts = [
            row
            for row in accounts
            if row.type_id in subtree_type_ids
            and by_id.get(row.type_id) is not None
            and by_id[row.type_id].statement == "balance"
        ]
        # Display sign: assets show +balance, liabilities/equity -balance.
        sign = 1 if key == "assets" else -1
        display = {
            row.account_id: MoneyIRR(sign * row.balance.amount)
            for row in section_accounts
        }
        display_cmp = {
            row.account_id: (
                MoneyIRR(sign * row.balance_cmp.amount)
                if row.balance_cmp is not None
                else None
            )
            for row in section_accounts
        }
        lines = _flatten_type_subtree(
            type_node=top,
            depth=0,
            children=children,
            accounts_by_type=accounts_by_type,
            display=display,
            display_cmp=display_cmp,
        )
        total = MoneyIRR(sum(value.amount for value in display.values()))
        total_cmp: MoneyIRR | None = None
        if any(value is not None for value in display_cmp.values()):
            total_cmp = MoneyIRR(
                sum((value or MoneyIRR(0)).amount for value in display_cmp.values())
            )
        if key == "equity":
            # The current-period result is an equity line (the
            # profit/loss type lives under equity in the chart).
            profit_label = "Current Period Profit or Lost"
            profit_ref = top.type_id
            for node in types:
                if node.name == profit_label:
                    profit_ref = node.type_id
                    break
            if _kept(net_income, net_income_cmp):
                lines.append(
                    StatementLineView(
                        kind="type",
                        ref_id=profit_ref,
                        label=profit_label,
                        code=None,
                        depth=1,
                        amount=net_income,
                        amount_cmp=net_income_cmp,
                    )
                )
            total = MoneyIRR(total.amount + net_income.amount)
            if total_cmp is not None and net_income_cmp is not None:
                total_cmp = MoneyIRR(total_cmp.amount + net_income_cmp.amount)
        sections.append(
            StatementSectionView(
                key=key,
                title=top.name,
                lines=tuple(lines),
                total=total,
                total_cmp=total_cmp,
            )
        )
        totals[key] = total
        totals_cmp[key] = total_cmp

    total_assets = totals.get("assets", MoneyIRR(0))
    total_liabilities = totals.get("liabilities", MoneyIRR(0))
    total_equity = totals.get("equity", MoneyIRR(0))
    balanced = total_assets.amount == total_liabilities.amount + total_equity.amount
    total_assets_cmp = totals_cmp.get("assets")
    total_liabilities_cmp = totals_cmp.get("liabilities")
    total_equity_cmp = totals_cmp.get("equity")
    balanced_cmp: bool | None = None
    if (
        total_assets_cmp is not None
        and total_liabilities_cmp is not None
        and total_equity_cmp is not None
    ):
        balanced_cmp = (
            total_assets_cmp.amount
            == total_liabilities_cmp.amount + total_equity_cmp.amount
        )
    return BalanceSheetSections(
        sections=tuple(sections),
        total_assets=total_assets,
        total_liabilities=total_liabilities,
        total_equity=total_equity,
        total_assets_cmp=total_assets_cmp,
        total_liabilities_cmp=total_liabilities_cmp,
        total_equity_cmp=total_equity_cmp,
        balanced=balanced,
        balanced_cmp=balanced_cmp,
    )


def classify_income_types(
    types: Sequence[AccountTypeNode],
    accounts: Sequence[AccountBalanceRow],
) -> dict[int, str]:
    """Classify each income type as revenue or expense for a period.

    By the type's net balance over the period: a net credit (negative
    balance) is revenue, a net debit (positive balance) is expense, a
    zero net is neither. The P&L sections and the monthly breakdown
    share this classification, so the monthly figures always sum to
    the statement totals.
    """
    by_id = {node.type_id: node for node in types}
    net: dict[int, int] = {}
    for row in accounts:
        if row.type_id is None:
            continue
        node = by_id.get(row.type_id)
        if node is None or node.statement != "income":
            continue
        net[row.type_id] = net.get(row.type_id, 0) + row.balance.amount
    classification: dict[int, str] = {}
    for type_id, amount in net.items():
        if amount < 0:
            classification[type_id] = "revenue"
        elif amount > 0:
            classification[type_id] = "expense"
    return classification


def build_income_statement_sections(
    types: Sequence[AccountTypeNode],
    accounts: Sequence[AccountBalanceRow],
) -> IncomeStatementSections:
    """Build P&L sections (revenue / expenses) from period balances.

    Income types are classified by their net sign over the period
    (see :func:`classify_income_types`); each section groups its
    types' accounts under their leaf type. Revenue displays
    ``-balance`` and expense ``+balance`` — both positive magnitudes.
    """
    by_id = {node.type_id: node for node in types}
    children = _children_by_parent(types)
    accounts_by_type: dict[int, list[AccountBalanceRow]] = {}
    for row in accounts:
        if row.type_id is not None:
            accounts_by_type.setdefault(row.type_id, []).append(row)
    classification = classify_income_types(types, accounts)

    sections: list[StatementSectionView] = []
    totals: dict[str, MoneyIRR] = {}
    totals_cmp: dict[str, MoneyIRR | None] = {}
    # NOTE: the section key is "expenses" but classify_income_types
    # returns the singular kind "expense" — the filter must use the
    # kind, not the key (filtering on "expenses" matched nothing and
    # the expenses section was always empty).
    for key, title, kind_filter in (
        ("revenue", "Revenue", "revenue"),
        ("expenses", "Expenses", "expense"),
    ):
        type_ids = {
            type_id
            for type_id, kind in classification.items()
            if kind == kind_filter
        }
        section_accounts = [
            row for row in accounts if row.type_id in type_ids
        ]
        sign = -1 if key == "revenue" else 1
        display = {
            row.account_id: MoneyIRR(sign * row.balance.amount)
            for row in section_accounts
        }
        display_cmp = {
            row.account_id: (
                MoneyIRR(sign * row.balance_cmp.amount)
                if row.balance_cmp is not None
                else None
            )
            for row in section_accounts
        }
        lines: list[StatementLineView] = []
        # One subtree per section-root type (a type whose ancestors
        # hold no other type of this section), ordered by the type's
        # sequence then name — building a subtree for both a type and
        # its child would double-count the child's rows.
        roots = {
            type_id
            for type_id in type_ids
            if _ancestors(type_id, by_id).isdisjoint(type_ids)
        }
        for node in sorted(
            (by_id[type_id] for type_id in roots),
            key=lambda n: (n.sequence, n.name),
        ):
            lines.extend(
                _flatten_type_subtree(
                    type_node=node,
                    depth=0,
                    children=children,
                    accounts_by_type=accounts_by_type,
                    display=display,
                    display_cmp=display_cmp,
                )
            )
        total = MoneyIRR(sum(value.amount for value in display.values()))
        total_cmp: MoneyIRR | None = None
        if any(value is not None for value in display_cmp.values()):
            total_cmp = MoneyIRR(
                sum((value or MoneyIRR(0)).amount for value in display_cmp.values())
            )
        sections.append(
            StatementSectionView(
                key=key,
                title=title,
                lines=tuple(lines),
                total=total,
                total_cmp=total_cmp,
            )
        )
        totals[key] = total
        totals_cmp[key] = total_cmp

    total_revenue = totals.get("revenue", MoneyIRR(0))
    total_expense = totals.get("expenses", MoneyIRR(0))
    net_income = MoneyIRR(total_revenue.amount - total_expense.amount)
    total_revenue_cmp = totals_cmp.get("revenue")
    total_expense_cmp = totals_cmp.get("expenses")
    net_income_cmp: MoneyIRR | None = None
    if total_revenue_cmp is not None and total_expense_cmp is not None:
        net_income_cmp = MoneyIRR(
            total_revenue_cmp.amount - total_expense_cmp.amount
        )
    return IncomeStatementSections(
        sections=tuple(sections),
        total_revenue=total_revenue,
        total_expense=total_expense,
        net_income=net_income,
        total_revenue_cmp=total_revenue_cmp,
        total_expense_cmp=total_expense_cmp,
        net_income_cmp=net_income_cmp,
    )


# ---------------------------------------------------------------------------
# Cash flow: direct-method section classification (calculation v1.0)
# ---------------------------------------------------------------------------
#
# Tryton has no cash-flow reference report, so the sections are a
# documented HesabYar derivation from the engine's posted cash moves:
# each cash line is classified by its LARGEST non-cash counterpart
# line's account code (most moves have a single counterpart; the
# largest-counterpart rule is deterministic for split moves).
#
# - investing: fixed assets / intangibles / accumulated depreciation
#   (1.5.x–1.9.x) and the investment accounts (1.1.2, 1.1.4);
# - financing: financial liabilities / loans (2.3.x) and equity (3.x);
# - transfer: another cash account (1.1.x) — excluded from every
#   section, it moves no cash in or out;
# - operating: everything else that touches cash in the normal course
#   of business (receivables, payables, inventory, accruals, income,
#   expenses, taxes);
# - unclassified: memorandum (7.x) accounts and unknown codes — counted
#   and reported, never silently dropped; they break the tie-out.


def classify_cash_flow_section(counterpart_code: str) -> str:
    """Classify one cash line by its largest counterpart's account code.

    Returns ``"operating"``, ``"investing"``, ``"financing"``,
    ``"transfer"`` (cash-to-cash, excluded from every section) or
    ``"unclassified"`` (memorandum / unknown codes).
    """
    code = counterpart_code.strip()
    if code in {"1.1.2", "1.1.4"}:
        return "investing"
    if code.startswith("1.1."):
        return "transfer"
    if code.startswith(("1.5.", "1.6.", "1.7.", "1.8.", "1.9.")):
        return "investing"
    if code.startswith("2.3.") or code.startswith("3."):
        return "financing"
    if code.startswith("7."):
        return "unclassified"
    if code and code[0] in "12456":
        return "operating"
    return "unclassified"


# ---------------------------------------------------------------------------
# Notes aging: the Tryton aged-balance rule (``account.aged_balance``)
# ---------------------------------------------------------------------------


def aging_bucket(age_days: int) -> str:
    """Bucket a notes line by its age in days at the as-of date.

    Age is as-of minus the line date (maturity date, falling back to
    the move date — the aged-balance model's ``Coalesce``). Tryton's
    model puts lines maturing after the as-of date in the balance but
    in no term; here they get the ``"not_yet_due"`` bucket so the
    buckets always sum to the row total.
    """
    if age_days < 0:
        return "not_yet_due"
    if age_days < 30:
        return "bucket_0_30"
    if age_days < 60:
        return "bucket_30_60"
    if age_days < 90:
        return "bucket_60_90"
    if age_days < 120:
        return "bucket_90_120"
    return "bucket_120_plus"


#: Bucket keys in display order (not-yet-due first, oldest last).
AGING_BUCKETS = (
    "not_yet_due",
    "bucket_0_30",
    "bucket_30_60",
    "bucket_60_90",
    "bucket_90_120",
    "bucket_120_plus",
)


# ---------------------------------------------------------------------------
# Jalali months (P&L monthly breakdown)
# ---------------------------------------------------------------------------


def jalali_months_in_range(
    date_from: FiscalDate, date_to: FiscalDate
) -> list[tuple[str, FiscalDate, FiscalDate]]:
    """Jalali months overlapping [date_from, date_to], clipped to the range.

    Each entry is ``(jalali_month, month_start, month_end)`` with the
    label ``"YYYY-MM"`` in Jalali (e.g. ``"1405-08"``) and Gregorian
    FiscalDate bounds. A range inside one Jalali month yields one
    (clipped) entry; an empty range yields none.
    """
    if date_to < date_from:
        return []
    start = date_from.jalali_date
    end = date_to.jalali_date
    months: list[tuple[str, FiscalDate, FiscalDate]] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        first = jdatetime.date(year, month, 1)
        last_day = jdatetime.date(year, month, 1) + datetime.timedelta(
            days=_jalali_month_length(year, month) - 1
        )
        month_start = FiscalDate(max(first.togregorian(), date_from.gregorian_date))
        month_end = FiscalDate(min(last_day.togregorian(), date_to.gregorian_date))
        months.append((f"{year:04d}-{month:02d}", month_start, month_end))
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return months


def _jalali_month_length(year: int, month: int) -> int:
    """Days in a Jalali month (months 1–6: 31, 7–11: 30, 12: 29/30)."""
    if month <= 6:
        return 31
    if month <= 11:
        return 30
    return 30 if jdatetime.date(year, 1, 1).isleap() else 29
