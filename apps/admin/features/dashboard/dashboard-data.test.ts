import { describe, expect, it } from "vitest";
import type {
  BankImportBatchResponse,
  CashBoxResponse,
  ChequeResponse,
  InstallmentLegResponse,
  InvoiceResponse,
  JournalDraftResponse,
  MaterialDetailResponse,
  Paginated,
  PaymentResponse,
  SettlementFileImportResponse,
  SettlementTrancheResponse,
} from "@/lib/api-client";
import {
  buildAlerts,
  fetchAllPages,
  importBatchRows,
  jalaliMonthBounds,
  packagingReorderAlerts,
  recentInvoices,
  recentPayments,
  statementBatchStatusLabel,
  summarizeCashboxes,
  summarizeDrafts,
  summarizeInClearingReceived,
  summarizeInvoices,
  summarizeIssuedDueSoon,
  summarizeLegsDueThisMonth,
  summarizeReceivedDueSoon,
  summarizeTodayPayments,
  summarizeTranches,
} from "./dashboard-data";

function invoice(partial: Partial<InvoiceResponse>): InvoiceResponse {
  return {
    id: 1,
    company_id: "company-1",
    engine_company_id: 1,
    invoice_type: "out",
    party_id: 1,
    party_name: "مشتری نمونه",
    number: "1",
    invoice_date: "2026-11-01",
    accounting_date: "2026-11-01",
    state: "posted",
    description: "",
    reference: "",
    journal_id: null,
    account_id: 1,
    currency: "IRR",
    untaxed_amount: 1000,
    tax_amount: 0,
    total_amount: 1000,
    lines: [],
    ...partial,
  };
}

function draft(partial: Partial<JournalDraftResponse>): JournalDraftResponse {
  return {
    id: 1,
    company_id: "company-1",
    engine_company_id: 1,
    journal_id: 1,
    period: { name: "2026-11", start_date: "2026-11-01", end_date: "2026-11-30", state: "open" },
    date: "2026-11-01",
    state: "draft",
    description: "",
    lines: [],
    total_debit: 1000,
    total_credit: 1000,
    ...partial,
  };
}

function payment(partial: Partial<PaymentResponse>): PaymentResponse {
  return {
    id: 1,
    company_id: "company-1",
    engine_company_id: 1,
    number: "1",
    kind: "receivable",
    party_id: 1,
    party_name: "مشتری نمونه",
    date: "2026-11-01",
    amount: 1000,
    state: "posted",
    reference: "",
    journal_name: "نقد",
    currency: "IRR",
    ...partial,
  };
}

describe("summarizeInvoices", () => {
  it("returns honest zeros for an empty list", () => {
    expect(summarizeInvoices([])).toEqual({
      receivableCount: 0,
      receivableTotalIrr: 0,
      payableCount: 0,
      payableTotalIrr: 0,
      consideredCount: 0,
    });
  });

  it("counts only validated/posted out invoices as receivables", () => {
    const summary = summarizeInvoices([
      invoice({ id: 1, invoice_type: "out", state: "posted", total_amount: 10_000 }),
      invoice({ id: 2, invoice_type: "out", state: "validated", total_amount: 5_000 }),
      invoice({ id: 3, invoice_type: "out", state: "draft", total_amount: 99_000 }),
      invoice({ id: 4, invoice_type: "out", state: "paid", total_amount: 99_000 }),
      invoice({ id: 5, invoice_type: "out", state: "cancelled", total_amount: 99_000 }),
    ]);
    expect(summary.receivableCount).toBe(2);
    expect(summary.receivableTotalIrr).toBe(15_000);
    expect(summary.payableCount).toBe(0);
    expect(summary.consideredCount).toBe(5);
  });

  it("counts only validated/posted in invoices as payables", () => {
    const summary = summarizeInvoices([
      invoice({ id: 1, invoice_type: "in", state: "posted", total_amount: 7_000 }),
      invoice({ id: 2, invoice_type: "in", state: "draft", total_amount: 99_000 }),
    ]);
    expect(summary.payableCount).toBe(1);
    expect(summary.payableTotalIrr).toBe(7_000);
    expect(summary.receivableCount).toBe(0);
  });

  it("subtracts credit notes from the matching side", () => {
    const summary = summarizeInvoices([
      invoice({ id: 1, invoice_type: "out", state: "posted", total_amount: 10_000 }),
      invoice({ id: 2, invoice_type: "out_credit_note", state: "posted", total_amount: 2_000 }),
      invoice({ id: 3, invoice_type: "in", state: "posted", total_amount: 8_000 }),
      invoice({ id: 4, invoice_type: "in_credit_note", state: "posted", total_amount: 3_000 }),
    ]);
    expect(summary.receivableTotalIrr).toBe(8_000);
    expect(summary.payableTotalIrr).toBe(5_000);
    // Credit notes reduce the total but are not counted as open invoices.
    expect(summary.receivableCount).toBe(1);
    expect(summary.payableCount).toBe(1);
  });

  it("ignores unknown invoice types instead of counting them", () => {
    const summary = summarizeInvoices([
      invoice({ id: 1, invoice_type: "mystery", state: "posted", total_amount: 50_000 }),
    ]);
    expect(summary.receivableTotalIrr).toBe(0);
    expect(summary.payableTotalIrr).toBe(0);
  });
});

