/**
 * Jalali (شمسی) calendar conversion — Borkowski algorithm as used by jalaali-js.
 * Valid for Jalali years 1 … 3177. Dependency-free.
 */

export type JalaliDate = { jy: number; jm: number; jd: number };

const div = (a: number, b: number) => Math.trunc(a / b);
const mod = (a: number, b: number) => a - Math.trunc(a / b) * b;

const BREAKS = [-61, 9, 38, 199, 426, 686, 756, 818, 1111, 1181, 1210, 1635, 2060, 2097, 2192, 2262, 2324, 2394, 2456, 3178];

function jalCal(jy: number) {
  const bl = BREAKS.length;
  const gy = jy + 621;
  let leapJ = -14;
  let jp = BREAKS[0];
  if (jy < jp || jy >= BREAKS[bl - 1]) throw new RangeError(`Invalid Jalali year ${jy}`);
  let jump = 0;
  for (let i = 1; i < bl; i += 1) {
    const jm = BREAKS[i];
    jump = jm - jp;
    if (jy < jm) break;
    leapJ = leapJ + div(jump, 33) * 8 + div(mod(jump, 33), 4);
    jp = jm;
  }
  let n = jy - jp;
  leapJ = leapJ + div(n, 33) * 8 + div(mod(n, 33) + 3, 4);
  if (mod(jump, 33) === 4 && jump - n === 4) leapJ += 1;
  const leapG = div(gy, 4) - div((div(gy, 100) + 1) * 3, 4) - 150;
  const march = 20 + leapJ - leapG;
  if (jump - n < 6) n = n - jump + div(jump + 4, 33) * 33;
  let leap = mod(mod(n + 1, 33) - 1, 4);
  if (leap === -1) leap = 4;
  return { leap, gy, march };
}

function g2d(gy: number, gm: number, gd: number) {
  let d = div((gy + div(gm - 8, 6) + 100100) * 1461, 4) + div(153 * mod(gm + 9, 12) + 2, 5) + gd - 34840408;
  d = d - div(div(gy + 100100 + div(gm - 8, 6), 100) * 3, 4) + 752;
  return d;
}

function d2g(jdn: number) {
  let j = 4 * jdn + 139361631;
  j = j + div(div(4 * jdn + 183187720, 146097) * 3, 4) * 4 - 3908;
  const i = div(mod(j, 1461), 4) * 5 + 308;
  const gd = div(mod(i, 153), 5) + 1;
  const gm = mod(div(i, 153), 12) + 1;
  const gy = div(j, 1461) - 100100 + div(8 - gm, 6);
  return { gy, gm, gd };
}

function j2d(jy: number, jm: number, jd: number) {
  const r = jalCal(jy);
  return g2d(r.gy, 3, r.march) + (jm - 1) * 31 - div(jm, 7) * (jm - 7) + jd - 1;
}

function d2j(jdn: number): JalaliDate {
  const gy = d2g(jdn).gy;
  let jy = gy - 621;
  const r = jalCal(jy);
  const jdn1f = g2d(gy, 3, r.march);
  let k = jdn - jdn1f;
  if (k >= 0) {
    if (k <= 185) return { jy, jm: 1 + div(k, 31), jd: mod(k, 31) + 1 };
    k -= 186;
  } else {
    jy -= 1;
    k += 179;
    if (r.leap === 1) k += 1;
  }
  return { jy, jm: 7 + div(k, 30), jd: mod(k, 30) + 1 };
}

export function toJalali(date: Date): JalaliDate {
  return d2j(g2d(date.getFullYear(), date.getMonth() + 1, date.getDate()));
}

export function toGregorian(jy: number, jm: number, jd: number): Date {
  const { gy, gm, gd } = d2g(j2d(jy, jm, jd));
  return new Date(gy, gm - 1, gd);
}

export function isLeapJalaliYear(jy: number) {
  return jalCal(jy).leap === 0;
}

export function jalaliMonthLength(jy: number, jm: number) {
  if (jm <= 6) return 31;
  if (jm <= 11) return 30;
  return isLeapJalaliYear(jy) ? 30 : 29;
}

export const JALALI_MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"];
export const JALALI_WEEKDAYS = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه"];
export const JALALI_WEEKDAYS_SHORT = ["ش", "ی", "د", "س", "چ", "پ", "ج"];

/** 0 = شنبه … 6 = جمعه */
export function jalaliWeekday(date: Date) {
  return (date.getDay() + 1) % 7;
}

const FA = ["۰", "۱", "۲", "۳", "۴", "۵", "۶", "۷", "۸", "۹"];
const faDigits = (n: number | string) => String(n).replace(/\d/g, (d) => FA[Number(d)]);

/** «چهارشنبه، ۲۵ شهریور ۱۴۰۵» */
export function formatJalali(date: Date, opts: { weekday?: boolean; year?: boolean } = {}) {
  const { jy, jm, jd } = toJalali(date);
  const parts = [`${faDigits(jd)} ${JALALI_MONTHS[jm - 1]}`];
  if (opts.year !== false) parts[0] += ` ${faDigits(jy)}`;
  return (opts.weekday ? `${JALALI_WEEKDAYS[jalaliWeekday(date)]}، ` : "") + parts.join(" ");
}

