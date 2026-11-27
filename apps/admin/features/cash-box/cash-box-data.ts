/**
 * Persian labels + helpers for the cash box (US-516, Phase 5-2 K2).
 *
 * Every cash-box kind, session status, variance policy and count
 * schedule the API returns gets one Persian label. Unknown values
 * fall back to the raw code so a new backend label never silently
 * disappears. Money is integer IRR on the wire; the UI shows Toman
 * (display-only, ×10 conversion at the boundary).
 */

import type { StatusTone } from "@/components/ui";
import { faNumber } from "@/lib/utils";

/** The three cash-box kinds. */
export const CASH_BOX_KINDS = ["store", "petty_cash", "other"] as const;

export type CashBoxKindCode = (typeof CASH_BOX_KINDS)[number];

export const CASH_BOX_KIND_LABELS: Record<string, { label: string; description: string }> = {
  store: { label: "صندوق فروشگاه", description: "صندوق نقدی فروشگاه" },
  petty_cash: { label: "تنخواه", description: "صندوق تنخواه‌گردان با سقف مشخص" },
  other: { label: "صندوق دیگر", description: "هر صندوق نقدی دیگر" },
};

export function cashBoxKindLabel(kind: string | null | undefined): string {
  if (!kind) return "نامشخص";
  return CASH_BOX_KIND_LABELS[kind]?.label ?? kind;
}

export function cashBoxKindDescription(kind: string | null | undefined): string | null {
  if (!kind) return null;
  return CASH_BOX_KIND_LABELS[kind]?.description ?? null;
}

/** The four counting-session statuses (phased design هـ.۲.۲). */
export const COUNT_SESSION_STATUSES = [
  "in_progress",
  "awaiting_supervisor",
  "awaiting_manager",
  "archived",
] as const;

export type CountSessionStatusCode = (typeof COUNT_SESSION_STATUSES)[number];

export const COUNT_SESSION_STATUS_LABELS: Record<string, { label: string; tone: StatusTone }> = {
  in_progress: { label: "در جریان", tone: "outline" },
  awaiting_supervisor: { label: "در انتظار ناظر", tone: "warning" },
  awaiting_manager: { label: "در انتظار تأیید مدیر", tone: "warning" },
  archived: { label: "بایگانی‌شده", tone: "success" },
};

export function sessionStatusLabel(status: string | null | undefined): string {
  if (!status) return "نامشخص";
  return COUNT_SESSION_STATUS_LABELS[status]?.label ?? status;
}

export function sessionStatusTone(status: string | null | undefined): StatusTone {
  if (!status) return "outline";
  return COUNT_SESSION_STATUS_LABELS[status]?.tone ?? "outline";
}

/** The two variance policies (phased design هـ.۲.۲). */
export const VARIANCE_POLICIES = ["expense", "receivable_from_cashier"] as const;

export type VariancePolicyCode = (typeof VARIANCE_POLICIES)[number];

export const VARIANCE_POLICY_LABELS: Record<string, { label: string; description: string }> = {
  expense: {
    label: "حساب کسر و اضافات صندوق",
    description: "سیاست (الف): کسری و اضافه هر دو در حساب «کسر و اضافات صندوق» ثبت می‌شوند و پایان دوره بسته می‌شوند.",
  },
  receivable_from_cashier: {
    label: "بدهکار موقت — کسر از صندوقدار",
    description: "سیاست (ب): کسری به بدهکار موقت صندوقدار و اضافه به بستانکار متفرقه می‌رود؛ کسری باید ظرف ۲۴ ساعت تسویه شود.",
  },
};

export function variancePolicyLabel(policy: string | null | undefined): string {
  if (!policy) return "نامشخص";
  return VARIANCE_POLICY_LABELS[policy]?.label ?? policy;
}

export function variancePolicyDescription(policy: string | null | undefined): string | null {
  if (!policy) return null;
  return VARIANCE_POLICY_LABELS[policy]?.description ?? null;
}

/** The three count schedules. */
export const COUNT_SCHEDULES = ["daily", "weekly", "period_end"] as const;

export const COUNT_SCHEDULE_LABELS: Record<string, string> = {
  daily: "روزانه",
  weekly: "هفتگی",
  period_end: "پایان دوره",
};

export function countScheduleLabel(schedule: string | null | undefined): string {
  if (!schedule) return "نامشخص";
  return COUNT_SCHEDULE_LABELS[schedule] ?? schedule;
}

/** The three signature roles on a session's minutes. */
export const SIGN_ROLES = ["cashier", "supervisor", "manager"] as const;

export const SIGN_ROLE_LABELS: Record<string, string> = {
  cashier: "صندوقدار",
  supervisor: "ناظر",
  manager: "مدیر",
};

export function signRoleLabel(role: string | null | undefined): string {
  if (!role) return "نامشخص";
  return SIGN_ROLE_LABELS[role] ?? role;
}

/** Variance direction: shortage / overage / balanced. */
export type VarianceDirection = "shortage" | "overage" | "balanced" | "unknown";

export function varianceDirection(varianceIrr: number | null | undefined): VarianceDirection {
  if (varianceIrr === null || varianceIrr === undefined) return "unknown";
  if (varianceIrr < 0) return "shortage";
  if (varianceIrr > 0) return "overage";
  return "balanced";
}

export const VARIANCE_DIRECTION_LABELS: Record<VarianceDirection, { label: string; tone: StatusTone }> = {
  shortage: { label: "کسری", tone: "destructive" },
  overage: { label: "اضافه", tone: "warning" },
  balanced: { label: "برابر", tone: "success" },
  unknown: { label: "شمارش نشده", tone: "outline" },
};

export function varianceDirectionLabel(varianceIrr: number | null | undefined): string {
  return VARIANCE_DIRECTION_LABELS[varianceDirection(varianceIrr)].label;
}

export function varianceDirectionTone(varianceIrr: number | null | undefined): StatusTone {
  return VARIANCE_DIRECTION_LABELS[varianceDirection(varianceIrr)].tone;
}

/** «۱۰ روز پیش» / «امروز» / «دیروز» / «هرگز شمارش نشده». */
export function daysSinceLabel(days: number | null | undefined): string {
  if (days === null || days === undefined) return "هرگز شمارش نشده";
  if (days <= 0) return "امروز";
  if (days === 1) return "دیروز";
  return `${faNumber(days)} روز پیش`;
}

/** Denomination breakdown total (IRR) for a list of lines. */
export function denominationsTotal(
  lines: { denomination_irr: number; count: number }[],
): number {
  return lines.reduce((sum, line) => sum + line.denomination_irr * line.count, 0);
}
