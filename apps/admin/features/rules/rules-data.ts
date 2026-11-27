/**
 * Persian labels + pure helpers for the rules management UI (US-562,
 * Phase 5-7 ۵خ W2).
 *
 * Everything the page renders about a rule lives here: the twelve
 * canonical event types (§10.2 + ADR 0004 + ADR 0005), the closed
 * action set (six kinds — the UI can only pick from these, never a
 * free-text kind), the condition DSL builder (rows ⇄ JSONLogic-style
 * tree), decision/outcome/mode labels for the execution audit log,
 * and the user-defined vs system ownership badge.
 *
 * Money: amounts inside conditions and draft lines are whole Rial on
 * the wire; the operator types تومان and this file converts (×10) at
 * the boundary. Unknown backend codes fall back to the raw code so a
 * new value never silently disappears.
 */

import type { StatusTone } from "@/components/ui";
import type {
  RuleAction,
  RuleCreateRequest,
  RuleExecutionResponse,
  RuleResponse,
  RuleUpdateRequest,
} from "@/lib/api-client";
import { en, faNumber } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Canonical event types (12)
// ---------------------------------------------------------------------------

/** The twelve canonical event types, in envelope order (US-561). */
export const RULE_EVENT_TYPES = [
  "sales.order.completed",
  "sales.order.cancelled",
  "sales.refund.created",
  "payment.captured",
  "payment.refunded",
  "payment.settlement.received",
  "purchase.invoice.received",
  "inventory.cost.recognized",
  "bank.transaction.imported",
  "cheque.received.registered",
  "cheque.received.cancelled",
  "installment.schedule.created",
] as const;

export type RuleEventTypeCode = (typeof RULE_EVENT_TYPES)[number];

export const RULE_EVENT_TYPE_LABELS: Record<string, { label: string; description: string }> = {
  "sales.order.completed": { label: "تکمیل سفارش فروش", description: "سفارش فروش تکمیل شد" },
  "sales.order.cancelled": { label: "لغو سفارش فروش", description: "سفارش فروش لغو شد" },
  "sales.refund.created": { label: "ایجاد استرداد فروش", description: "استرداد فروش ثبت شد" },
  "payment.captured": { label: "دریافت پرداخت", description: "پرداخت دریافت شد" },
  "payment.refunded": { label: "استرداد پرداخت", description: "پرداخت مسترد شد" },
  "payment.settlement.received": { label: "دریافت تسویه", description: "تسویهٔ درگاه دریافت شد" },
  "purchase.invoice.received": { label: "دریافت فاکتور خرید", description: "فاکتور خرید دریافت شد" },
  "inventory.cost.recognized": { label: "شناسایی بهای تمام‌شده", description: "بهای تمام‌شدهٔ کالا شناسایی شد" },
  "bank.transaction.imported": { label: "ایمپورت تراکنش بانکی", description: "تراکنش بانکی ایمپورت شد" },
  "cheque.received.registered": { label: "ثبت چک دریافتی", description: "چک دریافتی ثبت شد" },
  "cheque.received.cancelled": { label: "لغو چک دریافتی", description: "چک دریافتی لغو شد" },
  "installment.schedule.created": { label: "ایجاد برنامهٔ اقساط", description: "برنامهٔ اقساط ساخته شد" },
};

export function eventTypeLabel(eventType: string | null | undefined): string {
  if (!eventType) return "نامشخص";
  return RULE_EVENT_TYPE_LABELS[eventType]?.label ?? eventType;
}

export function eventTypeDescription(eventType: string | null | undefined): string | null {
  if (!eventType) return null;
  return RULE_EVENT_TYPE_LABELS[eventType]?.description ?? null;
}

// ---------------------------------------------------------------------------
// Closed action set (6) — the picker offers only these
// ---------------------------------------------------------------------------

export const RULE_ACTION_KINDS = [
  "create_draft",
  "post_if_policy_allows",
  "notify",
  "assign",
  "tag",
  "set_field",
] as const;

export type RuleActionKindCode = (typeof RULE_ACTION_KINDS)[number];

export const RULE_ACTION_LABELS: Record<string, { label: string; description: string }> = {
  create_draft: {
    label: "ساخت پیش‌نویس روزنامه",
    description: "یک پیش‌نویس روزنامه می‌سازد؛ ثبت آن فقط با تأیید و از مسیر درگاه سیاستی ممکن است.",
  },
  post_if_policy_allows: {
    label: "ثبت پیش‌نویس (با درگاه سیاستی)",
    description: "پیش‌نویس رویداد را فقط در صورت اجازهٔ درگاه سیاستی سه‌لایه ثبت می‌کند؛ در غیر این صورت پیش‌نویس می‌ماند.",
  },
  notify: {
    label: "اطلاع‌رسانی",
    description: "به گیرنده‌ها اطلاع می‌دهد. ایمیل فعلاً بک‌اند ندارد و پیامک با US-564 فعال می‌شود؛ نتیجهٔ هر کانال در اجرا ثبت می‌شود.",
  },
  assign: {
    label: "تخصیص آیتم بازبینی",
    description: "آیتم بازبینی رویداد را به یک مسئول تخصیص می‌دهد.",
  },
  tag: {
    label: "برچسب‌زدن آیتم بازبینی",
    description: "یک برچسب روی آیتم بازبینی رویداد می‌گذارد.",
  },
  set_field: {
    label: "تغییر شدت آیتم بازبینی",
    description: "شدت آیتم بازبینی رویداد را تغییر می‌دهد.",
  },
};

export function actionKindLabel(kind: string | null | undefined): string {
  if (!kind) return "نامشخص";
  return RULE_ACTION_LABELS[kind]?.label ?? kind;
}

export function actionKindDescription(kind: string | null | undefined): string | null {
  if (!kind) return null;
  return RULE_ACTION_LABELS[kind]?.description ?? null;
}

/** Notification channels the notify action accepts (v1 adapter: in_app recorded, email skipped, sms unsupported). */
export const NOTIFY_CHANNELS = ["in_app", "email", "sms"] as const;

