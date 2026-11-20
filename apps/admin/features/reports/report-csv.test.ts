import { describe, expect, it } from "vitest";
import {
  balanceSheetToCsv,
  cashFlowToCsv,
  generalLedgerDetailToCsv,
  generalLedgerSummaryToCsv,
  incomeStatementToCsv,
  invoicePartySummaryToCsv,
  invoiceStateSummaryToCsv,
  notesBalanceToCsv,
  paymentKindSummaryToCsv,
  paymentStateSummaryToCsv,
  trialBalanceToCsv,
} from "./report-csv";
import { statementSectionLabel } from "./financial-report-data";
import type {
  BalanceSheetResponse,
  CashFlowResponse,
  IncomeStatementResponse,
  NotesBalanceResponse,
  TrialBalanceResponse,
} from "@/lib/api-client";

describe("report CSV exports", () => {
  it("invoice-by-state CSV has Persian headers and normalized values", () => {
    const csv = invoiceStateSummaryToCsv([
      { state: "draft", count: 2, untaxedIrr: 300, taxIrr: 30, totalIrr: 330 },
      { state: "posted", count: 1, untaxedIrr: 100, taxIrr: 10, totalIrr: 110 },
    ]);
    expect(csv).toBe(
      "\uFEFFوضعیت,تعداد فاکتور,مبلغ بدون مالیات (ریال),مالیات (ریال),مبلغ کل (ریال)\r\n" +
        "draft,2,300,30,330\r\n" +
        "posted,1,100,10,110\r\n",
    );
  });

  it("invoice-by-party CSV quotes names containing a comma", () => {
    const csv = invoicePartySummaryToCsv([
      { partyId: 12, partyName: "شرکت الف, با مسئولیت محدود", count: 1, totalIrr: 500 },
    ]);
    expect(csv).toContain('"شرکت الف, با مسئولیت محدود",1,500');
  });

  it("payment-by-state CSV exports normalized state codes", () => {
    const csv = paymentStateSummaryToCsv([{ state: "succeeded", count: 3, totalIrr: 900 }]);
    expect(csv).toBe("\uFEFFوضعیت,تعداد پرداخت,مبلغ کل (ریال)\r\nsucceeded,3,900\r\n");
  });

  it("payment-by-kind CSV exports normalized kind codes", () => {
    const csv = paymentKindSummaryToCsv([
      { kind: "receivable", count: 1, totalIrr: 200 },
      { kind: "payable", count: 2, totalIrr: 400 },
    ]);
    expect(csv).toBe(
      "\uFEFFنوع,تعداد پرداخت,مبلغ کل (ریال)\r\nreceivable,1,200\r\npayable,2,400\r\n",
    );
  });

  it("empty summaries still export the header row", () => {
    expect(invoiceStateSummaryToCsv([])).toBe(
      "\uFEFFوضعیت,تعداد فاکتور,مبلغ بدون مالیات (ریال),مالیات (ریال),مبلغ کل (ریال)\r\n",
    );
  });
});

