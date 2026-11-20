/**
 * Persian labels + pure helpers for the cheque center (US-535, Phase 5-4).
 *
 * Every cheque state, kind, purpose, storage location, Sayyad status,
 * inquiry channel/type and Fintech scope the API returns gets one
 * Persian label (from phased design §5). Unknown values fall back to
 * the raw code so a new backend value never silently disappears.
 *
 * Pure helpers (Jalali date math, due buckets, clearing aging,
 * intake reminders, the 12-week cash forecast, lifecycle timeline)
 * live here so the reports and the pages share them — and so vitest
 * can cover them without a backend.
 *
 * Dates: the API travels Jalali "YYYY-MM-DD" strings (zero-padded,
 * `to_jalali_string`), so lexicographic comparison orders them and
 * `jalaliToIso` converts them to Gregorian for day arithmetic.
 */

import type { StatusTone } from "@/components/ui";
import type { ChequeResponse } from "@/lib/api-client";
import { fa, faNumber } from "@/lib/utils";
import { JALALI_MONTHS, isoToJalali, jalaliToIso, todayIso } from "@/lib/jalali";

/** The 12 accounting states (§5 §2.1–§2.3), in lifecycle order. */
export const CHEQUE_STATES = [
  "registered",
  "held",
  "in_clearing",
  "cleared",
  "bounced",
  "returned",
  "endorsed",
  "expired_unreceived",
  "rejected_mismatch",
  "issued",
  "voided",
  "called_in",
] as const;

export const CHEQUE_STATE_LABELS: Record<string, { label: string; tone: StatusTone }> = {
  registered: { label: "ثبت‌شده (در انتظار برگ)", tone: "warning" },
  held: { label: "نزد ما", tone: "outline" },
  in_clearing: { label: "در جریان وصول", tone: "warning" },
  cleared: { label: "وصول‌شده", tone: "success" },
  bounced: { label: "برگشتی", tone: "destructive" },
  returned: { label: "مستردشده", tone: "outline" },
  endorsed: { label: "ظهرنویسی‌شده", tone: "outline" },
  expired_unreceived: { label: "منقضی — برگ نرسید", tone: "destructive" },
  rejected_mismatch: { label: "ردشده — مغایرت", tone: "destructive" },
  issued: { label: "صادرشده", tone: "outline" },
  voided: { label: "باطل‌شده", tone: "outline" },
  called_in: { label: "فراخوان‌شده", tone: "warning" },
};

export function chequeStateLabel(state: string | null | undefined): string {
  if (!state) return "نامشخص";
  return CHEQUE_STATE_LABELS[state]?.label ?? state;
}

export function chequeStateTone(state: string | null | undefined): StatusTone {
  if (!state) return "outline";
  return CHEQUE_STATE_LABELS[state]?.tone ?? "outline";
}

/** States in which a cheque still represents money in motion. */
export function isChequeOpen(state: string): boolean {
  return state === "registered" || state === "held" || state === "in_clearing" || state === "issued";
}

export const CHEQUE_KIND_LABELS: Record<string, string> = {
  received: "دریافتی",
  issued: "پرداختی",
};

export function chequeKindLabel(kind: string | null | undefined): string {
  if (!kind) return "نامشخص";
  return CHEQUE_KIND_LABELS[kind] ?? kind;
}

export const CHEQUE_PURPOSE_LABELS: Record<string, string> = {
  payment: "تجاری",
  collateral: "پشتوانه",
};

export function chequePurposeLabel(purpose: string | null | undefined): string {
  if (!purpose) return "نامشخص";
  return CHEQUE_PURPOSE_LABELS[purpose] ?? purpose;
}

export const STORAGE_LOCATION_LABELS: Record<string, string> = {
  cashbox: "صندوق",
  vault: "خزانه",
  bank: "بانک",
  with_endorsee: "نزد ظهرنویس",
  returned_to_issuer: "مستردشده به صادرکننده",
};

export function storageLocationLabel(location: string | null | undefined): string {
  if (!location) return "ثبت نشده";
  return STORAGE_LOCATION_LABELS[location] ?? location;
}

export const SAYAD_STATUS_LABELS: Record<string, string> = {
  unregistered: "ثبت‌نشده در صیاد",
  registered: "ثبت‌شده در صیاد",
  confirmed: "تأییدشده",
  transferred: "منتقل‌شده",
};

