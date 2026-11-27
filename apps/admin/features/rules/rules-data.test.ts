/**
 * Pure-helper tests for the rules management UI (US-562).
 *
 * The condition builder round-trips (rows ⇄ DSL tree), the action
 * descriptions and validation mirror the backend's closed set, and
 * the audit-log labels cover every decision/outcome the engine
 * emits. No dates depend on "today".
 */

import { describe, expect, it } from "vitest";
import type { RuleAction, RuleExecutionResponse, RuleResponse } from "@/lib/api-client";
import {
  RULE_ACTION_KINDS,
  RULE_EVENT_TYPES,
  buildSamplePayload,
  conditionsToBuilder,
  describeActionFa,
  describeConditionsFa,
  emptyEventTestInput,
  eventTestInputToRequest,
  executionDetailEntries,
  formValuesToCreate,
  formValuesToUpdate,
  gateVerdict,
  irrToTomanString,
  isValidFieldPath,
  lastExecutionByRule,
  parseTomanToIrr,
  rowsToConditions,
  ruleOwner,
  ruleToFormValues,
  validateActionParams,
  validateConditionRow,
  type ConditionRow,
  type RuleFormValues,
} from "./rules-data";

function rule(overrides: Partial<RuleResponse> = {}): RuleResponse {
  return {
    id: "rule-1",
    tenant_id: "tenant-1",
    installation_id: null,
    name: "قانون آزمایشی",
    description: "",
    event_type: "sales.order.completed",
    conditions: {},
    action: { kind: "notify", params: { recipients: ["ops"], channels: ["in_app"], subject: "س", message: "م" } },
    priority: 100,
    enabled: true,
    created_by: "operator-1",
    created_at: "2026-11-21T10:00:00Z",
    updated_at: null,
    ...overrides,
  };
}

function execution(overrides: Partial<RuleExecutionResponse> = {}): RuleExecutionResponse {
  return {
    id: "exec-1",
    tenant_id: "tenant-1",
    rule_id: "rule-1",
    event_id: null,
    event_type: "sales.order.completed",
    mode: "live",
    decision: "executed",
    outcome: "notified",
    action_kind: "notify",
    details: {},
    error: null,
    executed_by: "operator-1",
    executed_at: "2026-11-21T10:00:00Z",
    ...overrides,
  };
}

function notifyForm(overrides: Partial<RuleFormValues> = {}): RuleFormValues {
  return {
    name: "قانون آزمایشی",
    description: "",
    eventType: "sales.order.completed",
    combinator: "and",
    rows: [],
    conditionsAdvanced: false,
    rawConditions: "{}",
    actionKind: "notify",
    recipients: "ops",
    channels: ["in_app"],
    subject: "موضوع",
    message: "متن",
    assignee: "",
    tag: "",
    severity: "warning",
    reviewItemId: "",
    draftDescription: "",
    lines: [
      { id: "line-1", accountCode: "", side: "debit", amountKind: "literal", amountToman: "", varPath: "amount_irr", description: "" },
      { id: "line-2", accountCode: "", side: "credit", amountKind: "literal", amountToman: "", varPath: "amount_irr", description: "" },
    ],
    draftId: "",
    draftIdField: "",
    priority: "100",
    enabled: true,
    ...overrides,
  };
}

describe("canonical event types + closed action set", () => {
  it("lists exactly the twelve canonical event types", () => {
    expect(RULE_EVENT_TYPES).toHaveLength(12);
    expect(RULE_EVENT_TYPES).toContain("installment.schedule.created");
    expect(RULE_EVENT_TYPES).toContain("cheque.received.registered");
  });

  it("lists exactly the six closed actions", () => {
    expect(RULE_ACTION_KINDS).toHaveLength(6);
    expect(RULE_ACTION_KINDS).not.toContain("execute_code");
  });
});