describe("financial report CSV exports (US-550)", () => {
  const freshness = {
    source: "tryton:account.move.line (Tryton REST)",
    source_timestamp: "2026-11-20T00:00:00Z",
    generated_at: "2026-11-20T00:00:00Z",
    company_id: "1",
    period_start: "2026-11-01",
    period_end: "2026-11-30",
    report_version: "1.0.0",
    calculation_version: "tryton-posted-lines/1.0",
    freshness_indicator: "live",
    reproducibility: {},
  };
  const filters: [string, string][] = [
    ["شرکت", "اکمه"],
    ["دوره", "2026-11-01 تا 2026-11-30"],
  ];

  it("trial balance CSV carries the filter header above the table", () => {
    const response = {
      company_id: "1",
      date_from: "2026-11-01",
      date_to: "2026-11-30",
      as_of_date: "2026-11-30",
      lines: [{ account_id: 1, code: "1.1.1", name: "نقد", total_debit: 100, total_credit: 40, balance: 60 }],
      total_debit: 100,
      total_credit: 40,
      balanced: false,
      move_count: 1,
      line_count: 1,
      source_endpoint: "account.move.line (Tryton REST)",
      freshness,
    } as TrialBalanceResponse;
    expect(trialBalanceToCsv(response, filters)).toBe(
      "\uFEFF# تراز آزمایشی\r\n# شرکت: اکمه\r\n# دوره: 2026-11-01 تا 2026-11-30\r\n" +
        "کد حساب,نام حساب,بدهکار (ریال),بستانکار (ریال),مانده (ریال)\r\n" +
        "1.1.1,نقد,100,40,60\r\n",
    );
  });

  it("general ledger summary CSV lists accounts with opening and closing balances", () => {
    const csv = generalLedgerSummaryToCsv(
      {
        company_id: "1",
        date_from: "2026-11-01",
        date_to: "2026-11-30",
        accounts: [
          {
            account_id: 1,
            code: "1.1.1",
            name: "نقد",
            opening_balance: 10,
            total_debit: 100,
            total_credit: 40,
            closing_balance: 70,
            lines: [],
          },
        ],
        total_debit: 100,
        total_credit: 40,
        move_count: 0,
        line_count: 0,
        source_endpoint: "account.move.line (Tryton REST)",
        freshness,
      },
      filters,
    );
    expect(csv).toContain("# دفتر کل (خلاصهٔ حساب‌ها)\r\n");
    expect(csv).toContain("1.1.1,نقد,10,100,40,70\r\n");
  });

  it("general ledger detail CSV adds the period to the filter header", () => {
    const csv = generalLedgerDetailToCsv(
      {
        account_id: 1,
        code: "1.1.1",
        name: "نقد",
        opening_balance: 10,
        total_debit: 100,
        total_credit: 40,
        closing_balance: 70,
        lines: [
          { move_id: 5, move_number: "12", date: "2026-11-17", description: "فروش", debit: 100, credit: 0, balance: 110 },
        ],
      },
      "2026-11-01",
      "2026-11-30",
      [["شرکت", "اکمه"]],
    );
    expect(csv).toContain("# دفتر کل — 1.1.1 نقد\r\n");
    expect(csv).toContain("# دوره: 2026-11-01 تا 2026-11-30\r\n");
    expect(csv).toContain("2026-11-17,12,فروش,100,0,110\r\n");
  });

  it("balance sheet CSV flattens sections with comparison amounts", () => {
    const response = {
      company_id: "1",
      as_of: "2026-11-30",
      comparison_as_of: "2025-11-30",
      sections: [
        {
          key: "assets",
          title: "Assets",
          lines: [
            { kind: "type", ref_id: 2, label: "Assets", code: null, depth: 0, amount: 150, amount_cmp: 140 },
            { kind: "account", ref_id: 1, label: "نقد", code: "1.1.1", depth: 1, amount: 150, amount_cmp: 140 },
          ],
          total: 150,
          total_cmp: 140,
        },
      ],
      total_assets: 150,
      total_liabilities: 0,
      total_equity: 150,
      total_assets_cmp: 140,
      total_liabilities_cmp: 0,
      total_equity_cmp: 140,
      balanced: true,
      balanced_cmp: true,
      source_endpoint: "account.account (Tryton REST)",
      freshness,
    } as BalanceSheetResponse;
    const csv = balanceSheetToCsv(response, statementSectionLabel, filters);
    expect(csv).toContain("# ترازنامه\r\n");
    expect(csv).toContain("دارایی‌ها,Assets,150,140\r\n");
    expect(csv).toContain("دارایی‌ها,  1.1.1 نقد,150,140\r\n");
    expect(csv).toContain("دارایی‌ها,جمع بخش,150,140\r\n");
  });

  it("income statement CSV appends the monthly breakdown block", () => {
    const response = {
      company_id: "1",
      date_from: "2026-11-01",
      date_to: "2026-11-30",
      comparison_from: "2025-11-01",
      comparison_to: "2025-11-30",
      sections: [
        {
          key: "revenue",
          title: "Revenue",
          lines: [{ kind: "type", ref_id: 10, label: "Revenue", code: null, depth: 0, amount: 600, amount_cmp: 500 }],
          total: 600,
          total_cmp: 500,
        },
      ],
      total_revenue: 600,
      total_expense: 0,
      net_income: 600,
      total_revenue_cmp: 500,
      total_expense_cmp: 0,
      net_income_cmp: 500,
      monthly: [{ jalali_month: "1405-08", month_start: "2026-10-23", month_end: "2026-11-21", revenue: 400, expense: 0 }],
      monthly_ties_out: true,
      source_endpoint: "account.account (Tryton REST)",
      freshness,
    } as IncomeStatementResponse;
    const csv = incomeStatementToCsv(response, statementSectionLabel, filters);
    expect(csv).toContain("# صورت سود و زیان\r\n");
    expect(csv).toContain("درآمدها,Revenue,600,500\r\n");
    expect(csv).toContain("ماه شمسی,درآمد (ریال),هزینه (ریال)\r\n1405-08,400,0\r\n");
  });

  it("cash flow CSV lists the three sections", () => {
    const response = {
      company_id: "1",
      date_from: "2026-11-01",
      date_to: "2026-11-30",
      opening_cash: 1000,
      closing_cash: 1300,
      sections: [
        { key: "operating", title: "Operating", inflow: 800, outflow: 300, net: 500, line_count: 4 },
        { key: "investing", title: "Investing", inflow: 0, outflow: 200, net: -200, line_count: 1 },
        { key: "financing", title: "Financing", inflow: 0, outflow: 0, net: 0, line_count: 0 },
      ],
      net_change: 300,
      ties_out: true,
      cash_accounts: ["1.1.1"],
      transfer_count: 0,
      unclassified_count: 0,
      unclassified_total: 0,
      source_endpoint: "account.move.line (Tryton REST)",
      freshness,
    } as CashFlowResponse;
    const csv = cashFlowToCsv(response, filters);
    expect(csv).toContain("# صورت جریان وجوه نقد\r\n");
    expect(csv).toContain("operating,800,300,500,4\r\n");
    expect(csv).toContain("investing,0,200,-200,1\r\n");
  });

  it("notes balance CSV lists rows with all aging buckets", () => {
    const row = {
      party_id: 1,
      party_name: "شرکت الف",
      instrument: "receivable",
      account_code: "1.2.4",
      account_name: "اسناد دریافتنی",
      bucket_not_yet_due: 10,
      bucket_0_30: 20,
      bucket_30_60: 30,
      bucket_60_90: 40,
      bucket_90_120: 50,
      bucket_120_plus: 60,
      total: 210,
      line_count: 3,
    };
    const response = {
      company_id: "1",
      as_of: "2026-11-30",
      instrument: "both",
      rows: [row],
      totals: row,
      accounts_read: ["1.2.4", "1.2.5", "2.1.5"],
      source_endpoint: "account.move.line (Tryton REST)",
      freshness,
    } as NotesBalanceResponse;
    const csv = notesBalanceToCsv(response, filters);
    expect(csv).toContain("# ماندهٔ اسناد دریافتنی/پرداختنی\r\n");
    expect(csv).toContain("شرکت الف,receivable,1.2.4,10,20,30,40,50,60,210,3\r\n");
  });
});