export function sayadStatusLabel(status: string | null | undefined): string {
  if (!status) return "نامشخص";
  return SAYAD_STATUS_LABELS[status] ?? status;
}

/** Issuer colour 1–5 (1=white … 5=red); colour is history only, never a guarantee. */
export const ISSUER_COLOR_LABELS: Record<number, string> = {
  1: "سفید",
  2: "زرد",
  3: "نارنجی",
  4: "قهوه‌ای",
  5: "قرمز",
};

export function issuerColorLabel(code: number | null | undefined): string {
  if (code === null || code === undefined) return "استعلام نشده";
  return ISSUER_COLOR_LABELS[code] ?? `رنگ ${faNumber(code)}`;
}

export const INQUIRY_CHANNEL_LABELS: Record<string, string> = {
  sms: "پیامک (۴۰۴۰۷۰۱۷۰۱)",
  cbi_website: "سایت بانک مرکزی",
  bank_app: "اپلیکیشن بانک",
  payment_app: "اپلیکیشن پرداخت",
  finnotech_api: "فینوتک (API)",
  ehrazino_api: "احرازینو (API)",
};

export function inquiryChannelLabel(channel: string | null | undefined): string {
  if (!channel) return "نامشخص";
  return INQUIRY_CHANNEL_LABELS[channel] ?? channel;
}

export const INQUIRY_TYPE_LABELS: Record<string, string> = {
  color: "رنگ چک",
  leaf_status: "وضعیت برگ",
  items_match: "تطبیق اقلام",
  issuer_history: "سابقهٔ صادرکننده",
  back_cheques: "چک‌های برگشتی صادرکننده",
  transfer_chain: "زنجیرهٔ انتقال",
};

export function inquiryTypeLabel(type: string | null | undefined): string {
  if (!type) return "نامشخص";
  return INQUIRY_TYPE_LABELS[type] ?? type;
}

export const CLEARING_CONFIDENCE_LABELS: Record<string, string> = {
  exact: "دقیق",
  probable: "محتمل",
  ambiguous: "مبهم",
  unmatched: "بی‌تطبیق",
};

export function clearingConfidenceLabel(confidence: string | null | undefined): string {
  if (!confidence) return "—";
  return CLEARING_CONFIDENCE_LABELS[confidence] ?? confidence;
}

/** The 18 documented Fintech Sayyad scopes (domain/cheques/sayyad.py). */
export const FINNOTECH_SAYYAD_SCOPES: { scope: string; label: string }[] = [
  { scope: "credit:cheque-status-inquiry:post", label: "وضعیت برگ" },
  { scope: "credit:sayad-serial-inquiry:get", label: "تطبیق اقلام (سریال)" },
  { scope: "credit:sayad-transfers-chain-inquiry:post", label: "زنجیرهٔ انتقال" },
  { scope: "credit:cheque-color-inquiry:get", label: "رنگ چک (حقیقی)" },
  { scope: "credit:cheque-color-inquiry-legal:get", label: "رنگ چک (حقوقی)" },
  { scope: "credit:back-cheques:get", label: "چک‌های برگشتی (حقیقی)" },
  { scope: "credit:back-cheques-legal:get", label: "چک‌های برگشتی (حقوقی)" },
  { scope: "credit:chequebook-inquiry:get", label: "استعلام دسته‌چک" },
  { scope: "credit:ac-sayad-issue-cheque:post", label: "صدور در صیاد" },
  { scope: "credit:ac-sayad-transfer-cheque:post", label: "انتقال در صیاد" },
  { scope: "credit:sms-sayad-accept-cheque:post", label: "پذیرش پیامکی" },
  { scope: "credit:sms-sayad-transfer-cheque:post", label: "انتقال پیامکی" },
  { scope: "credit:sms-sayad-cancel-cheque:post", label: "لغو پیامکی" },
  { scope: "credit:sms-cheque-color-inquiry:get", label: "رنگ پیامکی" },
  { scope: "credit:sms-cheque-status-inquiry:post", label: "وضعیت پیامکی" },
  { scope: "credit:sms-sayad-transfers-chain-inquiry:post", label: "زنجیرهٔ انتقال پیامکی" },
  { scope: "credit:sms-sayady-cheque-inquiry:get", label: "استعلام صیادی پیامکی" },
  { scope: "credit:sms-sayad-issuer-inquiry-cheque:post", label: "استعلام صادرکنندهٔ پیامکی" },
];