describe("summarizeDrafts", () => {
  it("sums integer IRR debit/credit across drafts", () => {
    expect(
      summarizeDrafts([
        draft({ id: 1, total_debit: 10_000, total_credit: 10_000 }),
        draft({ id: 2, total_debit: 2_500, total_credit: 2_500 }),
      ]),
    ).toEqual({ count: 2, totalDebitIrr: 12_500, totalCreditIrr: 12_500 });
  });

  it("returns honest zeros for an empty list", () => {
    expect(summarizeDrafts([])).toEqual({ count: 0, totalDebitIrr: 0, totalCreditIrr: 0 });
  });
});

describe("recentInvoices / recentPayments", () => {
  it("sorts by normalized ISO date descending, id descending on ties", () => {
    const sorted = recentInvoices(
      [
        invoice({ id: 1, invoice_date: "2026-11-01" }),
        invoice({ id: 2, invoice_date: "2026-11-03" }),
        invoice({ id: 3, invoice_date: "2026-11-03" }),
        invoice({ id: 4, invoice_date: null, accounting_date: "2026-10-01" }),
      ],
      10,
    );
    expect(sorted.map((i) => i.id)).toEqual([3, 2, 1, 4]);
  });

  it("respects the limit", () => {
    const sorted = recentPayments(
      [
        payment({ id: 1, date: "2026-11-01" }),
        payment({ id: 2, date: "2026-11-02" }),
        payment({ id: 3, date: "2026-11-03" }),
      ],
      2,
    );
    expect(sorted.map((p) => p.id)).toEqual([3, 2]);
  });
});

describe("fetchAllPages", () => {
  it("follows cursors until the list ends", async () => {
    const pages: Paginated<number>[] = [
      { items: [1, 2], next_cursor: "c2" },
      { items: [3], next_cursor: null },
    ];
    const seen: (string | undefined)[] = [];
    const result = await fetchAllPages<number>(async (cursor) => {
      seen.push(cursor);
      return pages[cursor === undefined ? 0 : 1];
    });
    expect(result).toEqual({ items: [1, 2, 3], truncated: false });
    expect(seen).toEqual([undefined, "c2"]);
  });

  it("reports truncated when the page cap stops paging", async () => {
    const result = await fetchAllPages<number>(
      async () => ({ items: [1], next_cursor: "more" }),
      { maxPages: 2 },
    );
    expect(result.truncated).toBe(true);
    expect(result.items).toEqual([1, 1]);
  });

  it("reports truncated when the item cap is hit", async () => {
    const result = await fetchAllPages<number>(
      async () => ({ items: [1, 2, 3], next_cursor: "more" }),
      { maxItems: 2 },
    );
    expect(result.truncated).toBe(true);
    expect(result.items).toEqual([1, 2]);
  });

  it("is not truncated when the last page ends the list exactly at the cap", async () => {
    const result = await fetchAllPages<number>(
      async (cursor) =>
        cursor === undefined
          ? { items: [1], next_cursor: "c2" }
          : { items: [2], next_cursor: null },
      { maxPages: 2 },
    );
    expect(result).toEqual({ items: [1, 2], truncated: false });
  });
});

