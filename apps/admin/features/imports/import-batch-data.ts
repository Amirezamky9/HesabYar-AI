/**
 * Persian labels + pure helpers for the unified manual-import batches
 * (US-554, Phase 5-6 Wave B).
 *
 * Every batch status, stage, posting status and settlement mapping
 * field the imports API returns gets one Persian label. Unknown values
 * fall back to the raw code so a new backend value never silently
 * disappears.
 *
 * Pure helpers (mapping completeness per file type, stage ordering)
 * live here so the wizards and the batch detail share them — and so
 * vitest can cover them without a backend.
 */

import type { StatusTone } from "@/components/ui";

/** Batch lifecycle, in happy-path order. */
export const BATCH_STATUSES = [
  "uploaded",
  "mapped",
  "validated",
  "previewed",
  "awaiting_review",
  "approved",
  "posted",
  "rejected",
  "archived",
] as const;

export const BATCH_STATUS_LABELS: Record<string, { label: string; tone: StatusTone }> = {
  uploaded: { label: "آپلودشده", tone: "outline" },
  mapped: { label: "نگاشت‌شده", tone: "outline" },
  validated: { label: "اعتبارسنجی‌شده", tone: "outline" },
  previewed: { label: "پیش‌نمایش", tone: "outline" },
  awaiting_review: { label: "در انتظار بازبینی", tone: "warning" },
  approved: { label: "تأییدشده", tone: "success" },
  posted: { label: "ثبت‌شده", tone: "success" },
  rejected: { label: "ردشده", tone: "destructive" },
  archived: { label: "بایگانی‌شده", tone: "outline" },
};

export function batchStatusLabel(status: string | null | undefined): string {
  if (!status) return "نامشخص";
  return BATCH_STATUS_LABELS[status]?.label ?? status;
}

export function batchStatusTone(status: string | null | undefined): StatusTone {
  if (!status) return "outline";
  return BATCH_STATUS_LABELS[status]?.tone ?? "outline";
}

/** The stage history shows the same labels as the status. */
export function batchStageLabel(stage: string | null | undefined): string {
  return batchStatusLabel(stage);
}

export const BATCH_KIND_LABELS: Record<string, string> = {
  statement: "صورت‌حساب بانکی",
  settlement: "تسویهٔ درگاه",
};

export function batchKindLabel(kind: string | null | undefined): string {
  if (!kind) return "نامشخص";
  return BATCH_KIND_LABELS[kind] ?? kind;
}

/** Posting-step statuses (stored on the batch, separate from the cycle state). */
export const POSTING_STATUS_LABELS: Record<string, { label: string; tone: StatusTone }> = {
  draft_hold: { label: "نگه‌داشته به‌صورت پیش‌نویس", tone: "warning" },
  held_gate: { label: "متوقف پشت درِ سیاست", tone: "warning" },
  tieout_failed: { label: "تطبیق سه‌طرفه برقرار نشد", tone: "destructive" },
  posting_failed: { label: "ثبت در موتور ناموفق بود", tone: "destructive" },
  posted: { label: "ثبت‌شده", tone: "success" },
};

export function postingStatusLabel(status: string | null | undefined): string {
  if (!status) return "—";
  return POSTING_STATUS_LABELS[status]?.label ?? status;
}

export function postingStatusTone(status: string | null | undefined): StatusTone {
  if (!status) return "outline";
  return POSTING_STATUS_LABELS[status]?.tone ?? "outline";
}

/** Canonical settlement-file fields, in the order the editor shows them. */
export const SETTLEMENT_MAPPING_FIELDS: { key: string; label: string }[] = [
  { key: "transaction_id", label: "شماره تراکنش" },
  { key: "date", label: "تاریخ تراکنش" },
  { key: "gross", label: "مبلغ ناخالص" },
  { key: "fee", label: "کارمزد" },
  { key: "vat", label: "مالیات کارمزد" },
  { key: "net", label: "مبلغ خالص" },
  { key: "deductions", label: "کسورات" },
  { key: "tracking_ref", label: "شماره پیگیری" },
  { key: "tran_type", label: "نوع تراکنش" },
  { key: "description", label: "شرح" },
];

export function settlementFieldLabel(field: string): string {
  return SETTLEMENT_MAPPING_FIELDS.find((f) => f.key === field)?.label ?? field;
}

/**
 * Mirror of the backend mapping validation
 * (`validate_settlement_mapping_columns`): detail files need
 * transaction_id + date + net or gross; payouts/statement files need
 * date + net. Returns the missing pieces as Persian messages (empty
 * when the mapping is complete).
 */
export function settlementMappingProblems(
  columns: Record<string, string>,
  fileType: string,
): string[] {
  const problems: string[] = [];
  if (fileType === "detail") {
    if (!columns["transaction_id"]) problems.push("شماره تراکنش الزامی است.");
    if (!columns["date"]) problems.push("تاریخ تراکنش الزامی است.");
    if (!columns["net"] && !columns["gross"])
      problems.push("مبلغ خالص یا مبلغ ناخالص الزامی است.");
  } else if (fileType === "payouts" || fileType === "statement") {
    if (!columns["date"]) problems.push("تاریخ تراکنش الزامی است.");
    if (!columns["net"]) problems.push("مبلغ خالص الزامی است.");
  } else {
    problems.push("نوع فایل نامعتبر است.");
  }
  return problems;
}

export function settlementMappingIsComplete(
  columns: Record<string, string>,
  fileType: string,
): boolean {
  return settlementMappingProblems(columns, fileType).length === 0;
}