// ---------------------------------------------------------------------------
// Jalali date helpers (API dates are Jalali YYYY-MM-DD, zero-padded)
// ---------------------------------------------------------------------------

const JALALI_RE = /^(\d{4})-(\d{2})-(\d{2})$/;

/** True for a well-formed Jalali "YYYY-MM-DD" string. Never throws. */
export function isValidJalaliString(value: unknown): value is string {
  if (typeof value !== "string") return false;
  const match = JALALI_RE.exec(value);
  if (!match) return false;
  const [, y, m, d] = match;
  const month = Number(m);
  const day = Number(d);
  if (month < 1 || month > 12 || day < 1 || day > 31) return false;
  try {
    jalaliToIso(Number(y), month, day);
    return true;
  } catch {
    return false;
  }
}

/** Today's Jalali date as "YYYY-MM-DD" (Asia/Tehran). */
export function todayJalaliString(): string {
  const { jy, jm, jd } = isoToJalali(todayIso("Asia/Tehran"));
  return `${jy}-${String(jm).padStart(2, "0")}-${String(jd).padStart(2, "0")}`;
}

/** Gregorian ISO "YYYY-MM-DD" -> Jalali "YYYY-MM-DD" (for `due_before` filters). */
export function isoToJalaliString(iso: string): string {
  const { jy, jm, jd } = isoToJalali(iso);
  return `${jy}-${String(jm).padStart(2, "0")}-${String(jd).padStart(2, "0")}`;
}

/** Whole days from `from` to `to` (Jalali strings); negative when `to` is earlier. */
export function jalaliDaysBetween(from: string, to: string): number {
  const [fy, fm, fd] = from.split("-").map(Number);
  const [ty, tm, td] = to.split("-").map(Number);
  const fromIso = jalaliToIso(fy, fm, fd);
  const toIso = jalaliToIso(ty, tm, td);
  return Math.round((Date.parse(`${toIso}T00:00:00Z`) - Date.parse(`${fromIso}T00:00:00Z`)) / 86_400_000);
}

/** Days until the due date (negative = overdue). */
export function daysToDue(dueJalali: string, todayJalali: string): number {
  return jalaliDaysBetween(todayJalali, dueJalali);
}

/** «۱۴۰۵/۰۸/۲۸» (numeric, Persian digits). Falls back to the raw string. */
export function formatJalaliShort(jalali: string | null | undefined): string {
  if (!jalali) return "—";
  if (!isValidJalaliString(jalali)) return jalali;
  const [y, m, d] = jalali.split("-");
  return fa(`${y}/${m}/${d}`);
}

/** «۲۸ آبان ۱۴۰۵» (long, Persian digits). Falls back to the raw string. */
export function formatJalaliLong(jalali: string | null | undefined): string {
  if (!jalali) return "—";
  if (!isValidJalaliString(jalali)) return jalali;
  const [y, m, d] = jalali.split("-").map(Number);
  return `${fa(d)} ${JALALI_MONTHS[m - 1]} ${fa(y)}`;
}

// ---------------------------------------------------------------------------
// Due buckets (report 1: ۷ / ۱۴ / ۳۰ روز + گذشته)
// ---------------------------------------------------------------------------

export type DueBucket = "overdue" | "due-7" | "due-14" | "due-30" | "future";

export const DUE_BUCKET_LABELS: Record<DueBucket, string> = {
  overdue: "سررسیدگذشته",
  "due-7": "تا ۷ روز آینده",
  "due-14": "۸ تا ۱۴ روز آینده",
  "due-30": "۱۵ تا ۳۰ روز آینده",
  future: "بیش از ۳۰ روز آینده",
};

export const DUE_BUCKETS: DueBucket[] = ["overdue", "due-7", "due-14", "due-30", "future"];

export function dueBucket(dueJalali: string, todayJalali: string): DueBucket {
  const days = daysToDue(dueJalali, todayJalali);
  if (days < 0) return "overdue";
  if (days <= 7) return "due-7";
  if (days <= 14) return "due-14";
  if (days <= 30) return "due-30";
  return "future";
}

// ---------------------------------------------------------------------------
// Clearing aging buckets (report 4: ۰–۷ / ۸–۱۴ / +۱۴ روز)
// ---------------------------------------------------------------------------