export const NOTIFY_CHANNEL_LABELS: Record<string, string> = {
  in_app: "درون‌برنامه‌ای",
  email: "ایمیل",
  sms: "پیامک",
};

export function notifyChannelLabel(channel: string | null | undefined): string {
  if (!channel) return "نامشخص";
  return NOTIFY_CHANNEL_LABELS[channel] ?? channel;
}

/** Delivery outcome per channel, as recorded on the execution (v1 honesty note, docs/rules-engine.md). */
export const NOTIFY_CHANNEL_STATUS_LABELS: Record<string, string> = {
  recorded: "ثبت شد",
  skipped: "ارسال نشد (بک‌اند ندارد)",
  unsupported: "پشتیبانی نمی‌شود",
};

export function notifyChannelStatusLabel(status: string | null | undefined): string {
  if (!status) return "نامشخص";
  return NOTIFY_CHANNEL_STATUS_LABELS[status] ?? status;
}

export const RULE_SEVERITY_LABELS: Record<string, string> = {
  info: "اطلاع",
  warning: "هشدار",
  critical: "بحرانی",
};

export function ruleSeverityLabel(value: string | null | undefined): string {
  if (!value) return "نامشخص";
  return RULE_SEVERITY_LABELS[value] ?? value;
}

// ---------------------------------------------------------------------------
// Condition DSL — operators, fields, rows ⇄ tree
// ---------------------------------------------------------------------------

export const CONDITION_OPERATORS = ["==", "!=", ">", ">=", "<", "<=", "in", "not_in"] as const;

export type ConditionOperator = (typeof CONDITION_OPERATORS)[number];

export const CONDITION_OPERATOR_LABELS: Record<ConditionOperator, string> = {
  "==": "برابر است با",
  "!=": "برابر نیست با",
  ">": "بزرگ‌تر از",
  ">=": "بزرگ‌تر یا مساوی",
  "<": "کوچک‌تر از",
  "<=": "کوچک‌تر یا مساوی",
  in: "در فهرست است",
  not_in: "در فهرست نیست",
};

export function conditionOperatorLabel(operator: string | null | undefined): string {
  if (!operator) return "نامشخص";
  return (CONDITION_OPERATOR_LABELS as Record<string, string>)[operator] ?? operator;
}

/** Facts the condition builder offers (docs/rules-engine.md § facts). */
export const CONDITION_FIELDS = [
  { path: "amount_irr", label: "مبلغ رویداد", hint: "به تومان وارد کنید؛ به ریال تبدیل می‌شود." },
  { path: "channel", label: "کانال / درگاه", hint: "مثلاً zarinpal" },
  { path: "party", label: "طرف (مشتری)", hint: "شناسهٔ طرف" },
  { path: "company", label: "شرکت", hint: "شناسهٔ شرکت" },
  { path: "occurred_at", label: "تاریخ وقوع", hint: "تاریخ میلادی ISO، مثلاً 2026-11-21" },
] as const;

export function conditionFieldLabel(path: string | null | undefined): string {
  if (!path) return "نامشخص";
  return CONDITION_FIELDS.find((f) => f.path === path)?.label ?? path;
}

/**
 * A field path is valid when it mirrors the backend's
 * validate_field_path: at most 5 dotted segments of [A-Za-z0-9_-]+.
 */
export function isValidFieldPath(path: string): boolean {
  const segments = path.split(".");
  if (segments.length < 1 || segments.length > 5) return false;
  return segments.every((segment) => /^[A-Za-z0-9_-]+$/.test(segment));
}

/** تومان input (Persian digits, separators, an optional decimal) → whole Rial, or null. */
export function parseTomanToIrr(raw: string): number | null {
  const ascii = en(raw)
    .trim()
    .replace(/[٬,،\s]/g, "")
    .replace(/تومان|تومن|ریال/g, "");
  if (!/^-?\d+(\.\d+)?$/.test(ascii)) return null;
  const irr = Number(ascii) * 10;
  if (!Number.isSafeInteger(irr)) return null;
  return irr;
}

/** Whole Rial → تومان for display (a decimal only when the Rial is not divisible by 10). */
export function irrToTomanString(irr: number): string {
  return String(irr / 10);
}

export interface ConditionRow {
  id: string;
  /** Var path, e.g. "amount_irr" | "channel" | "payload.total_amount". */
  field: string;
  operator: ConditionOperator;
  /** Raw input as typed: تومان for amount_irr, comma-separated for in/not_in. */
  value: string;
}

export type ConditionCombinator = "and" | "or";

export const CONDITION_COMBINATOR_LABELS: Record<ConditionCombinator, string> = {
  and: "همهٔ شرط‌ها (و)",
  or: "دست‌کم یکی از شرط‌ها (یا)",
};

export interface ConditionBuilderState {
  combinator: ConditionCombinator;
  rows: ConditionRow[];
}

export function emptyConditionRow(id: string): ConditionRow {
  return { id, field: "amount_irr", operator: ">=", value: "" };
}

/** Persian error for one builder row, or null when the row is valid. */
export function validateConditionRow(row: ConditionRow): string | null {
  if (!row.field.trim()) return "فیلد شرط را انتخاب کنید.";
  if (!isValidFieldPath(row.field.trim())) {
    return "مسیر فیلد معتبر نیست؛ حداکثر ۵ بخش با حروف، عدد، خط تیره و زیرخط.";
  }
  if (row.field.trim() === "occurred_at" && !/^\d{4}-\d{2}-\d{2}/.test(row.value.trim())) {
    return "تاریخ وقوع باید به قالب میلادی ISO باشد (مثلاً 2026-11-21).";
  }
  const parts = splitListValue(row.value);
  if (parts.length === 0) return "مقدار شرط را وارد کنید.";
  if (row.operator !== "in" && row.operator !== "not_in" && parts.length > 1) {
    return "برای این عملگر فقط یک مقدار وارد کنید؛ برای چند مقدار از «در فهرست است» استفاده کنید.";
  }
  if (row.field.trim() === "amount_irr") {
    for (const part of parts) {
      if (parseTomanToIrr(part) === null) return "مبلغ باید عدد باشد.";
    }
  }
  return null;
}

