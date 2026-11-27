/**
 * Money formatting — spec §5.5 rules 4–5 and §8.3.
 *
 * - Money is stored and moved as **integer IRR** everywhere. These helpers
 *   never change that: they only format for display or parse user input.
 * - **Toman is display-only** and must always be explicitly labelled.
 *   `formatMoney(..., { unit: "toman" })` divides by 10 and appends «تومان».
 * - Persian digits are for **presentation only**. `parseMoneyInput`
 *   normalizes Persian/Arabic-Indic digits to ASCII before parsing, so
 *   validation and storage always see ASCII.
 * - Thousands separator «٬» (U+066C), decimal separator «٫» (U+066B).
 * - Formatting is manual (no Intl dependency), so output is identical in
 *   Node, the browser and tests.
 */

import { en } from "./utils";

export type MoneyUnit = "irr" | "toman";

/** Persian digits for presentation only. Re-export of the VibeFarsi helper. */
export { fa as toPersianDigits } from "./utils";

/** Normalize Persian/Arabic-Indic digits to ASCII (validation/storage). */
export function normalizeDigits(value: string): string {
  return en(value);
}

/** 1 Toman = 10 IRR. */
export function irrToToman(amountIrr: number): number {
  return amountIrr / 10;
}

/** Toman back to integer IRR. Throws on a fractional result. */
export function tomanToIrr(toman: number): number {
  const irr = toman * 10;
  if (!Number.isInteger(irr)) {
    throw new RangeError(`Toman value ${toman} does not map to integer IRR`);
  }
  return irr;
}

const FA_DIGITS = ["۰", "۱", "۲", "۳", "۴", "۵", "۶", "۷", "۸", "۹"];

function toFaDigits(ascii: string): string {
  return ascii.replace(/\d/g, (d) => FA_DIGITS[Number(d)]);
}

/** Group an integer with «٬» and render Persian digits: 12450000 -> ۱۲٬۴۵۰٬۰۰۰. */
export function formatInteger(value: number): string {
  if (!Number.isFinite(value)) throw new RangeError(`Not a finite number: ${value}`);
  const sign = value < 0 ? "-" : "";
  const grouped = Math.abs(Math.trunc(value)).toLocaleString("en-US");
  return sign + toFaDigits(grouped).replace(/,/g, "٬");
}

/**
 * Format a decimal with up to `maxDecimals` places, trimming trailing
 * zeros: 13855.5 -> ۱۳٬۸۵۵٫۵. Used for fractional Toman display.
 */
export function formatDecimal(value: number, maxDecimals = 1): string {
  if (!Number.isFinite(value)) throw new RangeError(`Not a finite number: ${value}`);
  const sign = value < 0 ? "-" : "";
  const abs = Math.abs(value);
  const fixed = abs.toFixed(maxDecimals);
  const trimmed = fixed.includes(".") ? fixed.replace(/0+$/, "").replace(/\.$/, "") : fixed;
  const [intPart, fracPart] = trimmed.split(".");
  const grouped = Number(intPart).toLocaleString("en-US");
  const out = fracPart ? `${grouped}.${fracPart}` : grouped;
  return sign + toFaDigits(out).replace(/,/g, "٬").replace(".", "٫");
}

/** Integer IRR as a Persian number, no unit: 12450000 -> ۱۲٬۴۵۰٬۰۰۰. */
export function formatIrr(amountIrr: number): string {
  if (!Number.isInteger(amountIrr)) {
    throw new RangeError(`IRR amounts must be integers, got ${amountIrr}`);
  }
  return formatInteger(amountIrr);
}

/** Integer IRR displayed as Toman, no unit: 12450000 -> ۱٬۲۴۵٬۰۰۰. */
export function formatToman(amountIrr: number): string {
  if (!Number.isInteger(amountIrr)) {
    throw new RangeError(`IRR amounts must be integers, got ${amountIrr}`);
  }
  return formatDecimal(irrToToman(amountIrr));
}

/**
 * Full money display with an explicit unit label — the only money
 * rendering feature pages should use:
 *   formatMoney(12450000)               -> «۱٬۲۴۵٬۰۰۰ تومان» (default)
 *   formatMoney(12450000, {unit:"irr"}) -> «۱۲٬۴۵۰٬۰۰۰ ریال»
 */
export function formatMoney(amountIrr: number, opts: { unit?: MoneyUnit } = {}): string {
  const unit = opts.unit ?? "toman";
  if (unit === "irr") return `${formatIrr(amountIrr)} ریال`;
  return `${formatToman(amountIrr)} تومان`;
}

/**
 * Parse a money string typed by the user into integer IRR.
 *
 * Accepts Persian/Arabic-Indic/ASCII digits, «٬»/`,`/space separators and
 * an optional unit word («تومان»/«ریال»/«تومن»). The `inputUnit` says what
 * the typed number means (default: the field's unit — Toman in most
 * accounting forms, where the user types «۱۲۵۰۰۰» meaning 125,000 Toman).
 * Returns null for empty/invalid input; never throws on user input.
 */
export function parseMoneyInput(raw: string, inputUnit: MoneyUnit = "toman"): number | null {
  const ascii = normalizeDigits(raw).trim();
  if (!ascii) return null;
  // Strip separators, spaces and unit words; keep an optional leading minus.
  const cleaned = ascii
    .replace(/[٬,،\s]/g, "")
    .replace(/تومان|تومن|ریال/g, "")
    .trim();
  if (!/^-?\d+$/.test(cleaned)) return null;
  const value = Number(cleaned);
  if (!Number.isSafeInteger(value)) return null;
  return inputUnit === "toman" ? value * 10 : value;
}