export type ClearingAgeBucket = "0-7" | "8-14" | "15+";

export const CLEARING_AGE_BUCKET_LABELS: Record<ClearingAgeBucket, string> = {
  "0-7": "۰ تا ۷ روز",
  "8-14": "۸ تا ۱۴ روز",
  "15+": "بیش از ۱۴ روز",
};

export const CLEARING_AGE_BUCKETS: ClearingAgeBucket[] = ["0-7", "8-14", "15+"];

export function clearingAgeBucket(daysInClearing: number): ClearingAgeBucket {
  if (daysInClearing <= 7) return "0-7";
  if (daysInClearing <= 14) return "8-14";
  return "15+";
}

// ---------------------------------------------------------------------------
// Intake reminders (report 11): day-3 + day-(N-1), expiry at the deadline
// ---------------------------------------------------------------------------

export type IntakeReminderState = "waiting" | "day-3" | "day-n-minus-1" | "expired";

export const INTAKE_REMINDER_LABELS: Record<IntakeReminderState, string> = {
  waiting: "در انتظار برگ",
  "day-3": "یادآوری روز ۳",
  "day-n-minus-1": "یادآوری روز پایانی",
  expired: "منقضی — برگ نرسید",
};

export function intakeReminderState(ageDays: number, deadlineDays: number): IntakeReminderState {
  if (ageDays >= deadlineDays) return "expired";
  if (ageDays >= deadlineDays - 1) return "day-n-minus-1";
  if (ageDays >= 3) return "day-3";
  return "waiting";
}

// ---------------------------------------------------------------------------
// Totals + grouping
// ---------------------------------------------------------------------------

export function sumIrr(cheques: Pick<ChequeResponse, "amount_irr">[]): number {
  return cheques.reduce((total, cheque) => total + cheque.amount_irr, 0);
}

export function countBy<T extends string>(rows: { state: T }[] | { kind: T }[]): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const row of rows) {
    const key = "state" in row ? row.state : row.kind;
    counts[key] = (counts[key] ?? 0) + 1;
  }
  return counts;
}

// ---------------------------------------------------------------------------
// Cash forecast (report 14): expected inflows vs outflows, 12 weeks.
// Collateral never enters the forecast (§5 §2.2).
// ---------------------------------------------------------------------------

export interface ForecastWeek {
  /** Jalali week-start "YYYY-MM-DD". */
  weekStart: string;
  /** Expected inflow: received commercial cheques held / in clearing. */
  inflowIrr: number;
  /** Expected outflow: issued commercial cheques still outstanding. */
  outflowIrr: number;
  inflowCount: number;
  outflowCount: number;
}

export function addJalaliDays(jalali: string, days: number): string {
  const [y, m, d] = jalali.split("-").map(Number);
  const iso = jalaliToIso(y, m, d);
  const shifted = new Date(Date.parse(`${iso}T00:00:00Z`) + days * 86_400_000);
  const { jy, jm, jd } = isoToJalali(shifted.toISOString().slice(0, 10));
  return `${jy}-${String(jm).padStart(2, "0")}-${String(jd).padStart(2, "0")}`;
}

export function cashForecast(cheques: ChequeResponse[], todayJalali: string): ForecastWeek[] {
  const weeks: ForecastWeek[] = Array.from({ length: 12 }, (_, i) => ({
    weekStart: addJalaliDays(todayJalali, i * 7),
    inflowIrr: 0,
    outflowIrr: 0,
    inflowCount: 0,
    outflowCount: 0,
  }));
  for (const cheque of cheques) {
    if (cheque.purpose !== "payment") continue;
    const dayOffset = jalaliDaysBetween(todayJalali, cheque.due_date);
    if (dayOffset < 0 || dayOffset >= 84) continue;
    const week = weeks[Math.floor(dayOffset / 7)];
    if (cheque.kind === "received" && (cheque.state === "held" || cheque.state === "in_clearing")) {
      week.inflowIrr += cheque.amount_irr;
      week.inflowCount += 1;
    } else if (cheque.kind === "issued" && cheque.state === "issued") {
      week.outflowIrr += cheque.amount_irr;
      week.outflowCount += 1;
    }
  }
  return weeks;
}

