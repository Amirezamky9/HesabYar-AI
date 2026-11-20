import { describe, expect, it } from "vitest";
import {
  invoicePartySummaryToCsv,
  invoiceStateSummaryToCsv,
  paymentKindSummaryToCsv,
  paymentStateSummaryToCsv,
} from "./report-csv";

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
