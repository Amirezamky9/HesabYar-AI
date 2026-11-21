/**
 * Monitoring panel labels, form models and validation (US-704).
 *
 * Pure functions only — the panel renders from these, and the
 * vitest suite covers them without a browser. All user-facing text
 * is Persian; wire values (rule codes, statuses, provider keys)
 * stay Latin.
 */

import type {
  MonitoringAlertResponse,
  MonitoringChannelRequest,
  MonitoringChannelResponse,
  MonitoringProcessHealth,
  MonitoringTokenRecordRequest,
  MonitoringTokenRecordResponse,
} from "@/lib/api-client";
import { en, fa, faNumber } from "@/lib/utils";

/** Process names -> Persian labels (unknown names fall back to the raw name). */
export const PROCESS_LABELS: Record<string, string> = {
  api: "رابط برنامه‌نویسی",
  database: "پایگاه داده",
  tryton: "ترایتون",
  worker: "کارگر",
  mcp: "سرور MCP",
};

export function processLabelFa(name: string): string {
  return PROCESS_LABELS[name] ?? name;
}

/** Process statuses -> Persian labels. */
export const PROCESS_STATUS_LABELS: Record<string, string> = {
  ok: "سالم",
  degraded: "کاهش‌یافته",
  error: "خطا",
  unknown: "نامشخص",
};

export function processStatusLabelFa(status: string): string {
  return PROCESS_STATUS_LABELS[status] ?? status;
}

/** Alert rules -> Persian labels. */
export const ALERT_RULE_LABELS: Record<string, string> = {
  inbox_stale: "صندوق ورودی گیرکرده",
  worker_lag: "تأخیر کارگر",
  token_expiring: "انقضای توکن",
  settlement_drift: "مغایرت تسویه",
  webhook_failed: "وب‌هوک ناموفق",
  backup_failed: "پشتیبان‌گیری ناموفق",
  error_rate_5xx: "نرخ خطای ۵xx",
};

export function alertRuleLabelFa(rule: string): string {
  return ALERT_RULE_LABELS[rule] ?? rule;
}

/** Alert severities -> Persian labels. */
export const ALERT_SEVERITY_LABELS: Record<string, string> = {
  info: "اطلاع",
  warning: "هشدار",
  critical: "بحرانی",
};

export function alertSeverityLabelFa(severity: string): string {
  return ALERT_SEVERITY_LABELS[severity] ?? severity;
}

/** Alert lifecycle statuses -> Persian labels. */
export const ALERT_STATUS_LABELS: Record<string, string> = {
  open: "باز",
  acknowledged: "تأیید شده",
  resolved: "حل‌شده",
};

export function alertStatusLabelFa(status: string): string {
  return ALERT_STATUS_LABELS[status] ?? status;
}

/** Channel kinds -> Persian labels. */
export const CHANNEL_KIND_LABELS: Record<string, string> = {
  sms: "پیامک",
  email: "ایمیل",
  webhook: "وب‌هوک",
};

export function channelKindLabelFa(kind: string): string {
  return CHANNEL_KIND_LABELS[kind] ?? kind;
}

/** Backup statuses -> Persian labels. */
export const BACKUP_STATUS_LABELS: Record<string, string> = {
  ok: "سالم",
  failed: "ناموفق",
  stale: "قدیمی",
  unknown: "نامشخص",
};

export function backupStatusLabelFa(status: string): string {
  return BACKUP_STATUS_LABELS[status] ?? status;
}

/** Backed-up databases -> Persian labels. */
export const BACKUP_DATABASE_LABELS: Record<string, string> = {
  hesabyar: "حسابیار",
  tryton: "ترایتون",
};

export function backupDatabaseLabelFa(database: string): string {
  return BACKUP_DATABASE_LABELS[database] ?? database;
}

/** Token kinds -> Persian labels. */
export const TOKEN_KIND_LABELS: Record<string, string> = {
  api_key: "کلید API",
  fintech: "فینوتک",
  other: "سایر",
};

export function tokenKindLabelFa(kind: string): string {
  return TOKEN_KIND_LABELS[kind] ?? kind;
}