// ---------------------------------------------------------------------------
// Lifecycle timeline (detail page): derived from state + recorded dates.
// A step is "done" when the cheque has passed it; the current state is
// the last done step. No step is invented: dates come from the record.
// ---------------------------------------------------------------------------

export interface LifecycleStep {
  key: string;
  label: string;
  done: boolean;
  /** Jalali date when known (due/issue/bounce/deposit). */
  date: string | null;
}

export function lifecycleSteps(cheque: ChequeResponse): LifecycleStep[] {
  const state = cheque.state;
  const terminal: Record<string, string> = {
    bounced: "برگشتی",
    returned: "مستردشده",
    endorsed: "ظهرنویسی‌شده",
    expired_unreceived: "منقضی — برگ نرسید",
    rejected_mismatch: "ردشده — مغایرت",
    voided: "باطل‌شده",
    called_in: "فراخوان‌شده",
  };
  const steps: LifecycleStep[] = [{ key: "registered", label: "ثبت‌شده", done: true, date: null }];
  if (cheque.kind === "issued") {
    steps.push({
      key: "issued",
      label: "صادرشده",
      done: state === "issued" || state === "voided" || state === "called_in" || state === "cleared",
      date: cheque.issue_date,
    });
  } else {
    steps.push(
      {
        key: "held",
        label: "برگ رسید — نزد ما",
        done: state !== "registered" && state !== "expired_unreceived" && state !== "rejected_mismatch",
        date: null,
      },
      {
        key: "in_clearing",
        label: "در جریان وصول",
        done: state === "in_clearing" || state === "cleared" || state === "bounced",
        date: cheque.deposit_date,
      },
      {
        key: "cleared",
        label: "وصول‌شده",
        done: state === "cleared",
        date: state === "cleared" ? cheque.due_date : null,
      },
    );
  }
  if (terminal[state]) {
    steps.push({ key: state, label: terminal[state], done: true, date: cheque.bounce_date });
  }
  return steps;
}

// ---------------------------------------------------------------------------
// The 17 cheque reports (§7.5) — catalog for the reports page.
// `backend` names the real endpoint (or "derived" for client-side
// aggregation over the registry, or "none" for the one honest
// coming-soon report). `chart` is the §7.5 chart type.
// ---------------------------------------------------------------------------

export interface ChequeReportMeta {
  id: string;
  number: number;
  title: string;
  /** §7.5 page. */
  page: string;
  chart: string;
  source: string;
  backend: "registry" | "endpoint" | "derived" | "none";
  filters: string;
}

