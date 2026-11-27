/**
 * Journal line-grid balance logic (US-303) — pure functions, no React.
 *
 * Money is integer IRR in state and payloads (spec §5.5 rule 5, §8.3);
 * Toman appears only at display time. A draft is balanced when the debit
 * total equals the credit total and is greater than zero (§8.2); every
 * line is one-sided — exactly one side carries a positive amount.
 */

import type {
  JournalDraftLineRequest,
  JournalDraftResponse,
} from "@/lib/api-client";

/** One editable row of the debit/credit grid. Amounts are integer IRR. */
export interface JournalLineDraft {
  /** Client-side row key (stable across edits, never sent to the API). */
  key: string;
  /** Engine account id, or null while the row has no account yet. */
  accountId: number | null;
  debit: number;
  credit: number;
  description: string;
}

export interface LineTotals {
  totalDebit: number;
  totalCredit: number;
  /** totalDebit - totalCredit; zero when balanced. */
  difference: number;
  balanced: boolean;
}

export interface LineValidation {
  valid: boolean;
  /** Persian messages, one per problem (empty when valid). */
  errors: string[];
}

let lineKeyCounter = 0;

/** Fresh empty grid row. */
export function emptyLine(): JournalLineDraft {
  lineKeyCounter += 1;
  return { key: `line-${lineKeyCounter}`, accountId: null, debit: 0, credit: 0, description: "" };
}

/** A new draft starts with two empty rows (a journal needs at least two lines). */
export function emptyLines(): JournalLineDraft[] {
  return [emptyLine(), emptyLine()];
}

/** Sum the grid. Amounts are integers, so the totals are exact. */
export function lineTotals(lines: JournalLineDraft[]): LineTotals {
  let totalDebit = 0;
  let totalCredit = 0;
  for (const line of lines) {
    totalDebit += line.debit;
    totalCredit += line.credit;
  }
  const difference = totalDebit - totalCredit;
  return {
    totalDebit,
    totalCredit,
    difference,
    balanced: difference === 0 && totalDebit > 0,
  };
}

/**
 * Validate the grid against the §8.2 invariants the backend enforces:
 * at least two lines, an account on every line, one-sided positive
 * amounts, and equal non-zero totals. Returns Persian errors for the UI;
 * the backend re-checks everything, so this is a fast path, not the
 * authority.
 */
export function validateLines(lines: JournalLineDraft[]): LineValidation {
  const errors: string[] = [];

  if (lines.length < 2) {
    errors.push("سند باید دست‌کم دو ردیف داشته باشد.");
  }

  let hasMissingAccount = false;
  let hasTwoSidedLine = false;
  let hasEmptyLine = false;
  let hasNegativeAmount = false;
  for (const line of lines) {
    if (line.accountId === null) hasMissingAccount = true;
    if (line.debit < 0 || line.credit < 0) hasNegativeAmount = true;
    if (line.debit > 0 && line.credit > 0) hasTwoSidedLine = true;
    if (line.debit === 0 && line.credit === 0) hasEmptyLine = true;
  }
  if (hasMissingAccount) errors.push("برای هر ردیف یک حساب انتخاب کنید.");
  if (hasNegativeAmount) errors.push("مبلغ‌ها نمی‌توانند منفی باشند.");
  if (hasTwoSidedLine) errors.push("هر ردیف فقط یک طرف دارد: یا بدهکار یا بستانکار، نه هر دو.");
  if (hasEmptyLine) errors.push("هر ردیف باید یک مبلغ بدهکار یا بستانکار داشته باشد.");

  const totals = lineTotals(lines);
  if (totals.totalDebit === 0 && totals.totalCredit === 0) {
    errors.push("مجموع سند باید بزرگ‌تر از صفر باشد.");
  } else if (totals.difference !== 0) {
    errors.push("مجموع بدهکار باید با مجموع بستانکار برابر باشد.");
  }

  return { valid: errors.length === 0, errors };
}

/** Grid rows -> API line payloads. Callers must validate first. */
export function toDraftLineRequests(lines: JournalLineDraft[]): JournalDraftLineRequest[] {
  return lines.map((line) => ({
    account_id: line.accountId ?? 0,
    debit: line.debit,
    credit: line.credit,
    description: line.description,
  }));
}

/** A saved draft -> editable grid rows (edit mode). */
export function fromDraftResponse(draft: JournalDraftResponse): JournalLineDraft[] {
  return draft.lines.map((line) => ({
    ...emptyLine(),
    accountId: line.account_id,
    debit: line.debit,
    credit: line.credit,
    description: line.description,
  }));
}

/**
 * Apply a one-sided amount edit: setting a debit clears the credit and
 * vice versa, so a row can never carry both sides.
 */
export function setLineAmount(
  line: JournalLineDraft,
  side: "debit" | "credit",
  amount: number | null,
): JournalLineDraft {
  const value = amount === null || amount < 0 ? 0 : Math.trunc(amount);
  return side === "debit"
    ? { ...line, debit: value, credit: value > 0 ? 0 : line.credit }
    : { ...line, credit: value, debit: value > 0 ? 0 : line.debit };
}