// ---------------------------------------------------------------------------
// US-551 — the six unified-dashboard sections
// ---------------------------------------------------------------------------

function tranche(partial: Partial<SettlementTrancheResponse>): SettlementTrancheResponse {
  return {
    id: "tr-1",
    gateway_payment_id: "pay-1",
    tranche_index: 1,
    tranche_count: 4,
    sales_period_jalali: "1405/08",
    expected_window_start: "2026-11-17",
    expected_date: "2026-11-20",
    gross_irr: 10_000_000,
    fee_irr: 500_000,
    fee_vat_irr: 50_000,
    net_irr: 9_450_000,
    confirmed_net_irr: 0,
    received_net_irr: 0,
    outstanding_net_irr: 9_450_000,
    state: "expected",
    tryton_receivable_line_id: null,
    variance_irr: null,
    variance_reason: null,
    match_method: null,
    match_confidence: null,
    created_at: null,
    updated_at: null,
    ...partial,
  };
}

function cheque(partial: Partial<ChequeResponse>): ChequeResponse {
  return {
    id: "chk-1",
    state: "held",
    kind: "received",
    purpose: "payment",
    sayad_id: "1234567890123456",
    sayad_status: "registered",
    sayad_unconfirmed: false,
    amount_irr: 10_000_000,
    amount_toman: 1_000_000,
    issue_date: "1405-08-01",
    due_date: "1405-08-28",
    issuer_name: "مشتری آزمایشی",
    issuer_national_id: "0012345678",
    issuer_bank: "056 سامان",
    payee_name: "فروشگاه",
    payee_national_id: "0012345679",
    order_id: "ORD-1",
    customer_ref: "CUST-1",
    leaf_number: "123456",
    issuer_branch: null,
    issuer_account_number: null,
    reason: null,
    invoice_id: null,
    installment_leg_id: null,
    installment_leg_status: null,
    scan_url: null,
    scan_ref: null,
    scan_filestore_ref: null,
    source_event_id: null,
    age_days: 2,
    storage_location: "vault",
    engine_id: null,
    move_ids: {},
    destination_bank_account_id: null,
    destination_bank_name: null,
    deposit_date: null,
    clearing_confidence: null,
    clearing_statement_ref: null,
    clearing_reconciliation_id: null,
    issuer_color_code: null,
    issuer_back_cheques: null,
    bounce_reason: null,
    bounce_date: null,
    bounce_certificate_reference: null,
    endorsee_name: null,
    endorsee_type: null,
    sayad_transfer_reference: null,
    checkbook_engine_id: null,
    secured_obligation: null,
    void_reason: null,
    call_in_approved_by: null,
    call_in_approved_at: null,
    unexpected_call_in: false,
    call_in_statement_reference: null,
    journal_pending_review: false,
    receipt_missing_warning: false,
    mismatch_detail: null,
    warnings: [],
    created_at: "2026-11-17T00:00:00Z",
    updated_at: "2026-11-17T00:00:00Z",
    ...partial,
  };
}

function leg(partial: Partial<InstallmentLegResponse>): InstallmentLegResponse {
  return {
    id: "leg-1",
    plan_id: "plan-1",
    sequence: 1,
    due_date: "1405-08-28",
    principal_irr: 10_000_000,
    principal_toman: 1_000_000,
    received_irr: 0,
    received_toman: 0,
    balance_irr: 10_000_000,
    balance_toman: 1_000_000,
    status: "due",
    pending_clearance_irr: 0,
    pending_clearance_toman: 0,
    linked_cheque: null,
    ...partial,
  };
}

function cashbox(partial: Partial<CashBoxResponse>): CashBoxResponse {
  return {
    id: "box-1",
    company_id: "company-1",
    name_fa: "صندوق فروشگاه",
    kind: "cashbox",
    statement_journal_id: 1,
    cash_account_id: 1,
    cash_account_code: "1.1.1",
    ceiling_irr: null,
    active: true,
    book_balance_irr: 5_000_000,
    last_count_at: "2026-11-17T00:00:00Z",
    days_since_last_count: 1,
    uncounted_alert: false,
    created_at: null,
    updated_at: null,
    ...partial,
  };
}

