/**
 * Date display — spec §5.5 rules 6–7.
 *
 * - APIs and storage use Gregorian ISO dates (`YYYY-MM-DD`) and
 *   timezone-aware ISO 8601 timestamps (UTC). Display is always Jalali.
 * - `formatIsoDate` / `formatIsoDateTime` are the only date renderers
 *   feature pages should use; they delegate to the deterministic
 *   converters in ./jalali (never `new Date(iso)` parsing).
 */

import { formatIsoJalali, formatIsoJalaliNumeric, isoToJalali } from "./jalali";

export { formatIsoJalali, formatIsoJalaliNumeric, isoToJalali };

/** ISO date -> Jalali display «۲۶ آبان ۱۴۰۵». Empty string for null/undefined. */
export function formatIsoDate(iso: string | null | undefined, opts: { weekday?: boolean } = {}): string {
  if (!iso) return "";
  return formatIsoJalali(iso, opts);
}

/** ISO date -> numeric Jalali «۱۴۰۵/۰۸/۲۶». Empty string for null/undefined. */
export function formatIsoDateNumeric(iso: string | null | undefined): string {
  if (!iso) return "";
  return formatIsoJalaliNumeric(iso);
}

const FA_DIGITS = ["۰", "۱", "۲", "۳", "۴", "۵", "۶", "۷", "۸", "۹"];

function toFaDigits(ascii: string): string {
  return ascii.replace(/\d/g, (d) => FA_DIGITS[Number(d)]);
}

/**
 * ISO 8601 timestamp -> Jalali date + 24h time in Asia/Tehran:
 * «۲۶ آبان ۱۴۰۵، ۱۴:۳۲». Empty string for null/undefined.
 * Throws on an unparseable timestamp.
 */
export function formatIsoDateTime(timestamp: string | null | undefined, timeZone = "Asia/Tehran"): string {
  if (!timestamp) return "";
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) {
    throw new RangeError(`Invalid ISO timestamp: ${JSON.stringify(timestamp)}`);
  }
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(date);
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
  const iso = `${get("year")}-${get("month")}-${get("day")}`;
  const time = toFaDigits(`${get("hour")}:${get("minute")}`);
  return `${formatIsoJalali(iso)}، ${time}`;
}