type ProcessStatus = "ok" | "degraded" | "error" | "unknown";

const STATUS_RANK: Record<ProcessStatus, number> = {
  ok: 0,
  unknown: 1,
  degraded: 2,
  error: 3,
};

/**
 * The worst status across the processes (error > degraded > unknown
 * > ok). An empty list is unknown — no processes checked is not a
 * clean bill of health.
 */
export function worstProcessStatus(processes: MonitoringProcessHealth[]): ProcessStatus {
  let worst: ProcessStatus = "ok";
  for (const process of processes) {
    const status = process.status as ProcessStatus;
    if (STATUS_RANK[status] === undefined) continue;
    if (STATUS_RANK[status] > STATUS_RANK[worst]) worst = status;
  }
  return processes.length === 0 ? "unknown" : worst;
}

/** Counts of firing (open/acknowledged) alerts by severity. */
export function alertCountBySeverity(alerts: MonitoringAlertResponse[]): {
  critical: number;
  warning: number;
  info: number;
} {
  const counts = { critical: 0, warning: 0, info: 0 };
  for (const alert of alerts) {
    if (alert.status === "resolved") continue;
    if (alert.severity === "critical") counts.critical += 1;
    else if (alert.severity === "warning") counts.warning += 1;
    else counts.info += 1;
  }
  return counts;
}

/**
 * Days-left text for a token expiry: «منقضی شده» past expiry,
 * «امروز» on the day, «X روز دیگر» otherwise.
 */
export function formatDaysLeft(daysLeft: number): string {
  if (daysLeft < 0) return `منقضی شده (${fa(Math.abs(daysLeft))} روز پیش)`;
  if (daysLeft === 0) return "امروز منقضی می‌شود";
  return `${fa(daysLeft)} روز دیگر`;
}

/** Rial amount with Persian digits: 1250000 -> ۱٬۲۵۰٬۰۰۰ ریال. */
export function formatRial(value: number): string {
  return `${faNumber(value)} ریال`;
}

/**
 * Ratio as a percent with one decimal, Persian digits + «٫»:
 * 0.05 -> ۵٫۰٪. (faPercent leaves a Latin dot for digits > 0.)
 */
export function formatPercent(value: number): string {
  return `${fa((value * 100).toFixed(1)).replace(".", "٫")}٪`;
}

/** Channel target labels + placeholders per kind (dialog copy). */
export const CHANNEL_TARGET_LABELS: Record<ChannelKind, string> = {
  sms: "شماره موبایل",
  email: "نشانی ایمیل",
  webhook: "نشانی وب‌هوک",
};

export const CHANNEL_TARGET_PLACEHOLDERS: Record<ChannelKind, string> = {
  sms: "09123456789",
  email: "ops@example.com",
  webhook: "https://hooks.example.com/alerts",
};

// ---------------------------------------------------------------------------
// Channel form
// ---------------------------------------------------------------------------

export type ChannelKind = "sms" | "email" | "webhook";

export interface ChannelFormValues {
  kind: ChannelKind;
  name: string;
  target: string;
  enabled: boolean;
  minSeverity: "info" | "warning" | "critical";
  providerKey: string;
}

export type ChannelFormErrors = Partial<Record<"name" | "target" | "providerKey", string>>;

export function emptyChannelForm(): ChannelFormValues {
  return {
    kind: "webhook",
    name: "",
    target: "",
    enabled: true,
    minSeverity: "warning",
    providerKey: "",
  };
}

export function channelFormFromExisting(
  channel: MonitoringChannelResponse,
): ChannelFormValues {
  return {
    kind: channel.kind,
    name: channel.name,
    target: channel.target,
    enabled: channel.enabled,
    minSeverity: channel.min_severity,
    providerKey: channel.provider_key ?? "",
  };
}

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const MOBILE_RE = /^09\d{9}$/;