/** Split an in/not_in value on commas (Latin or Persian) or newlines; empty items dropped. */
export function splitListValue(raw: string): string[] {
  return raw
    .split(/[,،\n]/)
    .map((part) => part.trim())
    .filter((part) => part !== "");
}

function scalarForField(field: string, raw: string): unknown {
  if (field === "amount_irr") return parseTomanToIrr(raw);
  if (field === "channel" || field === "party" || field === "company" || field === "occurred_at") {
    return raw;
  }
  // A payload.* field: integer-like values compare as numbers, the
  // literals true/false as booleans, everything else as text.
  if (/^-?\d+$/.test(raw)) return Number(raw);
  if (raw === "true") return true;
  if (raw === "false") return false;
  return raw;
}

/**
 * Builder rows → the DSL tree. Returns null when any row is invalid
 * (the form reports the row's own error via validateConditionRow).
 * No rows means {} — "no conditions".
 */
export function rowsToConditions(state: ConditionBuilderState): Record<string, unknown> | null {
  if (state.rows.length === 0) return {};
  const comparisons: Record<string, unknown>[] = [];
  for (const row of state.rows) {
    if (validateConditionRow(row) !== null) return null;
    const field = row.field.trim();
    const parts = splitListValue(row.value);
    const values = parts.map((part) => scalarForField(field, part));
    const right = row.operator === "in" || row.operator === "not_in" ? values : values[0];
    comparisons.push({ [row.operator]: [{ var: field }, right] });
  }
  if (comparisons.length === 1) return comparisons[0];
  return { [state.combinator]: comparisons };
}

function comparisonToRow(node: unknown, id: string): ConditionRow | null {
  if (typeof node !== "object" || node === null || Array.isArray(node)) return null;
  const entries = Object.entries(node as Record<string, unknown>);
  if (entries.length !== 1) return null;
  const [operator, operands] = entries[0];
  if (!(CONDITION_OPERATORS as readonly string[]).includes(operator)) return null;
  if (!Array.isArray(operands) || operands.length !== 2) return null;
  const [left, right] = operands;
  if (typeof left !== "object" || left === null || Array.isArray(left)) return null;
  const leftEntries = Object.entries(left as Record<string, unknown>);
  if (leftEntries.length !== 1 || leftEntries[0][0] !== "var") return null;
  const field = leftEntries[0][1];
  if (typeof field !== "string") return null;
  const isListOp = operator === "in" || operator === "not_in";
  const rawValues = isListOp ? (Array.isArray(right) ? right : null) : [right];
  if (rawValues === null) return null;
  const parts: string[] = [];
  for (const raw of rawValues) {
    if (typeof raw === "number") {
      parts.push(field === "amount_irr" ? irrToTomanString(raw) : String(raw));
    } else if (typeof raw === "string" || typeof raw === "boolean") {
      parts.push(String(raw));
    } else {
      return null;
    }
  }
  return {
    id,
    field,
    operator: operator as ConditionOperator,
    value: parts.join("، "),
  };
}

/**
 * A stored DSL tree → builder rows. Only the shapes the builder can
 * edit round-trip: {} (no conditions), one comparison, or a flat
 * and/or of comparisons. Anything else (not, nesting, a var on the
 * right, non-scalar operands) reports `unsupported` and the form
 * falls back to raw-JSON editing — the rule is never silently
 * rewritten into a shape it did not have.
 */
export function conditionsToBuilder(conditions: Record<string, unknown>): {
  state: ConditionBuilderState;
  unsupported: boolean;
} {
  const empty: ConditionBuilderState = { combinator: "and", rows: [] };
  const keys = Object.keys(conditions);
  if (keys.length === 0) return { state: empty, unsupported: false };
  if (keys.length === 1 && (keys[0] === "and" || keys[0] === "or")) {
    const children = conditions[keys[0]];
    if (!Array.isArray(children)) return { state: empty, unsupported: true };
    const rows: ConditionRow[] = [];
    for (let i = 0; i < children.length; i += 1) {
      const row = comparisonToRow(children[i], `row-${i + 1}`);
      if (row === null) return { state: empty, unsupported: true };
      rows.push(row);
    }
    return { state: { combinator: keys[0] as ConditionCombinator, rows }, unsupported: false };
  }
  const row = comparisonToRow(conditions, "row-1");
  if (row === null) return { state: empty, unsupported: true };
  return { state: { combinator: "and", rows: [row] }, unsupported: false };
}

function formatRowValueFa(row: ConditionRow): string {
  if (row.field === "amount_irr") {
    const parts = splitListValue(row.value).map((part) => {
      const irr = parseTomanToIrr(part);
      return irr === null ? part : `${faNumber(irr / 10)} تومان`;
    });
    return parts.join("، ");
  }
  return row.value;
}

/**
 * One-line Persian summary of a rule's conditions for the list card:
 * «بدون شرط…» for {}, the rows joined with «و»/«یا», or «شرط پیشرفته»
 * when the tree is beyond the builder.
 */
export function describeConditionsFa(conditions: Record<string, unknown>): string {
  const { state, unsupported } = conditionsToBuilder(conditions);
  if (unsupported) return "شرط پیشرفته (خارج از سازندهٔ شرط)";
  if (state.rows.length === 0) return "بدون شرط — روی همهٔ رویدادهای این نوع اجرا می‌شود";
  const joiner = state.combinator === "and" ? " و " : " یا ";
  return state.rows
    .map(
      (row) =>
        `${conditionFieldLabel(row.field)} ${conditionOperatorLabel(row.operator)} ${formatRowValueFa(row)}`,
    )
    .join(joiner);
}

// ---------------------------------------------------------------------------
// Action descriptions + validation (Persian)
// ---------------------------------------------------------------------------