describe("parseTomanToIrr / irrToTomanString", () => {
  it("converts Persian-digit تومان with separators to whole Rial", () => {
    expect(parseTomanToIrr("۱٬۰۰۰٬۰۰۰")).toBe(10_000_000);
    expect(parseTomanToIrr("150000")).toBe(1_500_000);
    expect(parseTomanToIrr("۱۰۰ تومان")).toBe(1000);
  });

  it("accepts a decimal تومان only when it maps to integer Rial", () => {
    expect(parseTomanToIrr("100.5")).toBe(1005);
    expect(parseTomanToIrr("100.05")).toBeNull();
  });

  it("rejects non-numeric input", () => {
    expect(parseTomanToIrr("")).toBeNull();
    expect(parseTomanToIrr("abc")).toBeNull();
    expect(parseTomanToIrr("12x")).toBeNull();
  });

  it("renders Rial back to تومان without a trailing decimal", () => {
    expect(irrToTomanString(1_500_000)).toBe("150000");
    expect(irrToTomanString(1005)).toBe("100.5");
  });
});

describe("isValidFieldPath", () => {
  it("accepts up to 5 dotted segments of word chars", () => {
    expect(isValidFieldPath("amount_irr")).toBe(true);
    expect(isValidFieldPath("payload.total_amount")).toBe(true);
    expect(isValidFieldPath("a.b.c.d.e")).toBe(true);
  });

  it("rejects empty, overlong and malformed paths", () => {
    expect(isValidFieldPath("")).toBe(false);
    expect(isValidFieldPath("a.b.c.d.e.f")).toBe(false);
    expect(isValidFieldPath("payload..total")).toBe(false);
    expect(isValidFieldPath("payload.total-amount!")).toBe(false);
    expect(isValidFieldPath("payload total")).toBe(false);
  });
});