function material(partial: Partial<MaterialDetailResponse>): MaterialDetailResponse {
  return {
    material: {
      id: "mat-1",
      company_id: "company-1",
      tryton_product_id: 1,
      tryton_template_id: 1,
      name_fa: "کارتن مقوایی",
      cost_method: "average",
      code: "BOX-1",
      uom_name: "عدد",
      is_active: true,
      created_at: null,
    },
    stock: {
      material_id: "mat-1",
      quantity: "12",
      unit_cost_irr: "100000",
      location_engine_id: 1,
      location_name: "انبار اصلی",
      stock_value_irr: "1200000",
      is_negative: false,
    },
    reorder_point: {
      material_id: "mat-1",
      warehouse_engine_id: 1,
      min_quantity: "10",
      target_quantity: "50",
      tryton_order_point_id: null,
      max_quantity: null,
    },
    coverage: null,
    ...partial,
  };
}

function statementBatch(partial: Partial<BankImportBatchResponse>): BankImportBatchResponse {
  return {
    id: "batch-1",
    company_id: "company-1",
    bank_key: "melli",
    statement_journal_id: 1,
    filename: "melli-1405-08.csv",
    file_format: "csv",
    currency_unit: "IRR",
    status: "awaiting_review",
    total_rows: 10,
    new_rows: 8,
    duplicate_rows: 1,
    invalid_rows: 1,
    total_deposits_irr: 20_000_000,
    total_withdrawals_irr: 5_000_000,
    default_account_id: 1,
    column_mapping_id: null,
    start_balance_irr: null,
    end_balance_irr: null,
    engine_statement_id: null,
    review_item_id: null,
    created_at: "2026-11-17T10:00:00Z",
    confirmed_at: null,
    cancelled_at: null,
    max_rows: 1000,
    ...partial,
  };
}

function settlementImport(partial: Partial<SettlementFileImportResponse>): SettlementFileImportResponse {
  return {
    id: "imp-1",
    payment_channel_id: "chan-1",
    file_type: "detail",
    period_jalali: "1405/08",
    file_sha256: "abc",
    import_batch_id: "batch-9",
    status: "awaiting_review",
    uploaded_by: null,
    uploaded_at: null,
    row_count: 20,
    matched_count: 18,
    new_count: 2,
    error_count: 0,
    created_at: "2026-11-18T10:00:00Z",
    updated_at: null,
    ...partial,
  };
}

describe("jalaliMonthBounds", () => {
  it("maps 2026-11-17 to Aban 1405 in both calendars", () => {
    // Anchor: 2026-11-17 = 1405-08-26 (lib/jalali.test.ts).
    const bounds = jalaliMonthBounds(new Date(2026, 10, 17));
    expect(bounds.year).toBe(1405);
    expect(bounds.month).toBe(8);
    expect(bounds.startJalali).toBe("1405-08-01");
    expect(bounds.endJalali).toBe("1405-08-30");
    expect(bounds.startIso).toBe("2026-10-23");
    expect(bounds.endIso).toBe("2026-11-21");
    expect(bounds.monthLabel).toBe("آبان 1405");
  });

  it("ends Esfand 1405 on day 29 (1405 is not a leap year)", () => {
    const bounds = jalaliMonthBounds(new Date(2027, 2, 19));
    expect(bounds.month).toBe(12);
    expect(bounds.startJalali).toBe("1405-12-01");
    expect(bounds.endJalali).toBe("1405-12-29");
  });
});

