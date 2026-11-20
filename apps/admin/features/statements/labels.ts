/**
 * Bank statement labels (US-304). States come from the facade STATUS_MAP
 * (draft/validated/posted/cancelled); journal validation types live here.
 */

/** Statement states the API accepts as a filter (docs/rest-api.md). */
export const STATEMENT_STATES = ["draft", "validated", "posted", "cancelled"] as const;

/** Journal validation types (schema statements.py). */
export const STATEMENT_VALIDATION_LABELS: Record<string, string> = {
  balance: "مانده",
  amount: "مبلغ",
  number_of_lines: "تعداد ردیف‌ها",
};

export function statementValidationLabel(validation: string): string {
  return STATEMENT_VALIDATION_LABELS[validation] ?? validation;
}