/** Persian description of what an action does (the dry-run panel shows this, never the backend's English would_do). */
export function describeActionFa(action: RuleAction): string {
  const params = action.params as Record<string, unknown>;
  switch (action.kind) {
    case "create_draft": {
      const lines = Array.isArray(params.lines) ? params.lines : [];
      return `ساخت پیش‌نویس روزنامه با ${faNumber(lines.length)} ردیف`;
    }
    case "post_if_policy_allows":
      return "ثبت پیش‌نویس رویداد، فقط در صورت اجازهٔ درگاه سیاستی";
    case "notify": {
      const recipients = Array.isArray(params.recipients)
        ? (params.recipients as unknown[]).map(String)
        : [];
      const channels = Array.isArray(params.channels)
        ? (params.channels as unknown[]).map((c) => notifyChannelLabel(String(c)))
        : [];
      const who = recipients.length > 0 ? recipients.join("، ") : "گیرندهٔ نامشخص";
      const how = channels.length > 0 ? channels.join("، ") : "کانال پیش‌فرض";
      return `اطلاع‌رسانی به ${who} از طریق ${how}`;
    }
    case "assign":
      return `تخصیص آیتم بازبینی به ${typeof params.assignee === "string" ? params.assignee : "نامشخص"}`;
    case "tag":
      return `برچسب «${typeof params.tag === "string" ? params.tag : "نامشخص"}» روی آیتم بازبینی`;
    case "set_field":
      return `تغییر شدت آیتم بازبینی به «${ruleSeverityLabel(typeof params.value === "string" ? params.value : null)}»`;
    default:
      return action.kind;
  }
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim() !== "";
}

/**
 * Client-side validation of an action's params, mirroring the
 * backend's per-kind schema (the backend still validates — this only
 * gives the operator a Persian error before the round-trip).
 * Returns the Persian error, or null when the action is valid.
 */
export function validateActionParams(action: RuleAction): string | null {
  const params = action.params as Record<string, unknown>;
  switch (action.kind) {
    case "create_draft": {
      if (params.document !== undefined && params.document !== "journal") {
        return "در این نسخه فقط پیش‌نویس روزنامه ساخته می‌شود.";
      }
      if (!Array.isArray(params.lines) || params.lines.length < 2) {
        return "پیش‌نویس روزنامه دست‌کم ۲ ردیف لازم دارد.";
      }
      for (const rawLine of params.lines as unknown[]) {
        if (typeof rawLine !== "object" || rawLine === null) return "یک ردیف پیش‌نویس معتبر نیست.";
        const line = rawLine as Record<string, unknown>;
        if (!isNonEmptyString(line.account_code)) return "کد حساب هر ردیف را وارد کنید.";
        const hasDebit = line.debit !== undefined && line.debit !== null;
        const hasCredit = line.credit !== undefined && line.credit !== null;
        if (hasDebit === hasCredit) return "هر ردیف دقیقاً یک سمت دارد: بدهکار یا بستانکار.";
        const amount = hasDebit ? line.debit : line.credit;
        if (typeof amount === "number") {
          if (!Number.isInteger(amount) || amount <= 0) return "مبلغ هر ردیف باید عدد مثبت باشد.";
        } else if (typeof amount === "object" && amount !== null && !Array.isArray(amount)) {
          const entries = Object.entries(amount as Record<string, unknown>);
          if (entries.length !== 1 || entries[0][0] !== "var" || typeof entries[0][1] !== "string") {
            return "مبلغ متغیر هر ردیف باید به شکل {\"var\": \"مسیر\"} باشد.";
          }
          if (!isValidFieldPath(entries[0][1])) return "مسیر مبلغ متغیر معتبر نیست.";
        } else {
          return "مبلغ هر ردیف باید عدد مثبت یا ارجاع به فیلد رویداد باشد.";
        }
      }
      return null;
    }
    case "post_if_policy_allows": {
      if (params.draft_kind !== undefined && params.draft_kind !== "journal") {
        return "در این نسخه فقط پیش‌نویس روزنامه ثبت می‌شود.";
      }
      if (params.draft_id !== undefined && params.draft_id !== null) {
        if (typeof params.draft_id !== "number" || !Number.isInteger(params.draft_id) || params.draft_id <= 0) {
          return "شناسهٔ پیش‌نویس باید عدد مثبت باشد.";
        }
      }
      if (params.draft_id_field !== undefined && params.draft_id_field !== null) {
        if (typeof params.draft_id_field !== "string" || !isValidFieldPath(params.draft_id_field)) {
          return "فیلد شناسهٔ پیش‌نویس معتبر نیست.";
        }
      }
      return null;
    }
    case "notify": {
      if (!Array.isArray(params.recipients) || params.recipients.length === 0) {
        return "دست‌کم یک گیرنده وارد کنید.";
      }
      if (Array.isArray(params.channels)) {
        for (const channel of params.channels as unknown[]) {
          if (!(NOTIFY_CHANNELS as readonly string[]).includes(String(channel))) {
            return "کانال اطلاع‌رسانی معتبر نیست.";
          }
        }
      }
      if (!isNonEmptyString(params.subject)) return "موضوع اطلاع‌رسانی را وارد کنید.";
      if (!isNonEmptyString(params.message)) return "متن اطلاع‌رسانی را وارد کنید.";
      return null;
    }
    case "assign":
      if (!isNonEmptyString(params.assignee)) return "نام مسئول تخصیص را وارد کنید.";
      return null;
    case "tag": {
      if (!isNonEmptyString(params.tag)) return "برچسب را وارد کنید.";
      if ((params.tag as string).trim().length > 64) return "برچسب حداکثر ۶۴ نویسه است.";
      return null;
    }
    case "set_field": {
      if (params.target !== "review_item") return "در این نسخه فقط فیلد آیتم بازبینی تغییر می‌کند.";
      if (params.field !== "severity") return "در این نسخه فقط شدت آیتم بازبینی تغییر می‌کند.";
      if (!["info", "warning", "critical"].includes(String(params.value))) {
        return "مقدار شدت معتبر نیست.";
      }
      return null;
    }
    default:
      return "این اقدام در مجموعهٔ مجاز نیست.";
  }
}

// ---------------------------------------------------------------------------
// Rule form values (pure: rule ⇄ form ⇄ request)
// ---------------------------------------------------------------------------

