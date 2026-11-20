/**
 * Vitest for the manual-import batch labels + mapping helpers (US-554).
 */

import { describe, expect, it } from "vitest";
import {
  BATCH_STATUSES,
  batchKindLabel,
  batchStageLabel,
  batchStatusLabel,
  batchStatusTone,
  postingStatusLabel,
  postingStatusTone,
  settlementFieldLabel,
  settlementMappingIsComplete,
  settlementMappingProblems,
} from "./import-batch-data";

describe("batchStatusLabel / batchStatusTone", () => {
  it("labels every lifecycle state in Persian", () => {
    expect(batchStatusLabel("uploaded")).toBe("آپلودشده");
    expect(batchStatusLabel("mapped")).toBe("نگاشت‌شده");
    expect(batchStatusLabel("validated")).toBe("اعتبارسنجی‌شده");
    expect(batchStatusLabel("previewed")).toBe("پیش‌نمایش");
    expect(batchStatusLabel("awaiting_review")).toBe("در انتظار بازبینی");
    expect(batchStatusLabel("approved")).toBe("تأییدشده");
    expect(batchStatusLabel("posted")).toBe("ثبت‌شده");
    expect(batchStatusLabel("rejected")).toBe("ردشده");
    expect(batchStatusLabel("archived")).toBe("بایگانی‌شده");
  });

  it("covers every state in BATCH_STATUSES", () => {
    for (const status of BATCH_STATUSES) {
      expect(batchStatusLabel(status)).not.toBe(status);
    }
  });

  it("tones: awaiting review warns, posted succeeds, rejected is destructive", () => {
    expect(batchStatusTone("awaiting_review")).toBe("warning");
    expect(batchStatusTone("approved")).toBe("success");
    expect(batchStatusTone("posted")).toBe("success");
    expect(batchStatusTone("rejected")).toBe("destructive");
    expect(batchStatusTone("archived")).toBe("outline");
  });

  it("falls back to the raw code for unknown states", () => {
    expect(batchStatusLabel("quarantined")).toBe("quarantined");
    expect(batchStatusTone("quarantined")).toBe("outline");
    expect(batchStatusLabel(null)).toBe("نامشخص");
    expect(batchStatusLabel(undefined)).toBe("نامشخص");
  });

  it("stage labels match status labels", () => {
    expect(batchStageLabel("awaiting_review")).toBe("در انتظار بازبینی");
    expect(batchStageLabel("rejected")).toBe("ردشده");
  });
});

describe("batchKindLabel", () => {
  it("labels both kinds", () => {
    expect(batchKindLabel("statement")).toBe("صورت‌حساب بانکی");
    expect(batchKindLabel("settlement")).toBe("تسویهٔ درگاه");
    expect(batchKindLabel("invoice")).toBe("invoice");
    expect(batchKindLabel(null)).toBe("نامشخص");
  });
});

describe("postingStatusLabel / postingStatusTone", () => {
  it("labels every posting step", () => {
    expect(postingStatusLabel("draft_hold")).toBe("نگه‌داشته به‌صورت پیش‌نویس");
    expect(postingStatusLabel("held_gate")).toBe("متوقف پشت درِ سیاست");
    expect(postingStatusLabel("tieout_failed")).toBe("تطبیق سه‌طرفه برقرار نشد");
    expect(postingStatusLabel("posting_failed")).toBe("ثبت در موتور ناموفق بود");
    expect(postingStatusLabel("posted")).toBe("ثبت‌شده");
    expect(postingStatusLabel(null)).toBe("—");
  });

  it("tones: held warns, failed is destructive, posted succeeds", () => {
    expect(postingStatusTone("draft_hold")).toBe("warning");
    expect(postingStatusTone("held_gate")).toBe("warning");
    expect(postingStatusTone("tieout_failed")).toBe("destructive");
    expect(postingStatusTone("posting_failed")).toBe("destructive");
    expect(postingStatusTone("posted")).toBe("success");
  });
});

describe("settlementFieldLabel", () => {
  it("labels the canonical fields", () => {
    expect(settlementFieldLabel("transaction_id")).toBe("شماره تراکنش");
    expect(settlementFieldLabel("net")).toBe("مبلغ خالص");
    expect(settlementFieldLabel("tracking_ref")).toBe("شماره پیگیری");
    expect(settlementFieldLabel("unknown_field")).toBe("unknown_field");
  });
});

describe("settlementMappingProblems", () => {
  const fullDetail = {
    transaction_id: "شماره تراکنش",
    date: "تاریخ تراکنش",
    gross: "مبلغ ناخالص",
    fee: "کارمزد",
    vat: "مالیات",
    net: "مبلغ خالص",
    tran_type: "نوع تراکنش",
  };

  it("a full detail mapping is complete", () => {
    expect(settlementMappingProblems(fullDetail, "detail")).toEqual([]);
    expect(settlementMappingIsComplete(fullDetail, "detail")).toBe(true);
  });

  it("detail needs transaction_id + date + net-or-gross", () => {
    expect(settlementMappingProblems({}, "detail")).toEqual([
      "شماره تراکنش الزامی است.",
      "تاریخ تراکنش الزامی است.",
      "مبلغ خالص یا مبلغ ناخالص الزامی است.",
    ]);
    const grossOnly = {
      transaction_id: "شماره تراکنش",
      date: "تاریخ تراکنش",
      gross: "مبلغ ناخالص",
    };
    expect(settlementMappingProblems(grossOnly, "detail")).toEqual([]);
    const noAmount = {
      transaction_id: "شماره تراکنش",
      date: "تاریخ تراکنش",
      fee: "کارمزد",
    };
    expect(settlementMappingProblems(noAmount, "detail")).toEqual([
      "مبلغ خالص یا مبلغ ناخالص الزامی است.",
    ]);
  });

  it("payouts/statement need date + net", () => {
    expect(settlementMappingProblems({}, "payouts")).toEqual([
      "تاریخ تراکنش الزامی است.",
      "مبلغ خالص الزامی است.",
    ]);
    expect(
      settlementMappingProblems({ date: "تاریخ", net: "خالص" }, "statement"),
    ).toEqual([]);
    expect(settlementMappingIsComplete({ date: "تاریخ" }, "payouts")).toBe(false);
  });

  it("an unknown file type is a problem", () => {
    expect(settlementMappingProblems(fullDetail, "weird")).toEqual([
      "نوع فایل نامعتبر است.",
    ]);
  });
});