describe("summarizeTodayPayments", () => {
  it("counts only succeeded payments dated today, grouped by journal", () => {
    const summary = summarizeTodayPayments(
      [
        payment({ id: 1, kind: "receivable", state: "succeeded", date: "2026-11-17", amount: 10_000, journal_name: "بانک ملی" }),
        payment({ id: 2, kind: "receivable", state: "succeeded", date: "2026-11-17", amount: 5_000, journal_name: "بانک ملی" }),
        payment({ id: 3, kind: "payable", state: "succeeded", date: "2026-11-17", amount: 3_000, journal_name: "صندوق" }),
        payment({ id: 4, kind: "receivable", state: "draft", date: "2026-11-17", amount: 99_000 }),
        payment({ id: 5, kind: "receivable", state: "approved", date: "2026-11-17", amount: 99_000 }),
        payment({ id: 6, kind: "receivable", state: "failed", date: "2026-11-17", amount: 99_000 }),
        payment({ id: 7, kind: "receivable", state: "succeeded", date: "2026-11-16", amount: 99_000 }),
      ],
      "2026-11-17",
    );
    expect(summary.receiptCount).toBe(2);
    expect(summary.receiptTotalIrr).toBe(15_000);
    expect(summary.paymentCount).toBe(1);
    expect(summary.paymentTotalIrr).toBe(3_000);
    expect(summary.receiptsByJournal).toEqual([
      { journal: "بانک ملی", count: 2, totalIrr: 15_000 },
    ]);
    expect(summary.paymentsByJournal).toEqual([{ journal: "صندوق", count: 1, totalIrr: 3_000 }]);
    expect(summary.consideredCount).toBe(7);
  });

  it("returns honest zeros for an empty list", () => {
    expect(summarizeTodayPayments([], "2026-11-17")).toEqual({
      receiptCount: 0,
      receiptTotalIrr: 0,
      paymentCount: 0,
      paymentTotalIrr: 0,
      receiptsByJournal: [],
      paymentsByJournal: [],
      consideredCount: 0,
    });
  });
});

describe("summarizeTranches", () => {
  it("sums outstanding on open tranches and splits due-this-month + overdue", () => {
    const summary = summarizeTranches(
      [
        tranche({ id: "t1", state: "expected", expected_date: "2026-11-20", outstanding_net_irr: 9_000_000 }),
        tranche({ id: "t2", state: "due", expected_date: "2026-12-05", outstanding_net_irr: 4_000_000 }),
        tranche({ id: "t3", state: "overdue", expected_date: "2026-11-10", outstanding_net_irr: 2_000_000 }),
        tranche({ id: "t4", state: "settled", expected_date: "2026-11-05", outstanding_net_irr: 0 }),
        tranche({ id: "t5", state: "cancelled", expected_date: "2026-11-05", outstanding_net_irr: 7_000_000 }),
        tranche({ id: "t6", state: "written_off", expected_date: "2026-11-05", outstanding_net_irr: 8_000_000 }),
      ],
      "2026-10-23",
      "2026-11-21",
    );
    expect(summary.receivableCount).toBe(3);
    expect(summary.receivableTotalIrr).toBe(15_000_000);
    expect(summary.dueThisMonthCount).toBe(2);
    expect(summary.dueThisMonthTotalIrr).toBe(11_000_000);
    expect(summary.overdueCount).toBe(1);
    expect(summary.overdueTotalIrr).toBe(2_000_000);
    expect(summary.consideredCount).toBe(6);
  });
});

describe("summarizeInClearingReceived", () => {
  it("counts only received commercial cheques in clearing", () => {
    const summary = summarizeInClearingReceived([
      cheque({ id: "c1", state: "in_clearing", amount_irr: 10_000_000 }),
      cheque({ id: "c2", state: "in_clearing", kind: "issued", amount_irr: 99_000_000 }),
      cheque({ id: "c3", state: "in_clearing", purpose: "collateral", amount_irr: 99_000_000 }),
      cheque({ id: "c4", state: "held", amount_irr: 99_000_000 }),
    ]);
    expect(summary).toEqual({ count: 1, totalIrr: 10_000_000 });
  });
});

describe("summarizeLegsDueThisMonth", () => {
  it("counts unpaid legs in the Jalali month by balance, not principal", () => {
    const summary = summarizeLegsDueThisMonth(
      [
        leg({ id: "l1", status: "due", due_date: "1405-08-28", balance_irr: 6_000_000, principal_irr: 10_000_000 }),
        leg({ id: "l2", status: "partially_paid", due_date: "1405-08-05", balance_irr: 2_000_000 }),
        leg({ id: "l3", status: "expected", due_date: "1405-09-01", balance_irr: 99_000_000 }),
        leg({ id: "l4", status: "paid", due_date: "1405-08-10", balance_irr: 0 }),
        // An overdue leg due this month still counts: the money is due.
        leg({ id: "l5", status: "overdue", due_date: "1405-08-02", balance_irr: 4_000_000 }),
        // Pending clearance is money already received — not owed.
        leg({ id: "l6", status: "pending_clearance", due_date: "1405-08-03", balance_irr: 99_000_000 }),
      ],
      "1405-08-01",
      "1405-08-30",
    );
    expect(summary).toEqual({ count: 3, totalIrr: 12_000_000 });
  });
});