export interface DraftLineDraft {
  id: string;
  accountCode: string;
  side: "debit" | "credit";
  amountKind: "literal" | "var";
  /** تومان, as typed (literal amounts only). */
  amountToman: string;
  /** Var path, e.g. "amount_irr" (var amounts only). */
  varPath: string;
  description: string;
}

export function emptyDraftLine(id: string, side: "debit" | "credit"): DraftLineDraft {
  return { id, accountCode: "", side, amountKind: "literal", amountToman: "", varPath: "amount_irr", description: "" };
}

export interface RuleFormValues {
  name: string;
  description: string;
  eventType: string;
  combinator: ConditionCombinator;
  rows: ConditionRow[];
  /** Raw JSON editing, only when the stored DSL is beyond the builder. */
  conditionsAdvanced: boolean;
  rawConditions: string;
  actionKind: string;
  // notify
  recipients: string;
  channels: string[];
  subject: string;
  message: string;
  // assign / tag / set_field (+ their shared optional review item)
  assignee: string;
  tag: string;
  severity: string;
  reviewItemId: string;
  // create_draft
  draftDescription: string;
  lines: DraftLineDraft[];
  // post_if_policy_allows
  draftId: string;
  draftIdField: string;
  priority: string;
  enabled: boolean;
}

export function emptyRuleFormValues(): RuleFormValues {
  return {
    name: "",
    description: "",
    eventType: "sales.order.completed",
    combinator: "and",
    rows: [],
    conditionsAdvanced: false,
    rawConditions: "{}",
    actionKind: "notify",
    recipients: "",
    channels: ["in_app"],
    subject: "",
    message: "",
    assignee: "",
    tag: "",
    severity: "warning",
    reviewItemId: "",
    draftDescription: "",
    lines: [emptyDraftLine("line-1", "debit"), emptyDraftLine("line-2", "credit")],
    draftId: "",
    draftIdField: "",
    priority: "100",
    enabled: true,
  };
}

function paramsToDraftLines(params: Record<string, unknown>): DraftLineDraft[] {
  const rawLines = Array.isArray(params.lines) ? (params.lines as unknown[]) : [];
  const lines = rawLines.map((raw, index) => {
    const line = (typeof raw === "object" && raw !== null ? raw : {}) as Record<string, unknown>;
    const debit = line.debit;
    const credit = line.credit;
    const side: "debit" | "credit" = debit !== undefined && debit !== null ? "debit" : "credit";
    const amount = side === "debit" ? debit : credit;
    const base: DraftLineDraft = {
      id: `line-${index + 1}`,
      accountCode: typeof line.account_code === "string" ? line.account_code : "",
      side,
      amountKind: "literal",
      amountToman: "",
      varPath: "amount_irr",
      description: typeof line.description === "string" ? line.description : "",
    };
    if (typeof amount === "number") {
      base.amountToman = irrToTomanString(amount);
    } else if (
      typeof amount === "object" &&
      amount !== null &&
      !Array.isArray(amount) &&
      typeof (amount as Record<string, unknown>).var === "string"
    ) {
      base.amountKind = "var";
      base.varPath = (amount as Record<string, unknown>).var as string;
    }
    return base;
  });
  while (lines.length < 2) {
    lines.push(emptyDraftLine(`line-${lines.length + 1}`, lines.length === 0 ? "debit" : "credit"));
  }
  return lines;
}

function stringList(params: Record<string, unknown>, key: string): string[] {
  const value = params[key];
  if (!Array.isArray(value)) return [];
  return (value as unknown[]).map(String);
}

/** A stored rule → the form's initial values (unsupported DSL trees open in raw-JSON mode). */
export function ruleToFormValues(rule: RuleResponse): RuleFormValues {
  const values = emptyRuleFormValues();
  const { state, unsupported } = conditionsToBuilder(rule.conditions);
  const params = rule.action.params as Record<string, unknown>;
  values.name = rule.name;
  values.description = rule.description;
  values.eventType = rule.event_type;
  values.combinator = state.combinator;
  values.rows = state.rows;
  values.conditionsAdvanced = unsupported;
  values.rawConditions = JSON.stringify(rule.conditions, null, 2);
  values.actionKind = rule.action.kind;
  values.recipients = stringList(params, "recipients").join("، ");
  values.channels = stringList(params, "channels").filter((c) =>
    (NOTIFY_CHANNELS as readonly string[]).includes(c),
  );
  if (values.channels.length === 0) values.channels = ["in_app"];
  values.subject = typeof params.subject === "string" ? params.subject : "";
  values.message = typeof params.message === "string" ? params.message : "";
  values.assignee = typeof params.assignee === "string" ? params.assignee : "";
  values.tag = typeof params.tag === "string" ? params.tag : "";
  values.severity = typeof params.value === "string" ? params.value : "warning";
  values.reviewItemId = typeof params.review_item_id === "string" ? params.review_item_id : "";
  values.draftDescription = typeof params.description === "string" ? params.description : "";
  values.lines = paramsToDraftLines(params);
  values.draftId = typeof params.draft_id === "number" ? String(params.draft_id) : "";
  values.draftIdField = typeof params.draft_id_field === "string" ? params.draft_id_field : "";
  values.priority = String(rule.priority);
  values.enabled = rule.enabled;
  return values;
}

function draftLinesToParams(lines: DraftLineDraft[]): { lines: Record<string, unknown>[] } | { error: string } {
  if (lines.length < 2) return { error: "پیش‌نویس روزنامه دست‌کم ۲ ردیف لازم دارد." };
  const out: Record<string, unknown>[] = [];
  for (const line of lines) {
    if (!line.accountCode.trim()) return { error: "کد حساب هر ردیف را وارد کنید." };
    let amount: number | { var: string };
    if (line.amountKind === "var") {
      if (!isValidFieldPath(line.varPath.trim())) return { error: "مسیر مبلغ متغیر معتبر نیست." };
      amount = { var: line.varPath.trim() };
    } else {
      const irr = parseTomanToIrr(line.amountToman);
      if (irr === null || irr <= 0) return { error: "مبلغ هر ردیف باید عدد مثبت باشد." };
      amount = irr;
    }
    const entry: Record<string, unknown> = {
      account_code: line.accountCode.trim(),
      [line.side]: amount,
    };
    if (line.description.trim()) entry.description = line.description.trim();
    out.push(entry);
  }
  return { lines: out };
}

