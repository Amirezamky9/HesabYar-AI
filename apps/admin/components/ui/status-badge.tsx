/**
 * Status badge (facade) — FarsiUI Badge underneath.
 *
 * Maps the API's English state strings to Persian labels and a tone.
 * Unknown states render as-is (outline) so a new backend state never
 * silently disappears from the UI.
 */

import type { VariantProps } from "class-variance-authority";
import { Badge, badgeVariants } from "./badge";

type BadgeVariant = VariantProps<typeof badgeVariants>["variant"];

export type StatusTone = "default" | "secondary" | "success" | "warning" | "destructive" | "outline";

const TONE_CLASS: Record<StatusTone, string> = {
  default: "",
  secondary: "",
  success: "border-transparent bg-success/10 text-success dark:bg-success/20",
  warning: "border-transparent bg-warning/15 text-warning-foreground dark:bg-warning/20",
  destructive: "",
  outline: "",
};

const TONE_VARIANT: Record<StatusTone, BadgeVariant> = {
  default: "default",
  secondary: "secondary",
  success: "secondary",
  warning: "secondary",
  destructive: "destructive",
  outline: "outline",
};

export interface StatusInfo {
  label: string;
  tone: StatusTone;
}

/** Known API states -> Persian label + tone. */
export const STATUS_MAP: Record<string, StatusInfo> = {
  // Journals / drafts
  draft: { label: "پیش‌نویس", tone: "secondary" },
  validated: { label: "تأییدشده", tone: "warning" },
  posted: { label: "ثبت‌شده", tone: "success" },
  cancelled: { label: "لغوشده", tone: "destructive" },
  reversed: { label: "برگشت‌خورده", tone: "outline" },
  // Invoices / payments
  paid: { label: "پرداخت‌شده", tone: "success" },
  open: { label: "باز", tone: "success" },
  closed: { label: "بسته", tone: "outline" },
  // Fiscal
  open_period: { label: "دوره باز", tone: "success" },
  closed_period: { label: "دوره بسته", tone: "outline" },
  // Generic
  active: { label: "فعال", tone: "success" },
  inactive: { label: "غیرفعال", tone: "outline" },
  pending: { label: "در انتظار", tone: "warning" },
  failed: { label: "ناموفق", tone: "destructive" },
  done: { label: "انجام‌شده", tone: "success" },
  ok: { label: "سالم", tone: "success" },
  error: { label: "خطا", tone: "destructive" },
  not_ready: { label: "آماده نیست", tone: "warning" },
  ready: { label: "آماده", tone: "success" },
};

export interface StatusBadgeProps {
  /** Raw API state string, e.g. "draft" / "posted". */
  status: string;
  /** Override the mapped Persian label. */
  label?: string;
  /** Override the mapped tone. */
  tone?: StatusTone;
  className?: string;
}

export function StatusBadge({ status, label, tone, className }: StatusBadgeProps) {
  const info = STATUS_MAP[status];
  const resolvedLabel = label ?? info?.label ?? status;
  const resolvedTone = tone ?? info?.tone ?? "outline";
  return (
    <Badge variant={TONE_VARIANT[resolvedTone]} className={`${TONE_CLASS[resolvedTone]} ${className ?? ""}`.trim()}>
      {resolvedLabel}
    </Badge>
  );
}