export const CHEQUE_REPORTS: ChequeReportMeta[] = [
  { id: "due-received", number: 1, title: "دریافتی‌های سررسید نزدیک/گذشته", page: "گزارش‌ها ← چک ← سررسید", chart: "میله‌ای سررسید (۷/۱۴/۳۰ روز + گذشته)", source: "حساب‌یار", backend: "registry", filters: "بازهٔ سررسید، طرف، بانک، وضعیت" },
  { id: "issued", number: 2, title: "پرداختی‌های سررسید آینده («چقدر چک دادیم»)", page: "گزارش‌ها ← چک ← پرداختی", chart: "میله‌ای ماهانهٔ خروجی موردانتظار", source: "حساب‌یار", backend: "registry", filters: "بازهٔ سررسید، بانک، دسته‌چک" },
  { id: "bounced", number: 3, title: "چک‌های برگشتی", page: "گزارش‌ها ← چک ← برگشتی", chart: "دونات دلیل برگشت؛ خط روند ماهانهٔ برگشت", source: "حساب‌یار", backend: "registry", filters: "طرف، بانک، دلیل برگشت، بازه" },
  { id: "in-clearing", number: 4, title: "در جریان وصول", page: "گزارش‌ها ← چک ← کلر", chart: "میله‌ای قدمت (۰–۷ / ۸–۱۴ / +۱۴ روز)", source: "حساب‌یار", backend: "endpoint", filters: "بانک، قدمت (روزهای در کلر)" },
  { id: "endorsed", number: 5, title: "خرج‌شده/واگذارشده", page: "گزارش‌ها ← چک ← ظهرنویسی", chart: "جدول + زنجیرهٔ انتقال", source: "حساب‌یار", backend: "endpoint", filters: "طرف اولیه، گیرندهٔ نهایی، بازه" },
  { id: "collateral", number: 6, title: "چک‌های پشتوانه («چقدر چک پشتوانه کردیم»)", page: "گزارش‌ها ← چک ← پشتوانه", chart: "دونات دریافتی/پرداختی؛ میله‌ای ماندهٔ انتظامی", source: "حساب‌یار", backend: "endpoint", filters: "دریافتی/پرداختی، طرف، وضعیت فراخوان، تعهد تحت پوشش" },
  { id: "notes-balance", number: 7, title: "ماندهٔ اسناد دریافتنی/پرداختنی", page: "گزارش‌های مالی ← اسناد", chart: "میله‌ای aging اسناد (۳۰/۶۰/۹۰/۱۲۰+)", source: "ترایتون (مرجع)", backend: "endpoint", filters: "طرف، سررسید، نوع سند" },
  { id: "checkbooks", number: 8, title: "صورت وضعیت دسته‌چک", page: "چک‌ها ← دسته‌چک‌ها", chart: "دونات برگ (مصرف‌شده/باطل‌شده/باقی‌مانده)", source: "هر دو", backend: "endpoint", filters: "دسته‌چک، وضعیت برگ" },
  { id: "registry", number: 9, title: "دفتر چک", page: "گزارش‌ها ← چک ← دفتر چک", chart: "جدول قابل‌فیلتر + خروجی", source: "حساب‌یار", backend: "registry", filters: "وضعیت، طرف، بانک، بازهٔ سررسید، مبدأ، وضعیت صیاد، محل نگهداری" },
  { id: "mismatches", number: 10, title: "مغایرت سایت ↔ صیاد ↔ برگ", page: "گزارش‌ها ← چک ← مغایرت‌ها", chart: "میله‌ای تعداد مغایرت به تفکیک نوع", source: "حساب‌یار", backend: "derived", filters: "نوع مغایرت، قدمت، وضعیت رسیدگی" },
  { id: "unreceived", number: 11, title: "چک‌های سایت و نرسیده", page: "گزارش‌ها ← چک ← نرسیده", chart: "میله‌ای قدمت (روز ۳ / N−1 / منقضی)", source: "حساب‌یار", backend: "endpoint", filters: "قدمت، اپراتور سایت، وضعیت" },
  { id: "clearing-match", number: 12, title: "تطبیق وصول", page: "گزارش‌ها ← چک ← تطبیق", chart: "دونات نرخ exact/probable/ambiguous/unmatched؛ خط روند نرخ تطبیق ماهانه", source: "هر دو", backend: "endpoint", filters: "بانک، دوره، نتیجهٔ تطبیق" },
  { id: "inquiry-ledger", number: 13, title: "دفتر استعلام‌ها", page: "گزارش‌ها ← چک ← استعلام‌ها", chart: "دونات توزیع رنگ؛ میله‌ای هزینهٔ تجمیعی به تفکیک کانال", source: "حساب‌یار", backend: "endpoint", filters: "کانال، رنگ، بازه" },
  { id: "cash-forecast", number: 14, title: "پیش‌بینی نقدینگی چک", page: "داشبورد خزانه‌داری ← پیش‌بینی + گزارش‌ها ← چک ← پیش‌بینی نقد", chart: "ناحیه‌ای ورودی در برابر خروجی موردانتظار، ۱۲ هفته آینده", source: "حساب‌یار", backend: "derived", filters: "هفته/ماه، بانک" },
  { id: "party-statement", number: 15, title: "صورت وضعیت چکی مشتری", page: "کارت مشتری ← تب چک", chart: "کارت مشتری: نزد ما / در وصول / برگشتی تاریخی / آخرین رنگ با تاریخ", source: "حساب‌یار", backend: "derived", filters: "مشتری" },
  { id: "storage", number: 16, title: "چک‌ها به تفکیک محل نگهداری", page: "گزارش‌ها ← چک ← محل نگهداری", chart: "دونات صندوق/خزانه؛ جدول موجودی هر محل", source: "حساب‌یار", backend: "derived", filters: "محل نگهداری، وضعیت، بانک" },
  { id: "transfer-history", number: 17, title: "تاریخچهٔ انتقال و واگذاری", page: "کارت چک ← تاریخچه + گزارش‌ها ← چک ← تاریخچهٔ انتقال", chart: "خط زمانی گذارها + سند مرتبط هر گذار", source: "حساب‌یار", backend: "derived", filters: "چک، بازه، نوع گذار" },
];