function formValuesToAction(values: RuleFormValues): { action: RuleAction } | { error: string } {
  const kind = values.actionKind;
  if (!(RULE_ACTION_KINDS as readonly string[]).includes(kind)) {
    return { error: "اقدام انتخاب‌شده در مجموعهٔ مجاز نیست." };
  }
  let params: Record<string, unknown> = {};
  switch (kind) {
    case "create_draft": {
      const converted = draftLinesToParams(values.lines);
      if ("error" in converted) return converted;
      params = { document: "journal", lines: converted.lines };
      if (values.draftDescription.trim()) params.description = values.draftDescription.trim();
      break;
    }
    case "post_if_policy_allows": {
      params = { draft_kind: "journal" };
      if (values.draftId.trim()) {
        const id = Number(values.draftId.trim());
        if (!Number.isInteger(id) || id <= 0) return { error: "شناسهٔ پیش‌نویس باید عدد مثبت باشد." };
        params.draft_id = id;
      }
      if (values.draftIdField.trim()) {
        if (!isValidFieldPath(values.draftIdField.trim())) {
          return { error: "فیلد شناسهٔ پیش‌نویس معتبر نیست." };
        }
        params.draft_id_field = values.draftIdField.trim();
      }
      break;
    }
    case "notify": {
      const recipients = splitListValue(values.recipients);
      if (recipients.length === 0) return { error: "دست‌کم یک گیرنده وارد کنید." };
      if (!values.subject.trim()) return { error: "موضوع اطلاع‌رسانی را وارد کنید." };
      if (!values.message.trim()) return { error: "متن اطلاع‌رسانی را وارد کنید." };
      params = {
        recipients,
        channels: values.channels.length > 0 ? values.channels : ["in_app"],
        subject: values.subject.trim(),
        message: values.message.trim(),
      };
      break;
    }
    case "assign": {
      if (!values.assignee.trim()) return { error: "نام مسئول تخصیص را وارد کنید." };
      params = { assignee: values.assignee.trim() };
      if (values.reviewItemId.trim()) params.review_item_id = values.reviewItemId.trim();
      break;
    }
    case "tag": {
      if (!values.tag.trim()) return { error: "برچسب را وارد کنید." };
      if (values.tag.trim().length > 64) return { error: "برچسب حداکثر ۶۴ نویسه است." };
      params = { tag: values.tag.trim() };
      if (values.reviewItemId.trim()) params.review_item_id = values.reviewItemId.trim();
      break;
    }
    case "set_field": {
      params = { target: "review_item", field: "severity", value: values.severity };
      if (values.reviewItemId.trim()) params.review_item_id = values.reviewItemId.trim();
      break;
    }
    default:
      return { error: "اقدام انتخاب‌شده در مجموعهٔ مجاز نیست." };
  }
  const action: RuleAction = { kind, params };
  const actionError = validateActionParams(action);
  if (actionError !== null) return { error: actionError };
  return { action };
}

function formValuesToConditions(values: RuleFormValues): { conditions: Record<string, unknown> } | { error: string } {
  if (values.conditionsAdvanced) {
    try {
      const parsed: unknown = JSON.parse(values.rawConditions);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        return { error: "شرط‌ها باید یک شیء JSON باشند." };
      }
      return { conditions: parsed as Record<string, unknown> };
    } catch {
      return { error: "JSON شرط‌ها معتبر نیست." };
    }
  }
  for (const row of values.rows) {
    const rowError = validateConditionRow(row);
    if (rowError !== null) return { error: rowError };
  }
  const conditions = rowsToConditions({ combinator: values.combinator, rows: values.rows });
  if (conditions === null) return { error: "شرط‌ها معتبر نیستند." };
  return { conditions };
}

function formValuesToShared(values: RuleFormValues):
  | { name: string; description: string; eventType: string; conditions: Record<string, unknown>; action: RuleAction; priority: number; enabled: boolean }
  | { error: string } {
  if (!values.name.trim()) return { error: "نام قانون را وارد کنید." };
  if (!(RULE_EVENT_TYPES as readonly string[]).includes(values.eventType)) {
    return { error: "رویداد انتخاب‌شده در فهرست رویدادهای مجاز نیست." };
  }
  const priority = Number(values.priority.trim());
  if (!Number.isInteger(priority) || priority < 0) {
    return { error: "اولویت باید عدد صحیح صفر یا بزرگ‌تر باشد." };
  }
  const conditions = formValuesToConditions(values);
  if ("error" in conditions) return conditions;
  const action = formValuesToAction(values);
  if ("error" in action) return action;
  return {
    name: values.name.trim(),
    description: values.description.trim(),
    eventType: values.eventType,
    conditions: conditions.conditions,
    action: action.action,
    priority,
    enabled: values.enabled,
  };
}

/** Form values → the POST /rules body, or the first Persian validation error. */
export function formValuesToCreate(values: RuleFormValues): { body: RuleCreateRequest } | { error: string } {
  const shared = formValuesToShared(values);
  if ("error" in shared) return shared;
  return {
    body: {
      name: shared.name,
      description: shared.description,
      event_type: shared.eventType,
      conditions: shared.conditions,
      action: shared.action,
      priority: shared.priority,
      enabled: shared.enabled,
    },
  };
}

/** Form values → the PUT /rules/{id} body (all fields are sent; the backend updates only given fields). */
export function formValuesToUpdate(values: RuleFormValues): { body: RuleUpdateRequest } | { error: string } {
  const created = formValuesToCreate(values);
  if ("error" in created) return created;
  const { name, description, event_type, conditions, action, priority, enabled } = created.body;
  return { body: { name, description, event_type, conditions, action, priority, enabled } };
}

