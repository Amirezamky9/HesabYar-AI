/**
 * Labels, descriptions and form helpers for the bank auto-coding
 * rules UI (US-565, W3).
 *
 * A bank rule is a user-defined W3 rule: when a statement line
 * matches its pattern (description / counterparty / amount range /
 * direction) the rule suggests an account. The rule is born in
 * SUGGEST mode and becomes AUTO only when a human promotes it after
 * three confirmed suggestions — the same threshold as the US-552
 * mapping learning. Amounts travel as whole Rial; the form takes
 * تومان and converts ×10 at the boundary.
 */

import type {
  BankRuleCreateRequest,
  BankRuleResponse,
  BankRuleUpdateRequest,
} from "@/lib/api-client";
import { formatMoney, irrToToman, parseMoneyInput } from "@/lib/format-money";
import { en, fa } from "@/lib/utils";

/** Confirmed suggestions a SUGGEST rule needs before a human may promote it. */
export const PROMOTION_THRESHOLD = 3;

export type BankRuleMode = "suggest" | "auto";
export type BankRuleDirection = "in" | "out" | "any";
export type BankRuleSuggestionStatus = "pending" | "confirmed" | "rejected";

export const BANK_RULE_MODE_LABELS: Record<BankRuleMode, string> = {
  suggest: "پیشنهاد",
  auto: "خودکار",
};

export const BANK_RULE_DIRECTION_LABELS: Record<BankRuleDirection, string> = {
  in: "ورودی",
  out: "خروجی",
  any: "هر دو",
};

export const BANK_RULE_DIRECTION_OPTIONS: { value: BankRuleDirection; label: string }[] = [
  { value: "any", label: "هر دو (ورودی و خروجی)" },
  { value: "in", label: "ورودی (واریز)" },
  { value: "out", label: "خروجی (برداشت)" },
];

export const BANK_RULE_SUGGESTION_STATUS_LABELS: Record<BankRuleSuggestionStatus, string> = {
  pending: "در انتظار بررسی",
  confirmed: "تأیید شده",
  rejected: "رد شده",
};

/** One-line Persian summary of a rule's pattern (all conditions are ANDed). */
export function describeBankRuleFa(rule: BankRuleResponse): string {
  const parts: string[] = [];
  if (rule.description_contains) {
    parts.push(`شرح شامل «${rule.description_contains}»`);
  }
  if (rule.counterparty_contains) {
    parts.push(`طرف حساب شامل «${rule.counterparty_contains}»`);
  }
  if (rule.direction === "in" || rule.direction === "out") {
    parts.push(BANK_RULE_DIRECTION_LABELS[rule.direction]);
  }
  if (rule.amount_min_irr !== null && rule.amount_max_irr !== null) {
    parts.push(`از ${formatMoney(rule.amount_min_irr)} تا ${formatMoney(rule.amount_max_irr)}`);
  } else if (rule.amount_min_irr !== null) {
    parts.push(`از ${formatMoney(rule.amount_min_irr)} به بالا`);
  } else if (rule.amount_max_irr !== null) {
    parts.push(`تا ${formatMoney(rule.amount_max_irr)}`);
  }
  if (rule.bank_key) {
    parts.push(`بانک ${rule.bank_key}`);
  }
  return parts.length > 0 ? parts.join(" • ") : "بدون شرط";
}

/** «۲ از ۳» — how far the rule is toward promotion. */
export function formatConfirmationsFa(rule: BankRuleResponse): string {
  return `${fa(rule.confirmations)} از ${fa(PROMOTION_THRESHOLD)}`;
}

/** One line explaining the rule's promotion state to the operator. */
export function promotionHintFa(rule: BankRuleResponse): string {
  if (rule.mode === "auto") {
    return "این قاعده خودکار است؛ ردیف‌های هم‌خوان را خودش کد می‌کند.";
  }
  if (rule.auto_eligible) {
    return "آمادهٔ ارتقاست؛ می‌توانید آن را خودکار کنید.";
  }
  const remaining = PROMOTION_THRESHOLD - rule.confirmations;
  return `${fa(remaining)} تأیید دیگر تا آماده‌شدن برای ارتقا مانده است.`;
}