describe("condition rows ⇄ DSL tree", () => {
  const amountRow: ConditionRow = { id: "r1", field: "amount_irr", operator: ">=", value: "۱٬۰۰۰٬۰۰۰" };
  const channelRow: ConditionRow = { id: "r2", field: "channel", operator: "==", value: "zarinpal" };

  it("builds {} from no rows", () => {
    expect(rowsToConditions({ combinator: "and", rows: [] })).toEqual({});
  });

  it("builds a bare comparison from one row (تومان → Rial)", () => {
    expect(rowsToConditions({ combinator: "and", rows: [amountRow] })).toEqual({
      ">=": [{ var: "amount_irr" }, 10_000_000],
    });
  });

  it("builds an and/or group from several rows", () => {
    expect(rowsToConditions({ combinator: "and", rows: [amountRow, channelRow] })).toEqual({
      and: [{ ">=": [{ var: "amount_irr" }, 10_000_000] }, { "==": [{ var: "channel" }, "zarinpal"] }],
    });
    expect(rowsToConditions({ combinator: "or", rows: [amountRow, channelRow] })).toEqual({
      or: [{ ">=": [{ var: "amount_irr" }, 10_000_000] }, { "==": [{ var: "channel" }, "zarinpal"] }],
    });
  });

  it("builds in/not_in lists (split on Latin or Persian commas)", () => {
    const row: ConditionRow = { id: "r1", field: "channel", operator: "in", value: "zarinpal، melli" };
    expect(rowsToConditions({ combinator: "and", rows: [row] })).toEqual({
      in: [{ var: "channel" }, ["zarinpal", "melli"]],
    });
    const amounts: ConditionRow = { id: "r1", field: "amount_irr", operator: "in", value: "100، 200" };
    expect(rowsToConditions({ combinator: "and", rows: [amounts] })).toEqual({
      in: [{ var: "amount_irr" }, [1000, 2000]],
    });
  });

  it("coerces payload.* integers and booleans, but never channel/party text", () => {
    const numeric: ConditionRow = { id: "r1", field: "payload.count", operator: ">", value: "5" };
    expect(rowsToConditions({ combinator: "and", rows: [numeric] })).toEqual({
      ">": [{ var: "payload.count" }, 5],
    });
    const flag: ConditionRow = { id: "r1", field: "payload.paid", operator: "==", value: "true" };
    expect(rowsToConditions({ combinator: "and", rows: [flag] })).toEqual({
      "==": [{ var: "payload.paid" }, true],
    });
    const numericChannel: ConditionRow = { id: "r1", field: "channel", operator: "==", value: "123" };
    expect(rowsToConditions({ combinator: "and", rows: [numericChannel] })).toEqual({
      "==": [{ var: "channel" }, "123"],
    });
  });

  it("returns null when any row is invalid", () => {
    const bad: ConditionRow = { id: "r1", field: "amount_irr", operator: ">=", value: "abc" };
    expect(rowsToConditions({ combinator: "and", rows: [bad] })).toBeNull();
    expect(rowsToConditions({ combinator: "and", rows: [amountRow, bad] })).toBeNull();
  });

  it("parses a stored tree back into rows (round-trip)", () => {
    const tree = {
      and: [{ ">=": [{ var: "amount_irr" }, 10_000_000] }, { "==": [{ var: "channel" }, "zarinpal"] }],
    };
    const { state, unsupported } = conditionsToBuilder(tree);
    expect(unsupported).toBe(false);
    expect(state.combinator).toBe("and");
    expect(state.rows).toHaveLength(2);
    expect(state.rows[0]).toMatchObject({ field: "amount_irr", operator: ">=", value: "1000000" });
    expect(state.rows[1]).toMatchObject({ field: "channel", operator: "==", value: "zarinpal" });
    expect(rowsToConditions(state)).toEqual(tree);
  });

  it("parses {} and a single comparison", () => {
    expect(conditionsToBuilder({})).toEqual({ state: { combinator: "and", rows: [] }, unsupported: false });
    const { state, unsupported } = conditionsToBuilder({ "==": [{ var: "channel" }, "zarinpal"] });
    expect(unsupported).toBe(false);
    expect(state.rows).toHaveLength(1);
  });

  it("reports not/nesting/var-on-right as unsupported (never silently rewritten)", () => {
    expect(conditionsToBuilder({ not: { "==": [{ var: "channel" }, "zarinpal"] } }).unsupported).toBe(true);
    expect(
      conditionsToBuilder({ and: [{ or: [{ "==": [{ var: "channel" }, "zarinpal"] }] }] }).unsupported,
    ).toBe(true);
    expect(conditionsToBuilder({ "==": ["zarinpal", { var: "channel" }] }).unsupported).toBe(true);
    expect(conditionsToBuilder({ "==": [{ var: "channel" }, { var: "party" }] }).unsupported).toBe(true);
  });
});

describe("validateConditionRow", () => {
  it("accepts a valid row", () => {
    expect(
      validateConditionRow({ id: "r1", field: "amount_irr", operator: ">=", value: "1000" }),
    ).toBeNull();
  });

  it("rejects an empty field, a bad path, an empty value and a bad amount", () => {
    expect(validateConditionRow({ id: "r1", field: "", operator: "==", value: "x" })).toContain("فیلد");
    expect(validateConditionRow({ id: "r1", field: "a..b", operator: "==", value: "x" })).toContain("مسیر");
    expect(validateConditionRow({ id: "r1", field: "channel", operator: "==", value: "" })).toContain("مقدار");
    expect(validateConditionRow({ id: "r1", field: "amount_irr", operator: ">=", value: "abc" })).toContain("مبلغ");
  });

  it("rejects several values for a single-value operator", () => {
    expect(
      validateConditionRow({ id: "r1", field: "channel", operator: "==", value: "a، b" }),
    ).toContain("یک مقدار");
  });

  it("requires an ISO date for occurred_at", () => {
    expect(
      validateConditionRow({ id: "r1", field: "occurred_at", operator: ">=", value: "۱۴۰۵/۰۸/۳۰" }),
    ).toContain("ISO");
    expect(
      validateConditionRow({ id: "r1", field: "occurred_at", operator: ">=", value: "2026-11-21" }),
    ).toBeNull();
  });
});