// ---------------------------------------------------------------------------
// Ownership: user-defined vs system
// ---------------------------------------------------------------------------

export type RuleOwner = "user" | "system";

/**
 * v1 has no seeded system rules — every rule comes through the API
 * with the operator client as created_by. The distinction is still
 * rendered (and forward-compatible): a null/“system” creator reads
 * as a system rule, anything else as user-defined.
 */
export function ruleOwner(rule: Pick<RuleResponse, "created_by">): RuleOwner {
  if (!rule.created_by || rule.created_by === "system") return "system";
  return "user";
}

export const RULE_OWNER_LABELS: Record<RuleOwner, { label: string; tone: StatusTone; description: string }> = {
  user: {
    label: "تعریف کاربر",
    tone: "outline",
    description: "این قانون را کاربر ساخته است.",
  },
  system: {
    label: "سیستمی",
    tone: "secondary",
    description: "این قانون را سیستم ساخته است.",
  },
};

export function ruleOwnerLabel(owner: RuleOwner): string {
  return RULE_OWNER_LABELS[owner].label;
}

// ---------------------------------------------------------------------------
// Execution history + audit log (decisions, outcomes, modes)
// ---------------------------------------------------------------------------

export const RULE_DECISION_LABELS: Record<string, { label: string; tone: StatusTone }> = {
  not_matched: { label: "تطبیق نکرد", tone: "outline" },
  would_execute: { label: "اجرا می‌شد (آزمایشی)", tone: "warning" },
  executed: { label: "اجرا شد", tone: "success" },
  blocked_by_policy_gate: { label: "مسدود توسط درگاه سیاستی", tone: "destructive" },
  skipped: { label: "رد شد", tone: "outline" },
  failed: { label: "ناموفق", tone: "destructive" },
};

export function ruleDecisionLabel(decision: string | null | undefined): string {
  if (!decision) return "نامشخص";
  return RULE_DECISION_LABELS[decision]?.label ?? decision;
}

export function ruleDecisionTone(decision: string | null | undefined): StatusTone {
  if (!decision) return "outline";
  return RULE_DECISION_LABELS[decision]?.tone ?? "outline";
}

export const RULE_OUTCOME_LABELS: Record<string, string> = {
  no_match: "تطبیقی نبود",
  would_execute: "اجرا می‌شد",
  draft_created: "پیش‌نویس ساخته شد",
  posted: "ثبت شد",
  draft_kept: "پیش‌نویس ماند",
  notified: "اطلاع‌رسانی شد",
  assigned: "تخصیص داده شد",
  tagged: "برچسب خورد",
  field_set: "فیلد تنظیم شد",
  no_draft: "پیش‌نویسی نبود",
  no_review_item: "آیتم بازبینی نبود",
  no_company_context: "شرکت مشخص نبود",
  port_not_wired: "اتصال وصل نیست",
  error: "خطا",
};

export function ruleOutcomeLabel(outcome: string | null | undefined): string {
  if (!outcome) return "نامشخص";
  return RULE_OUTCOME_LABELS[outcome] ?? outcome;
}

export const RULE_MODE_LABELS: Record<string, string> = {
  live: "زنده",
  dry_run: "آزمایشی",
};

export function ruleModeLabel(mode: string | null | undefined): string {
  if (!mode) return "نامشخص";
  return RULE_MODE_LABELS[mode] ?? mode;
}

/** Newest execution per rule (the history arrives newest-first, so the first row per rule wins). */
export function lastExecutionByRule(
  executions: RuleExecutionResponse[],
): Map<string, RuleExecutionResponse> {
  const map = new Map<string, RuleExecutionResponse>();
  for (const execution of executions) {
    if (execution.rule_id && !map.has(execution.rule_id)) {
      map.set(execution.rule_id, execution);
    }
  }
  return map;
}

/** The policy-gate verdict carried in an execution's details (dry-run of a post action, or a live post). */
export function gateVerdict(details: Record<string, unknown>): {
  evaluated: boolean;
  wouldPost: boolean | null;
  allowed: boolean | null;
} {
  const evaluated = details.gate_evaluated === true;
  const wouldPost = typeof details.would_post === "boolean" ? details.would_post : null;
  const allowed = typeof details.gate_allowed === "boolean" ? details.gate_allowed : null;
  return { evaluated, wouldPost, allowed };
}

export interface ExecutionDetailEntry {
  label: string;
  value: string;
  /** Identifiers and codes render LTR. */
  ltr?: boolean;
}

/**
 * An execution's details → Persian label/value rows for the audit
 * view. Known keys get Persian labels; the backend's English free
 * text (would_do, gate_reason, reason, channel detail) is never
 * rendered raw — the outcome/decision labels and the Persian action
 * description carry that meaning. Unknown keys still show (raw key
 * + JSON value) so no evidence silently disappears.
 */