/** Raw form state (amounts as typed تومان strings, like the rules form). */
export interface BankRuleFormValues {
  name: string;
  bankKey: string;
  direction: BankRuleDirection;
  descriptionContains: string;
  counterpartyContains: string;
  amountMinToman: string;
  amountMaxToman: string;
  targetAccountId: number | null;
  targetPartyId: string;
  priority: string;
  enabled: boolean;
}

export function emptyBankRuleForm(): BankRuleFormValues {
  return {
    name: "",
    bankKey: "",
    direction: "any",
    descriptionContains: "",
    counterpartyContains: "",
    amountMinToman: "",
    amountMaxToman: "",
    targetAccountId: null,
    targetPartyId: "",
    priority: "100",
    enabled: true,
  };
}

/** A stored rule back into the form (Rial → تومان for the amount fields). */
export function bankRuleToForm(rule: BankRuleResponse): BankRuleFormValues {
  return {
    name: rule.name,
    bankKey: rule.bank_key,
    direction: (rule.direction as BankRuleDirection) ?? "any",
    descriptionContains: rule.description_contains ?? "",
    counterpartyContains: rule.counterparty_contains ?? "",
    amountMinToman:
      rule.amount_min_irr === null ? "" : String(irrToToman(rule.amount_min_irr)),
    amountMaxToman:
      rule.amount_max_irr === null ? "" : String(irrToToman(rule.amount_max_irr)),
    targetAccountId: rule.target_account_id,
    targetPartyId: rule.target_party_id === null ? "" : String(rule.target_party_id),
    priority: String(rule.priority),
    enabled: rule.enabled,
  };
}

function trimmed(value: string): string | null {
  const out = value.trim();
  return out === "" ? null : out;
}

function parseOptionalInt(raw: string): number | null {
  const ascii = en(raw).trim().replace(/[٬,،\s]/g, "");
  if (ascii === "") return null;
  if (!/^\d+$/.test(ascii)) return Number.NaN;
  return Number(ascii);
}

export type BankRuleFormErrors = Partial<Record<keyof BankRuleFormValues, string>> & {
  form?: string;
};

/**
 * Validate the form (mirrors the backend's BANK_RULE_INVALID cases):
 * a name, a target account, at least one pattern condition, and a
 * well-ordered amount range. Returns an empty object when valid.
 */
export function validateBankRuleForm(values: BankRuleFormValues): BankRuleFormErrors {
  const errors: BankRuleFormErrors = {};
  if (values.name.trim() === "") {
    errors.name = "نام قاعده را وارد کنید.";
  }
  if (values.targetAccountId === null) {
    errors.targetAccountId = "حساب مقصد را انتخاب کنید.";
  }
  const hasDescription = values.descriptionContains.trim() !== "";
  const hasCounterparty = values.counterpartyContains.trim() !== "";
  const minIrr = parseMoneyInput(values.amountMinToman, "toman");
  const maxIrr = parseMoneyInput(values.amountMaxToman, "toman");
  if (values.amountMinToman.trim() !== "" && minIrr === null) {
    errors.amountMinToman = "حداقل مبلغ را به‌صورت عدد وارد کنید.";
  }
  if (values.amountMaxToman.trim() !== "" && maxIrr === null) {
    errors.amountMaxToman = "حداکثر مبلغ را به‌صورت عدد وارد کنید.";
  }
  if (minIrr !== null && maxIrr !== null && minIrr > maxIrr) {
    errors.amountMaxToman = "حداکثر مبلغ نمی‌تواند کمتر از حداقل باشد.";
  }
  const hasAmount = minIrr !== null || maxIrr !== null;
  if (!hasDescription && !hasCounterparty && !hasAmount) {
    errors.form =
      "دست‌کم یک شرط لازم است؛ قاعدهٔ بدون شرط روی همهٔ ردیف‌ها می‌نشیند.";
  }
  const priority = parseOptionalInt(values.priority);
  if (Number.isNaN(priority)) {
    errors.priority = "اولویت باید عدد باشد.";
  }
  const partyId = parseOptionalInt(values.targetPartyId);
  if (Number.isNaN(partyId)) {
    errors.targetPartyId = "شناسهٔ طرف حساب باید عدد باشد.";
  }
  return errors;
}