describe("describeConditionsFa", () => {
  it("describes no conditions, rows and advanced trees", () => {
    expect(describeConditionsFa({})).toContain("بدون شرط");
    expect(describeConditionsFa({ ">=": [{ var: "amount_irr" }, 10_000_000] })).toBe(
      "مبلغ رویداد بزرگ‌تر یا مساوی ۱٬۰۰۰٬۰۰۰ تومان",
    );
    expect(
      describeConditionsFa({
        and: [{ ">=": [{ var: "amount_irr" }, 10_000_000] }, { "==": [{ var: "channel" }, "zarinpal"] }],
      }),
    ).toContain(" و ");
    expect(describeConditionsFa({ not: { "==": [{ var: "channel" }, "zarinpal"] } })).toContain("پیشرفته");
  });
});

describe("describeActionFa", () => {
  it("describes every closed action in Persian", () => {
    expect(
      describeActionFa({
        kind: "create_draft",
        params: { document: "journal", lines: [{ account_code: "1", debit: 1 }, { account_code: "2", credit: 1 }] },
      }),
    ).toContain("۲ ردیف");
    expect(describeActionFa({ kind: "post_if_policy_allows", params: {} })).toContain("درگاه سیاستی");
    expect(
      describeActionFa({
        kind: "notify",
        params: { recipients: ["ops", "admin"], channels: ["in_app", "email"], subject: "s", message: "m" },
      }),
    ).toBe("اطلاع‌رسانی به ops، admin از طریق درون‌برنامه‌ای، ایمیل");
    expect(describeActionFa({ kind: "assign", params: { assignee: "سارا" } })).toContain("سارا");
    expect(describeActionFa({ kind: "tag", params: { tag: "فوری" } })).toContain("فوری");
    expect(
      describeActionFa({ kind: "set_field", params: { target: "review_item", field: "severity", value: "critical" } }),
    ).toContain("بحرانی");
  });
});

describe("validateActionParams", () => {
  it("accepts a valid action of every kind", () => {
    const valid: RuleAction[] = [
      {
        kind: "create_draft",
        params: {
          document: "journal",
          lines: [
            { account_code: "101", debit: 1000 },
            { account_code: "202", credit: { var: "amount_irr" } },
          ],
        },
      },
      { kind: "post_if_policy_allows", params: {} },
      { kind: "notify", params: { recipients: ["ops"], channels: ["in_app"], subject: "s", message: "m" } },
      { kind: "assign", params: { assignee: "سارا" } },
      { kind: "tag", params: { tag: "فوری" } },
      { kind: "set_field", params: { target: "review_item", field: "severity", value: "info" } },
    ];
    for (const action of valid) expect(validateActionParams(action)).toBeNull();
  });

  it("rejects an unknown kind, one-line drafts, two-sided lines and bad notify params", () => {
    expect(validateActionParams({ kind: "execute_code", params: {} })).toContain("مجموعهٔ مجاز");
    expect(
      validateActionParams({ kind: "create_draft", params: { lines: [{ account_code: "1", debit: 1 }] } }),
    ).toContain("۲ ردیف");
    expect(
      validateActionParams({
        kind: "create_draft",
        params: {
          lines: [
            { account_code: "1", debit: 1, credit: 1 },
            { account_code: "2", credit: 1 },
          ],
        },
      }),
    ).toContain("دقیقاً یک سمت");
    expect(
      validateActionParams({ kind: "notify", params: { recipients: [], subject: "s", message: "m" } }),
    ).toContain("گیرنده");
    expect(validateActionParams({ kind: "tag", params: { tag: "x".repeat(65) } })).toContain("۶۴");
  });
});