export function executionDetailEntries(execution: RuleExecutionResponse): ExecutionDetailEntry[] {
  const details = execution.details as Record<string, unknown>;
  const entries: ExecutionDetailEntry[] = [];
  const seen = new Set<string>();

  const take = (key: string, build: (value: unknown) => ExecutionDetailEntry | null) => {
    if (!(key in details)) return;
    seen.add(key);
    const entry = build(details[key]);
    if (entry !== null) entries.push(entry);
  };

  take("draft_ref", (v) =>
    v === null || v === undefined ? null : { label: "مرجع پیش‌نویس", value: String(v), ltr: true },
  );
  take("posted_ref", (v) =>
    v === null || v === undefined ? null : { label: "مرجع ثبت", value: String(v), ltr: true },
  );
  take("company_engine_id", (v) =>
    typeof v === "number" ? { label: "شناسهٔ شرکت (موتور)", value: faNumber(v), ltr: true } : null,
  );
  take("review_item_id", (v) =>
    typeof v === "string" ? { label: "آیتم بازبینی", value: v, ltr: true } : null,
  );
  take("assignee", (v) => (typeof v === "string" ? { label: "تخصیص به", value: v } : null));
  take("tag", (v) => (typeof v === "string" ? { label: "برچسب", value: v } : null));
  take("tags", (v) =>
    Array.isArray(v) ? { label: "برچسب‌ها", value: (v as unknown[]).map(String).join("، ") } : null,
  );
  take("field", (v) => (v === "severity" ? { label: "فیلد", value: "شدت" } : null));
  take("value", (v) =>
    typeof v === "string" && details.field === "severity"
      ? { label: "مقدار جدید", value: ruleSeverityLabel(v) }
      : null,
  );
  take("policy_version", (v) =>
    typeof v === "string" ? { label: "نسخهٔ سیاست", value: v, ltr: true } : null,
  );
  take("gate_evaluated", (v) =>
    v === false ? { label: "درگاه سیاستی", value: "ارزیابی نشد — ثبت به‌صورت بسته انجام می‌شد" } : null,
  );
  take("gate_allowed", (v) =>
    typeof v === "boolean" ? { label: "رأی درگاه", value: v ? "اجازه داد" : "رد کرد" } : null,
  );
  take("would_post", (v) =>
    typeof v === "boolean" ? { label: "ثبت انجام می‌شد؟", value: v ? "بله" : "خیر" } : null,
  );
  take("error_code", (v) =>
    typeof v === "string" ? { label: "کد خطا", value: v, ltr: true } : null,
  );
  take("gate_layers", (v) => {
    if (!Array.isArray(v)) return null;
    for (const raw of v as unknown[]) {
      if (typeof raw !== "object" || raw === null) continue;
      const layer = raw as Record<string, unknown>;
      entries.push({
        label: `لایهٔ ${typeof layer.name === "string" ? layer.name : "نامشخص"}`,
        value: layer.passed === true ? "گذشت" : "رد شد",
      });
    }
    return null;
  });
  take("channels", (v) => {
    if (!Array.isArray(v)) return null;
    for (const raw of v as unknown[]) {
      if (typeof raw !== "object" || raw === null) continue;
      const result = raw as Record<string, unknown>;
      entries.push({
        label: `کانال ${notifyChannelLabel(typeof result.channel === "string" ? result.channel : null)}`,
        value: notifyChannelStatusLabel(typeof result.status === "string" ? result.status : null),
      });
    }
    return null;
  });

  // English free text is never rendered raw (see the doc comment).
  for (const key of ["would_do", "gate_reason", "reason"]) seen.add(key);

  for (const [key, value] of Object.entries(details)) {
    if (seen.has(key)) continue;
    if (value === null || value === undefined) continue;
    entries.push({ label: key, value: JSON.stringify(value), ltr: true });
  }
  return entries;
}

// ---------------------------------------------------------------------------
// Rule-set match modes
// ---------------------------------------------------------------------------

export const RULE_SET_MODES = ["first_match", "all_matches"] as const;

export type RuleSetModeCode = (typeof RULE_SET_MODES)[number];

export const RULE_SET_MODE_LABELS: Record<string, { label: string; description: string }> = {
  first_match: {
    label: "اولین تطبیق",
    description: "فقط اولین قانونِ مطابق (با کمترین اولویت) اجرا می‌شود؛ الگوی زرو.",
  },
  all_matches: {
    label: "همهٔ تطبیق‌ها",
    description: "همهٔ قوانین مطابق اجرا می‌شوند.",
  },
};

export function ruleSetModeLabel(mode: string | null | undefined): string {
  if (!mode) return "نامشخص";
  return RULE_SET_MODE_LABELS[mode]?.label ?? mode;
}

export function ruleSetModeDescription(mode: string | null | undefined): string | null {
  if (!mode) return null;
  return RULE_SET_MODE_LABELS[mode]?.description ?? null;
}

// ---------------------------------------------------------------------------
// Dry-run sample payloads
// ---------------------------------------------------------------------------

export interface SamplePayloadInput {
  amountToman: string;
  channel: string;
  partyId: string;
  extraJson: string;
}

/**
 * The dry-run panel's sample fields → the payload the backend
 * evaluates. Amounts are whole Rial on the wire (the operator types
 * تومان); total_amount is the fact the engine reads first for
 * amount_irr. Extra JSON merges in last (it wins on conflicts).
 */
export function buildSamplePayload(
  input: SamplePayloadInput,
): { payload: Record<string, unknown> } | { error: string } {
  const payload: Record<string, unknown> = {};
  if (input.amountToman.trim()) {
    const irr = parseTomanToIrr(input.amountToman);
    if (irr === null) return { error: "مبلغ نمونه باید عدد باشد." };
    payload.total_amount = irr;
  }
  if (input.channel.trim()) payload.payment_channel = input.channel.trim();
  if (input.partyId.trim()) payload.party_id = input.partyId.trim();
  if (input.extraJson.trim()) {
    try {
      const parsed: unknown = JSON.parse(input.extraJson);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        return { error: "JSON اضافی باید یک شیء باشد." };
      }
      Object.assign(payload, parsed as Record<string, unknown>);
    } catch {
      return { error: "JSON اضافی معتبر نیست." };
    }
  }
  return { payload };
}

/** The dry-run / live-evaluate panel's input: a past event OR a sample payload, never both. */
export interface EventTestInput {
  mode: "sample" | "past";
  amountToman: string;
  channel: string;
  partyId: string;
  extraJson: string;
  eventId: string;
}

export function emptyEventTestInput(): EventTestInput {
  return { mode: "sample", amountToman: "", channel: "", partyId: "", extraJson: "", eventId: "" };
}

/** Panel input → the dry-run/evaluate request body (the sample's event_type is the rule's). */
export function eventTestInputToRequest(
  input: EventTestInput,
  eventType: string,
): { request: { event_id?: string; event_type?: string; payload?: Record<string, unknown> } } | { error: string } {
  if (input.mode === "past") {
    if (!input.eventId.trim()) return { error: "شناسهٔ رویداد گذشته را وارد کنید." };
    return { request: { event_id: input.eventId.trim() } };
  }
  const built = buildSamplePayload(input);
  if ("error" in built) return built;
  return { request: { event_type: eventType, payload: built.payload } };
}