function formAmounts(values: BankRuleFormValues): {
  amount_min_irr: number | null;
  amount_max_irr: number | null;
} {
  return {
    amount_min_irr: parseMoneyInput(values.amountMinToman, "toman"),
    amount_max_irr: parseMoneyInput(values.amountMaxToman, "toman"),
  };
}

/** Form → create request (تومان ×10 to whole Rial at the boundary). */
export function bankRuleFormToCreate(
  values: BankRuleFormValues,
  companyId: string,
): BankRuleCreateRequest {
  const partyId = parseOptionalInt(values.targetPartyId);
  const priority = parseOptionalInt(values.priority);
  return {
    company_id: companyId,
    name: values.name.trim(),
    target_account_id: values.targetAccountId as number,
    bank_key: values.bankKey.trim(),
    description_contains: trimmed(values.descriptionContains),
    counterparty_contains: trimmed(values.counterpartyContains),
    ...formAmounts(values),
    direction: values.direction,
    target_party_id: partyId === null || Number.isNaN(partyId) ? null : partyId,
    priority: priority === null || Number.isNaN(priority) ? 100 : priority,
    enabled: values.enabled,
  };
}

/**
 * Form → update request. Only the fields that changed are sent, and
 * a cleared pattern field goes as an explicit null (the backend
 * clears that condition). Changing the pattern or the target resets
 * the rule's confirmations — the UI warns about that before saving.
 */
export function bankRuleFormToUpdate(
  values: BankRuleFormValues,
  original: BankRuleResponse,
): BankRuleUpdateRequest {
  const body: BankRuleUpdateRequest = {};
  if (values.name.trim() !== original.name) body.name = values.name.trim();
  if (values.bankKey.trim() !== original.bank_key) body.bank_key = values.bankKey.trim();
  const description = trimmed(values.descriptionContains);
  if (description !== original.description_contains) {
    body.description_contains = description;
  }
  const counterparty = trimmed(values.counterpartyContains);
  if (counterparty !== original.counterparty_contains) {
    body.counterparty_contains = counterparty;
  }
  const { amount_min_irr, amount_max_irr } = formAmounts(values);
  if (amount_min_irr !== original.amount_min_irr) body.amount_min_irr = amount_min_irr;
  if (amount_max_irr !== original.amount_max_irr) body.amount_max_irr = amount_max_irr;
  if (values.direction !== original.direction) body.direction = values.direction;
  if (values.targetAccountId !== original.target_account_id) {
    body.target_account_id = values.targetAccountId;
  }
  const partyId = parseOptionalInt(values.targetPartyId);
  const partyValue = partyId === null || Number.isNaN(partyId) ? null : partyId;
  if (partyValue !== original.target_party_id) body.target_party_id = partyValue;
  const priority = parseOptionalInt(values.priority);
  const priorityValue = priority === null || Number.isNaN(priority) ? 100 : priority;
  if (priorityValue !== original.priority) body.priority = priorityValue;
  if (values.enabled !== original.enabled) body.enabled = values.enabled;
  return body;
}

/** True when the edit changes the pattern or the target (resets confirmations). */
export function bankRuleUpdateResetsTrust(update: BankRuleUpdateRequest): boolean {
  return (
    update.description_contains !== undefined ||
    update.counterparty_contains !== undefined ||
    update.amount_min_irr !== undefined ||
    update.amount_max_irr !== undefined ||
    update.direction !== undefined ||
    update.target_account_id !== undefined ||
    update.target_party_id !== undefined ||
    update.target_kind !== undefined ||
    update.bank_key !== undefined
  );
}
