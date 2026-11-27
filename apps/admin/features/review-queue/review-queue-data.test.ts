import { describe, expect, it } from "vitest";
import {
  MAPPING_KINDS,
  MAPPING_TARGET_TYPES,
  REVIEW_ACTION_LABELS,
  REVIEW_ITEM_TYPES,
  SUGGESTED_REASON_LABELS,
  formatAgeHours,
  isWaiting,
  mappingKindLabel,
  mappingTargetTypeLabel,
  reviewActionLabel,
  reviewResolutionLabel,
  reviewSeverityLabel,
  reviewSeverityTone,
  reviewStatusLabel,
  reviewTypeLabel,
  slaState,
  suggestedReasonLabel,
} from "./review-queue-data";

describe("review queue labels", () => {
  it("covers all eleven §7.2 item types", () => {
    expect(REVIEW_ITEM_TYPES).toHaveLength(11);
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
      "reminder",
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
      "acknowledge",
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

describe("derived suggestions (US-552)", () => {
  it("has a Persian reason for every reason key the backend derives", () => {
    const reasonKeys = [
      "mapping_blocks_event",
      "variance_is_fee_mismatch",
      "large_variance_needs_gateway",
      "small_variance_accept",
      "cheque_bounced",
      "cheque_needs_customer",
      "line_needs_match",
      "stock_below_reorder",
      "installment_overdue_remind",
      "batch_preview_ready",
      "large_variance_needs_manager",
      "variance_needs_approval",
      "event_dead_lettered",
      "sms_send_failed",
      "reminder_needs_follow_up",
      "reminder_needs_attention",
    ];
    expect(Object.keys(SUGGESTED_REASON_LABELS)).toEqual(expect.arrayContaining(reasonKeys));
    for (const key of reasonKeys) {
      const label = suggestedReasonLabel(key);
      expect(label).not.toBe(key);
      expect(label.length).toBeGreaterThan(0);
      expect(label.endsWith(".")).toBe(true);
    }
  });

  it("falls back to the raw key for an unknown reason", () => {
    expect(suggestedReasonLabel("future_reason")).toBe("future_reason");
    expect(suggestedReasonLabel(null)).toBe("");
    expect(suggestedReasonLabel(undefined)).toBe("");
  });

  it("labels all nine §10.5 mapping kinds", () => {
    expect(MAPPING_KINDS).toHaveLength(9);
    expect(MAPPING_KINDS).toEqual([
      "gateway_journal",
      "revenue_category",
      "shipping_charge",
      "discount",
      "refund_reason",
      "tax_category",
      "store_company",
      "external_party",
      "product_dimension",
    ]);
    for (const kind of MAPPING_KINDS) {
      expect(mappingKindLabel(kind)).not.toBe(kind);
    }
    expect(mappingKindLabel("unknown_kind")).toBe("unknown_kind");
  });

  it("labels all six mapping target types", () => {
    expect(MAPPING_TARGET_TYPES).toHaveLength(6);
    for (const targetType of MAPPING_TARGET_TYPES) {
      expect(mappingTargetTypeLabel(targetType)).not.toBe(targetType);
    }
    expect(mappingTargetTypeLabel("account")).toBe("حساب");
    expect(mappingTargetTypeLabel(null)).toBe("نامشخص");
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
