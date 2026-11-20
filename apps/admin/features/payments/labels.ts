/**
 * Payment labels (US-304).
 *
 * Payment states from the API (schema payments.py): draft, submitted,
 * approved, processing, succeeded, failed. Only draft/failed exist in
 * the facade STATUS_MAP, so the rest are mapped here with tones.
 */

import type { StatusTone } from "@/components/ui";

/** Payment states the API accepts as a filter (docs/rest-api.md). */
export const PAYMENT_STATES = ["draft", "submitted", "approved", "processing", "succeeded", "failed"] as const;

export const PAYMENT_STATE_LABELS: Record<string, { label: string; tone: StatusTone }> = {
  draft: { label: "پیش‌نویس", tone: "secondary" },
  submitted: { label: "ارسال‌شده", tone: "warning" },
  approved: { label: "تأییدشده", tone: "warning" },
  processing: { label: "در حال پردازش", tone: "warning" },
  succeeded: { label: "موفق", tone: "success" },
  failed: { label: "ناموفق", tone: "destructive" },
};

/** Payment kinds: 'receivable' (money in) / 'payable' (money out). */
export const PAYMENT_KINDS = ["receivable", "payable"] as const;

export const PAYMENT_KIND_LABELS: Record<string, string> = {
  receivable: "دریافتی",
  payable: "پرداختی",
};