describe("summarizeCashboxes", () => {
  it("sums known balances of active boxes and lists uncounted ones", () => {
    const summary = summarizeCashboxes([
      cashbox({ id: "b1", book_balance_irr: 5_000_000 }),
      cashbox({ id: "b2", book_balance_irr: null }),
      cashbox({ id: "b3", book_balance_irr: 2_000_000, uncounted_alert: true, days_since_last_count: 9, name_fa: "صندوق شعبه" }),
      cashbox({ id: "b4", active: false, book_balance_irr: 99_000_000 }),
    ]);
    expect(summary.boxCount).toBe(3);
    expect(summary.totalBalanceIrr).toBe(7_000_000);
    expect(summary.knownBalanceCount).toBe(2);
    expect(summary.unknownBalanceCount).toBe(1);
    expect(summary.uncounted).toEqual([{ id: "b3", nameFa: "صندوق شعبه", daysSinceLastCount: 9 }]);
  });
});

describe("summarizeIssuedDueSoon / summarizeReceivedDueSoon", () => {
  const TODAY = "1405-08-26";

  it("finds issued cheques due within 7 days or overdue, nearest first", () => {
    const summary = summarizeIssuedDueSoon(
      [
        cheque({ id: "c1", kind: "issued", state: "issued", due_date: "1405-08-30", amount_irr: 5_000_000 }),
        cheque({ id: "c2", kind: "issued", state: "issued", due_date: "1405-08-20", amount_irr: 7_000_000 }),
        cheque({ id: "c3", kind: "issued", state: "issued", due_date: "1405-09-10", amount_irr: 99_000_000 }),
        cheque({ id: "c4", kind: "issued", state: "issued", purpose: "collateral", due_date: "1405-08-27", amount_irr: 99_000_000 }),
        cheque({ id: "c5", kind: "issued", state: "voided", due_date: "1405-08-27", amount_irr: 99_000_000 }),
      ],
      TODAY,
    );
    expect(summary.count).toBe(2);
    expect(summary.totalIrr).toBe(12_000_000);
    expect(summary.cheques.map((c) => c.id)).toEqual(["c2", "c1"]);
    expect(summary.nearestDueJalali).toBe("1405-08-20");
  });

  it("finds received cheques due within 7 days or overdue", () => {
    const summary = summarizeReceivedDueSoon(
      [
        cheque({ id: "c1", state: "held", due_date: "1405-08-28", amount_irr: 5_000_000 }),
        cheque({ id: "c2", state: "in_clearing", due_date: "1405-08-25", amount_irr: 3_000_000 }),
        cheque({ id: "c3", state: "cleared", due_date: "1405-08-27", amount_irr: 99_000_000 }),
        cheque({ id: "c4", state: "held", due_date: "1405-10-01", amount_irr: 99_000_000 }),
      ],
      TODAY,
    );
    expect(summary.count).toBe(2);
    expect(summary.totalIrr).toBe(8_000_000);
    expect(summary.nearestDueJalali).toBe("1405-08-25");
  });
});

