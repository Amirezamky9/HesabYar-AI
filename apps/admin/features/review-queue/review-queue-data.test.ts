import { describe, expect, it } from "vitest";
import {
  REVIEW_ACTION_LABELS,
  REVIEW_ITEM_TYPES,
  formatAgeHours,
  isWaiting,
  reviewActionLabel,
  reviewResolutionLabel,
  reviewSeverityLabel,
  reviewSeverityTone,
  reviewStatusLabel,
  reviewTypeLabel,
  slaState,
} from "./review-queue-data";

describe("review queue labels", () => {
  it("covers all ten §7.2 item types", () => {
    expect(REVIEW_ITEM_TYPES).toHaveLength(10);
    expect(REVIEW_ITEM_TYPES).toEqual([
      "mapping",
      "settlement_variance",
      "cheque_exception",
      "bank_unmatched",
      "packaging_reorder",
      "installment_overdue",
      "manual_import_review",
      "cashbox_variance",
      "inbox_dead_letter",
      "sms_delivery_failed",
    ]);
    for (const type of REVIEW_ITEM_TYPES) {
      expect(reviewTypeLabel(type)).not.toBe(type);
    }
  });

  it("labels severities and statuses in Persian", () => {
    expect(reviewSeverityLabel("critical")).toBe("بحرانی");
    expect(reviewSeverityLabel("warning")).toBe("هشدار");
    expect(reviewSeverityLabel("info")).toBe("اطلاع");
    expect(reviewSeverityTone("critical")).toBe("destructive");
    expect(reviewStatusLabel("open")).toBe("باز");
    expect(reviewStatusLabel("in_review")).toBe("در حال بررسی");
    expect(reviewStatusLabel("resolved")).toBe("حل‌شده");
    expect(reviewStatusLabel("written_off")).toBe("بسته‌شده");
  });

  it("labels every §7.2 action code", () => {
    const actionCodes = [
      "complete_mapping_retry",
      "auto_create_party_item",
      "skip",
      "accept_variance",
      "escalate_to_gateway",
      "fix_fee_mapping",
      "record_bounce",
      "contact_customer",
      "record_alternative_payment",
      "manual_match",
      "record_misc_income",
      "snooze",
      "approve_purchase",
      "change_reorder_point",
      "cash_purchase",
      "send_reminder",
      "extend_due_date",
      "activate_collateral_cheque",
      "approve_preview",
      "fix_rows_recalc",
      "cancel_batch",
      "approve_variance_entry",
      "recount",
      "escalate_to_manager",
      "replay",
      "fix_source_retry",
      "close_with_reason",
    ];
    expect(Object.keys(REVIEW_ACTION_LABELS)).toEqual(expect.arrayContaining(actionCodes));
    for (const code of actionCodes) {
      expect(reviewActionLabel(code)).not.toBe(code);
    }
  });

  it("falls back to the raw code for unknown values", () => {
    expect(reviewTypeLabel("future_type")).toBe("future_type");
    expect(reviewSeverityLabel("unknown")).toBe("unknown");
    expect(reviewStatusLabel("unknown")).toBe("unknown");
    expect(reviewActionLabel("unknown_action")).toBe("unknown_action");
    expect(reviewResolutionLabel("unknown")).toBe("unknown");
  });

  it("labels resolutions", () => {
    expect(reviewResolutionLabel("adjusted")).toBe("اصلاح شد");
    expect(reviewResolutionLabel("rescheduled")).toBe("زمان‌بندی مجدد شد");
    expect(reviewResolutionLabel(null)).toBeNull();
  });
});

describe("formatAgeHours", () => {
  it("renders minutes under one hour", () => {
    expect(formatAgeHours(0)).toBe("۱ دقیقه");
    expect(formatAgeHours(0.5)).toBe("۳۰ دقیقه");
    expect(formatAgeHours(0.99)).toBe("۵۹ دقیقه");
  });

  it("renders hours under a day", () => {
    expect(formatAgeHours(1)).toBe("۱ ساعت");
    expect(formatAgeHours(5.9)).toBe("۵ ساعت");
    expect(formatAgeHours(23.5)).toBe("۲۳ ساعت");
  });

  it("renders days and months", () => {
    expect(formatAgeHours(24)).toBe("۱ روز");
    expect(formatAgeHours(24 * 6.5)).toBe("۶ روز");
    expect(formatAgeHours(24 * 30)).toBe("۱ ماه");
    expect(formatAgeHours(24 * 65)).toBe("۲ ماه");
  });

  it("renders an em dash for missing or invalid ages", () => {
    expect(formatAgeHours(null)).toBe("—");
    expect(formatAgeHours(undefined)).toBe("—");
    expect(formatAgeHours(-1)).toBe("—");
    expect(formatAgeHours(Number.NaN)).toBe("—");
  });
});

describe("slaState / isWaiting", () => {
  it("derives the SLA state from the backend flags", () => {
    expect(slaState({ status: "open", sla_breached: true, sla_warn: false })).toBe("breached");
    expect(slaState({ status: "open", sla_breached: false, sla_warn: true })).toBe("warn");
    expect(slaState({ status: "open", sla_breached: false, sla_warn: false })).toBe("ok");
    expect(slaState({ status: "resolved", sla_breached: true, sla_warn: false })).toBe("closed");
    expect(slaState({ status: "written_off", sla_breached: false, sla_warn: false })).toBe("closed");
  });

  it("marks open and in-review items as waiting", () => {
    expect(isWaiting({ status: "open" })).toBe(true);
    expect(isWaiting({ status: "in_review" })).toBe(true);
    expect(isWaiting({ status: "resolved" })).toBe(false);
    expect(isWaiting({ status: "written_off" })).toBe(false);
  });
});
