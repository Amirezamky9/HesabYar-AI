"""Shared fixtures for the standards registry tests (US-401).

``BALANCED_DATA`` is a complete, internally consistent financial
document — per-statement namespaces plus the cross-statement links —
against which every executable rule of the ported ruleset PASSES.
Each figure was solved from the rules themselves (see
``test_standards_seed.py::test_golden_data_passes_all_executable_rules``);
it is the golden dataset for the DSL port, the validation service and
the REST endpoints.

Statement figures (IRR, integers throughout):

- balance sheet: assets 1000 = liabilities 600 + equity 400.
- income statement: revenue 500, gross profit 360, operating profit
  320, profit before tax 310, net profit 250.
- comprehensive income: net profit 250 + OCI 50 = total 300.
- equity changes: opening 90 + adjustments 10 = 100 adjusted;
  100 + 300 + 0 = 400 closing equity.
- cash flow: operating 100, investing -20, financing 10, net change
  90; beginning 60 + 90 = ending 150 = balance-sheet cash.
"""

from __future__ import annotations

from typing import Any

BALANCED_DATA: dict[str, Any] = {
    "balance_sheet": {
        "total_assets": 1000,
        "total_liabilities": 600,
        "total_equity": 400,
        "total_current_assets": 400,
        "total_non_current_assets": 600,
        "total_current_liabilities": 250,
        "total_non_current_liabilities": 350,
        "current_assets_items": [{"amount": 150}, {"amount": 250}],
        "non_current_assets_items": [{"amount": 600}],
        "current_liabilities_items": [{"amount": 250}],
        "non_current_liabilities_items": [{"amount": 350}],
        "equity_items": [{"amount": 400}],
        "cash_and_equivalents": 150,
    },
    "income_statement": {
        "raw_materials_consumed": 100,
        "employee_benefits": 50,
        "depreciation": 20,
        "other_expenses": 30,
        "changes_in_inventory": 10,
        "total_expenses": 190,
        "revenue": 500,
        "other_income": 10,
        "operating_profit": 320,
        "cost_of_goods_sold": 140,
        "gross_profit": 360,
        "selling_expenses": 12,
        "administrative_expenses": 8,
        "finance_costs": 15,
        "share_of_associates": 5,
        "profit_before_tax": 310,
        "tax_expense": 60,
        "discontinued_operations": 0,
        "net_profit": 250,
        "owners_of_parent": 200,
        "non_controlling_interests": 50,
    },
    "comprehensive_income": {
        "net_profit": 250,
        "subtotal_not_reclassified": 30,
        "subtotal_reclassified": 20,
        "total_comprehensive_income": 300,
        "owners_of_parent": 240,
        "non_controlling_interests": 60,
        "reclassification_adjustments": 0,
        "other_comprehensive_income": 50,
    },
    "equity_changes": {
        "adjusted_opening_balance": 100,
        "total_comprehensive_income": 300,
        "total_owner_transactions": 0,
        "closing_balance": {"total_equity": 400},
        "opening_balance": 90,
        "retrospective_application": 5,
        "retrospective_restatement": 5,
        "net_profit": 250,
        "other_comprehensive_income": 50,
        "capital_contributions": 10,
        "distributions_to_owners": 10,
        "changes_in_ownership": 0,
    },
    "cash_flow": {
        "net_cash_from_operating": 100,
        "net_cash_from_investing": -20,
        "net_cash_from_financing": 10,
        "net_change_in_cash": 90,
        "beginning_cash": 60,
        "effect_of_exchange_rate": 0,
        "ending_cash": 150,
        "profit_before_tax": 310,
        "depreciation": 20,
        "amortization": 5,
        "provisions": 3,
        "impairment_losses": 2,
        "foreign_exchange_gains_losses": 1,
        "gains_losses_on_disposal": -1,
        "change_in_receivables": -10,
        "change_in_inventory": -5,
        "change_in_payables": 8,
        "change_in_other_working_capital": 2,
        "income_tax_paid": 60,
        "other_adjustments": -175,
    },
}