describe("packagingReorderAlerts", () => {
  it("flags materials below their reorder point only", () => {
    const alerts = packagingReorderAlerts([
      material({ stock: { material_id: "mat-1", quantity: "5", unit_cost_irr: null, location_engine_id: 1, location_name: "انبار", stock_value_irr: null, is_negative: false } }),
      material({ material: { id: "mat-2", company_id: "company-1", tryton_product_id: 2, tryton_template_id: 2, name_fa: "چسب", cost_method: "average", code: null, uom_name: null, is_active: true, created_at: null } }),
      material({
        material: { id: "mat-3", company_id: "company-1", tryton_product_id: 3, tryton_template_id: 3, name_fa: "برچسب", cost_method: "average", code: null, uom_name: null, is_active: false, created_at: null },
        stock: { material_id: "mat-3", quantity: "1", unit_cost_irr: null, location_engine_id: 1, location_name: "انبار", stock_value_irr: null, is_negative: false },
      }),
      material({ stock: null }),
      material({ reorder_point: null }),
      material({ stock: { material_id: "mat-1", quantity: "not-a-number", unit_cost_irr: null, location_engine_id: 1, location_name: "انبار", stock_value_irr: null, is_negative: false } }),
    ]);
    expect(alerts).toEqual([
      { materialId: "mat-1", nameFa: "کارتن مقوایی", quantity: "5", minQuantity: "10" },
    ]);
  });
});

describe("buildAlerts", () => {
  const empty = {
    overdueTranches: { count: 0, totalIrr: 0 },
    chequesDueSoon: { count: 0, totalIrr: 0 },
    overdueInstallments: { count: 0, totalIrr: 0 },
    reorderCrossed: [],
    uncountedCashboxes: [],
    staleInbox: { count: 0, hours: 24 },
  };

  it("returns no alerts when nothing needs attention", () => {
    expect(buildAlerts(empty)).toEqual([]);
  });

  it("orders critical first, then warning, in fixed kind order", () => {
    const alerts = buildAlerts({
      ...empty,
      chequesDueSoon: { count: 2, totalIrr: 5_000_000 },
      overdueTranches: { count: 1, totalIrr: 9_000_000 },
      uncountedCashboxes: [{ id: "b1", nameFa: "صندوق", daysSinceLastCount: 9 }],
      staleInbox: { count: 3, hours: 24 },
      overdueInstallments: { count: 1, totalIrr: 4_000_000 },
      reorderCrossed: [{ materialId: "m1", nameFa: "کارتن", quantity: "5", minQuantity: "10" }],
    });
    expect(alerts.map((a) => a.kind)).toEqual([
      "overdue-installment",
      "overdue-tranche",
      "stale-inbox",
      "cheque-due-soon",
      "packaging-reorder",
      "cashbox-uncounted",
    ]);
    expect(alerts.map((a) => a.severity)).toEqual([
      "critical",
      "critical",
      "critical",
      "warning",
      "warning",
      "warning",
    ]);
    expect(alerts[0].href).toBe("/installments/reports?report=overdue");
    expect(alerts[1].href).toBe("/settlements/schedule?state=overdue");
    expect(alerts[2].totalIrr).toBeNull();
  });
});

describe("importBatchRows", () => {
  it("merges both flows newest first and respects the limit", () => {
    const rows = importBatchRows(
      [
        statementBatch({ id: "s1", created_at: "2026-11-17T10:00:00Z" }),
        statementBatch({ id: "s2", created_at: null }),
      ],
      [settlementImport({ id: "g1", created_at: "2026-11-18T10:00:00Z" })],
      2,
    );
    expect(rows.map((r) => r.id)).toEqual(["settlement:g1", "statement:s1"]);
    expect(rows[0].title).toBe("تسویهٔ دورهٔ 1405/08");
    expect(rows[0].href).toBe("/settlements/imports/g1");
    expect(rows[1].depositsIrr).toBe(20_000_000);
    expect(rows[1].withdrawalsIrr).toBe(5_000_000);
    expect(rows[1].href).toBe("/banking/statements/import");
  });

  it("returns an empty list when both flows are empty", () => {
    expect(importBatchRows([], [], 5)).toEqual([]);
  });
});

describe("statementBatchStatusLabel", () => {
  it("labels the three batch statuses and falls back to the raw code", () => {
    expect(statementBatchStatusLabel("awaiting_review")).toBe("در انتظار بازبینی");
    expect(statementBatchStatusLabel("confirmed")).toBe("تأیید و ثبت شد");
    expect(statementBatchStatusLabel("cancelled")).toBe("لغوشده");
    expect(statementBatchStatusLabel("mystery")).toBe("mystery");
    expect(statementBatchStatusLabel(null)).toBe("نامشخص");
  });
});