/** Numeric form «۱۴۰۵/۰۶/۲۵» */
export function formatJalaliNumeric(date: Date) {
  const { jy, jm, jd } = toJalali(date);
  return faDigits(`${jy}/${String(jm).padStart(2, "0")}/${String(jd).padStart(2, "0")}`);
}

/* ------------------------------------------------------------------ */
/* HesabYar additions — strict ISO dates (spec §5.5 rules 6, 9).       */
/*                                                                     */
/* APIs and storage use Gregorian ISO dates (`YYYY-MM-DD`). The UI     */
/* shows Jalali. The mapping must be deterministic and timezone-free:   */
/* these helpers work on integer components via the Borkowski          */
/* converters above — never on `Date` parsing, which shifts with the   */
/* viewer's timezone. An ambiguous or malformed string throws (or      */
/* returns false from `isValidIsoDate`); the calendar is never         */
/* inferred from an ambiguous numeric string.                          */
/* ------------------------------------------------------------------ */

const ISO_DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/;

export type GregorianDate = { gy: number; gm: number; gd: number };

/** Parse a strict `YYYY-MM-DD` string. Throws on anything else. */
export function parseIsoDate(iso: string): GregorianDate {
  const m = ISO_DATE_RE.exec(iso);
  if (!m) throw new RangeError(`Not a strict ISO date (YYYY-MM-DD): ${JSON.stringify(iso)}`);
  const gy = Number(m[1]);
  const gm = Number(m[2]);
  const gd = Number(m[3]);
  if (gm < 1 || gm > 12 || gd < 1 || gd > 31) {
    throw new RangeError(`Invalid Gregorian date: ${iso}`);
  }
  // Round-trip check rejects e.g. 2026-02-30.
  const back = d2g(g2d(gy, gm, gd));
  if (back.gy !== gy || back.gm !== gm || back.gd !== gd) {
    throw new RangeError(`Invalid Gregorian date: ${iso}`);
  }
  return { gy, gm, gd };
}

/** True when `value` is a strict, real `YYYY-MM-DD` date. Never throws. */
export function isValidIsoDate(value: unknown): value is string {
  if (typeof value !== "string") return false;
  try {
    parseIsoDate(value);
    return true;
  } catch {
    return false;
  }
}

/** Strict ISO date -> Jalali components (pure integer math). */
export function isoToJalali(iso: string): JalaliDate {
  const { gy, gm, gd } = parseIsoDate(iso);
  return d2j(g2d(gy, gm, gd));
}

/** Validate Jalali components; throws RangeError when out of range. */
export function assertValidJalali(jy: number, jm: number, jd: number): void {
  if (!Number.isInteger(jy) || !Number.isInteger(jm) || !Number.isInteger(jd)) {
    throw new RangeError(`Jalali date components must be integers: ${jy}/${jm}/${jd}`);
  }
  if (jm < 1 || jm > 12) throw new RangeError(`Invalid Jalali month: ${jm}`);
  // jalCal throws for years outside 1..3177.
  const len = jalaliMonthLength(jy, jm);
  if (jd < 1 || jd > len) {
    throw new RangeError(`Invalid Jalali day ${jd} for ${jy}/${jm} (month has ${len} days)`);
  }
}

/** Jalali components -> strict Gregorian ISO date (pure integer math). */
export function jalaliToIso(jy: number, jm: number, jd: number): string {
  assertValidJalali(jy, jm, jd);
  const { gy, gm, gd } = d2g(j2d(jy, jm, jd));
  return `${String(gy).padStart(4, "0")}-${String(gm).padStart(2, "0")}-${String(gd).padStart(2, "0")}`;
}

/** Jalali weekday (0 = شنبه) for a Julian day number. */
function weekdayFromJdn(jdn: number): number {
  return mod(jdn + 2, 7);
}

/** Strict ISO date -> Jalali display «۲۶ آبان ۱۴۰۵» (weekday optional). */
export function formatIsoJalali(iso: string, opts: { weekday?: boolean; year?: boolean } = {}) {
  const { gy, gm, gd } = parseIsoDate(iso);
  const { jy, jm, jd } = d2j(g2d(gy, gm, gd));
  const day = `${faDigits(jd)} ${JALALI_MONTHS[jm - 1]}`;
  const withYear = opts.year === false ? day : `${day} ${faDigits(jy)}`;
  if (!opts.weekday) return withYear;
  return `${JALALI_WEEKDAYS[weekdayFromJdn(g2d(gy, gm, gd))]}، ${withYear}`;
}

/** Strict ISO date -> numeric Jalali «۱۴۰۵/۰۸/۲۶». */
export function formatIsoJalaliNumeric(iso: string) {
  const { jy, jm, jd } = isoToJalali(iso);
  return faDigits(`${jy}/${String(jm).padStart(2, "0")}/${String(jd).padStart(2, "0")}`);
}

/**
 * Today as a strict ISO date in the given IANA timezone
 * (default Asia/Tehran, spec §5.5 / VibeFarsi rules).
 */
export function todayIso(timeZone = "Asia/Tehran"): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date());
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
  return `${get("year")}-${get("month")}-${get("day")}`;
}