/** Mirror of the backend's channel validation (docs/monitoring.md). */
export function validateChannelForm(values: ChannelFormValues): ChannelFormErrors {
  const errors: ChannelFormErrors = {};
  if (!values.name.trim()) errors.name = "نام کانال الزامی است.";
  else if (values.name.trim().length > 128) errors.name = "نام کانال حداکثر ۱۲۸ نویسه است.";
  const target = values.target.trim();
  if (!target) {
    errors.target = "مقصد الزامی است.";
  } else if (values.kind === "sms") {
    const digits = en(target).replace(/[\s-]/g, "");
    if (!MOBILE_RE.test(digits)) {
      errors.target = "شماره موبایل باید ۱۱ رقم باشد و با ۰۹ شروع شود.";
    }
  } else if (values.kind === "email") {
    if (!EMAIL_RE.test(target)) errors.target = "نشانی ایمیل معتبر نیست.";
  } else {
    try {
      const parsed = new URL(target);
      if (parsed.protocol !== "https:" || !parsed.host) {
        errors.target = "نشانی وب‌هوک باید یک URL با https باشد.";
      }
    } catch {
      errors.target = "نشانی وب‌هوک باید یک URL با https باشد.";
    }
  }
  if (values.kind === "sms" && !values.providerKey.trim()) {
    errors.providerKey = "کانال پیامکی به ارائه‌دهنده نیاز دارد.";
  }
  return errors;
}

export function channelFormToRequest(values: ChannelFormValues): MonitoringChannelRequest {
  const target =
    values.kind === "sms"
      ? en(values.target.trim()).replace(/[\s-]/g, "")
      : values.target.trim();
  return {
    kind: values.kind,
    name: values.name.trim(),
    target,
    enabled: values.enabled,
    min_severity: values.minSeverity,
    provider_key: values.kind === "sms" ? values.providerKey.trim() : null,
    installation_id: null, // defaults to the caller's installation server-side
  };
}

// ---------------------------------------------------------------------------
// Token-record form
// ---------------------------------------------------------------------------

export type TokenKind = "fintech" | "other";

export interface TokenFormValues {
  kind: TokenKind;
  label: string;
  /** Date input value (Gregorian yyyy-mm-dd, the native input's shape). */
  expiresDate: string;
  rotationRunbookRef: string;
  enabled: boolean;
  notes: string;
}

export type TokenFormErrors = Partial<Record<"label" | "expiresDate" | "notes", string>>;

export function emptyTokenForm(): TokenFormValues {
  return {
    kind: "fintech",
    label: "",
    expiresDate: "",
    rotationRunbookRef: "",
    enabled: true,
    notes: "",
  };
}

export function tokenFormFromExisting(
  record: MonitoringTokenRecordResponse,
): TokenFormValues {
  return {
    kind: record.kind,
    label: record.label,
    expiresDate: record.expires_at.slice(0, 10),
    rotationRunbookRef: record.rotation_runbook_ref ?? "",
    enabled: record.enabled,
    notes: record.notes ?? "",
  };
}

/** Mirror of the backend's token-record validation. */
export function validateTokenForm(values: TokenFormValues): TokenFormErrors {
  const errors: TokenFormErrors = {};
  if (!values.label.trim()) errors.label = "برچسب توکن الزامی است.";
  else if (values.label.trim().length > 128) errors.label = "برچسب حداکثر ۱۲۸ نویسه است.";
  if (!values.expiresDate) {
    errors.expiresDate = "تاریخ انقضا الزامی است.";
  } else {
    const expires = new Date(`${values.expiresDate}T23:59:59Z`);
    if (Number.isNaN(expires.getTime())) {
      errors.expiresDate = "تاریخ انقضا معتبر نیست.";
    } else if (expires.getTime() < Date.now()) {
      errors.expiresDate = "تاریخ انقضا نمی‌تواند در گذشته باشد.";
    }
  }
  if (values.notes.trim().length > 1024) errors.notes = "یادداشت حداکثر ۱۰۲۴ نویسه است.";
  return errors;
}

export function tokenFormToRequest(values: TokenFormValues): MonitoringTokenRecordRequest {
  return {
    kind: values.kind,
    label: values.label.trim(),
    // End of the chosen day (UTC): the token is valid through that day.
    expires_at: `${values.expiresDate}T23:59:59Z`,
    rotation_runbook_ref: values.rotationRunbookRef.trim() || null,
    enabled: values.enabled,
    notes: values.notes.trim() || null,
  };
}