describe("rule ⇄ form values", () => {
  it("round-trips a stored rule through the form", () => {
    const stored = rule({
      conditions: { ">=": [{ var: "amount_irr" }, 10_000_000] },
      action: { kind: "notify", params: { recipients: ["ops"], channels: ["in_app"], subject: "س", message: "م" } },
    });
    const values = ruleToFormValues(stored);
    expect(values.rows).toHaveLength(1);
    expect(values.rows[0]).toMatchObject({ field: "amount_irr", value: "1000000" });
    expect(values.conditionsAdvanced).toBe(false);
    const created = formValuesToCreate(values);
    expect(created).toEqual({
      body: {
        name: "قانون آزمایشی",
        description: "",
        event_type: "sales.order.completed",
        conditions: { ">=": [{ var: "amount_irr" }, 10_000_000] },
        action: { kind: "notify", params: { recipients: ["ops"], channels: ["in_app"], subject: "س", message: "م" } },
        priority: 100,
        enabled: true,
      },
    });
  });

  it("opens an advanced DSL tree in raw-JSON mode and keeps it byte-identical", () => {
    const tree = { not: { "==": [{ var: "channel" }, "zarinpal"] } };
    const values = ruleToFormValues(rule({ conditions: tree }));
    expect(values.conditionsAdvanced).toBe(true);
    const created = formValuesToCreate(values);
    expect(created).toEqual(expect.objectContaining({ body: expect.objectContaining({ conditions: tree }) }));
  });

  it("rejects an empty name, an unknown event type and a bad priority", () => {
    expect(formValuesToCreate(notifyForm({ name: "  " }))).toEqual({ error: expect.stringContaining("نام قانون") });
    expect(formValuesToCreate(notifyForm({ eventType: "store.custom" }))).toEqual({
      error: expect.stringContaining("رویداد"),
    });
    expect(formValuesToCreate(notifyForm({ priority: "-1" }))).toEqual({
      error: expect.stringContaining("اولویت"),
    });
  });

  it("builds a create_draft action from literal + var lines (تومان → Rial)", () => {
    const values = notifyForm({
      actionKind: "create_draft",
      draftDescription: "سند آزمایشی",
      lines: [
        { id: "l1", accountCode: "101", side: "debit", amountKind: "literal", amountToman: "۱۰۰۰", varPath: "amount_irr", description: "" },
        { id: "l2", accountCode: "202", side: "credit", amountKind: "var", amountToman: "", varPath: "amount_irr", description: "" },
      ],
    });
    const created = formValuesToCreate(values);
    expect(created).toEqual({
      body: expect.objectContaining({
        action: {
          kind: "create_draft",
          params: {
            document: "journal",
            lines: [
              { account_code: "101", debit: 10000 },
              { account_code: "202", credit: { var: "amount_irr" } },
            ],
            description: "سند آزمایشی",
          },
        },
      }),
    });
  });

  it("formValuesToUpdate sends the same fields as create", () => {
    const values = notifyForm({ name: "ویرایش‌شده" });
    const updated = formValuesToUpdate(values);
    expect(updated).toEqual({ body: expect.objectContaining({ name: "ویرایش‌شده" }) });
  });
});

describe("ruleOwner", () => {
  it("reads null/system creators as system, operator clients as user-defined", () => {
    expect(ruleOwner(rule({ created_by: null }))).toBe("system");
    expect(ruleOwner(rule({ created_by: "system" }))).toBe("system");
    expect(ruleOwner(rule({ created_by: "operator-1" }))).toBe("user");
  });
});

describe("lastExecutionByRule", () => {
  it("keeps the newest execution per rule (list is newest-first)", () => {
    const map = lastExecutionByRule([
      execution({ id: "e-new", rule_id: "rule-1", decision: "executed" }),
      execution({ id: "e-old", rule_id: "rule-1", decision: "failed" }),
      execution({ id: "e-2", rule_id: "rule-2", decision: "skipped" }),
      execution({ id: "e-orphan", rule_id: null, decision: "would_execute" }),
    ]);
    expect(map.get("rule-1")?.id).toBe("e-new");
    expect(map.get("rule-2")?.id).toBe("e-2");
    expect(map.size).toBe(2);
  });
});

describe("gateVerdict + executionDetailEntries", () => {
  it("reads the gate verdict from details", () => {
    expect(gateVerdict({ gate_evaluated: true, would_post: false, gate_allowed: false })).toEqual({
      evaluated: true,
      wouldPost: false,
      allowed: false,
    });
    expect(gateVerdict({})).toEqual({ evaluated: false, wouldPost: null, allowed: null });
  });

  it("renders known detail keys in Persian and never renders English free text raw", () => {
    const entries = executionDetailEntries(
      execution({
        details: {
          would_do: "Notify ops via in_app.",
          gate_reason: "No policy gate is wired.",
          reason: "No draft was created.",
          draft_ref: 42,
          gate_allowed: false,
          policy_version: "registry-v1",
          gate_layers: [{ name: "fiscal_period", passed: true, reason: "ok" }],
          channels: [{ channel: "email", status: "skipped", detail: "No SMTP backend." }],
        },
      }),
    );
    const text = entries.map((e) => `${e.label}: ${e.value}`).join("\n");
    expect(text).toContain("مرجع پیش‌نویس: 42");
    expect(text).toContain("رأی درگاه: رد کرد");
    expect(text).toContain("کانال ایمیل: ارسال نشد (بک‌اند ندارد)");
    expect(text).not.toContain("Notify ops");
    expect(text).not.toContain("No policy gate");
    expect(text).not.toContain("No draft was created");
    expect(text).not.toContain("No SMTP backend");
  });

  it("still shows unknown keys (raw key + JSON value) so no evidence disappears", () => {
    const entries = executionDetailEntries(execution({ details: { future_key: "future-value" } }));
    expect(entries).toContainEqual({ label: "future_key", value: '"future-value"', ltr: true });
  });
});

describe("buildSamplePayload", () => {
  it("builds a payload from the sample fields (تومان → Rial)", () => {
    expect(
      buildSamplePayload({ amountToman: "۱٬۵۰۰٬۰۰۰", channel: "zarinpal", partyId: "cust-1", extraJson: "" }),
    ).toEqual({
      payload: { total_amount: 15_000_000, payment_channel: "zarinpal", party_id: "cust-1" },
    });
  });

  it("merges extra JSON last and rejects bad JSON / a bad amount", () => {
    expect(
      buildSamplePayload({ amountToman: "", channel: "", partyId: "", extraJson: '{"total_amount": 5}' }),
    ).toEqual({ payload: { total_amount: 5 } });
    expect(
      buildSamplePayload({ amountToman: "", channel: "", partyId: "", extraJson: "{nope" }),
    ).toEqual({ error: expect.stringContaining("JSON") });
    expect(
      buildSamplePayload({ amountToman: "abc", channel: "", partyId: "", extraJson: "" }),
    ).toEqual({ error: expect.stringContaining("مبلغ") });
  });
});

describe("eventTestInputToRequest", () => {
  it("builds a sample-payload request with the rule's event type", () => {
    expect(
      eventTestInputToRequest(
        { ...emptyEventTestInput(), amountToman: "1000", channel: "zarinpal" },
        "sales.order.completed",
      ),
    ).toEqual({
      request: {
        event_type: "sales.order.completed",
        payload: { total_amount: 10000, payment_channel: "zarinpal" },
      },
    });
  });

  it("builds a past-event request from the event id only", () => {
    expect(
      eventTestInputToRequest({ ...emptyEventTestInput(), mode: "past", eventId: "evt-1" }, "sales.order.completed"),
    ).toEqual({ request: { event_id: "evt-1" } });
  });

  it("requires the event id in past mode and a valid amount in sample mode", () => {
    expect(
      eventTestInputToRequest({ ...emptyEventTestInput(), mode: "past" }, "sales.order.completed"),
    ).toEqual({ error: expect.stringContaining("شناسهٔ رویداد") });
    expect(
      eventTestInputToRequest({ ...emptyEventTestInput(), amountToman: "abc" }, "sales.order.completed"),
    ).toEqual({ error: expect.stringContaining("مبلغ") });
  });
});
