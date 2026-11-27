/**
 * MCP client-management labels, form model and validation (US-606).
 *
 * The scope checklist offers the scopes an MCP client can carry:
 * `mcp:connect` (mandatory — without it the key cannot open a
 * session) plus the read scopes the MCP tools enforce. Write scopes
 * are deliberately absent: the MCP server is read-only by design
 * (spec §13.2 — «read-only tools»), and US-605's draft tools are
 * draft-only; no MCP client should ever hold a write scope.
 *
 * Persian labels only — the scope code itself stays Latin in the
 * payload and is shown next to the label for operators.
 */

import type { McpClientCreateRequest } from "@/lib/api-client";

export const MCP_CONNECT_SCOPE = "mcp:connect";

/** Scopes an MCP client may carry, in the order the checklist shows them. */
export const MCP_CLIENT_SCOPES: { scope: string; label: string }[] = [
  { scope: "mcp:connect", label: "اتصال MCP" },
  { scope: "company:read", label: "خواندن شرکت‌ها" },
  { scope: "account:read", label: "خواندن حساب‌ها" },
  { scope: "ledger:read", label: "خواندن دفتر کل" },
  { scope: "reports:read", label: "خواندن گزارش‌ها" },
  { scope: "banking:read", label: "خواندن بانک" },
  { scope: "compliance:read", label: "خواندن انطباق" },
  { scope: "standards:read", label: "خواندن استانداردها" },
  { scope: "standards:validate", label: "اعتبارسنجی استانداردها" },
  { scope: "workflow:read", label: "خواندن گردش کار" },
  { scope: "review:read", label: "خواندن صف بررسی" },
  { scope: "settlement:read", label: "خواندن تسویه‌ها" },
  { scope: "cheques:read", label: "خواندن چک‌ها" },
  { scope: "packaging:read", label: "خواندن بسته‌بندی" },
  { scope: "cashbox:read", label: "خواندن صندوق" },
  { scope: "tax:read", label: "خواندن مالیات" },
  { scope: "integrations:read", label: "خواندن یکپارچه‌سازی‌ها" },
];

export const MCP_SCOPE_LABELS: Record<string, string> = Object.fromEntries(
  MCP_CLIENT_SCOPES.map(({ scope, label }) => [scope, label]),
);

/** Persian label for a scope; unknown scopes fall back to the raw code. */
export function scopeLabelFa(scope: string): string {
  return MCP_SCOPE_LABELS[scope] ?? scope;
}

export const MCP_AUDIT_KIND_LABELS: Record<string, string> = {
  tool: "ابزار",
  resource: "منبع",
  prompt: "پرامپت",
  management: "مدیریت",
};

export const MCP_AUDIT_OUTCOME_LABELS: Record<string, string> = {
  allowed: "مجاز",
  denied: "رد شد",
  error: "خطا",
};

export const MCP_AUDIT_TRANSPORT_LABELS: Record<string, string> = {
  stdio: "stdio",
  "streamable-http": "HTTP جریانی",
  memory: "داخلی",
  rest: "پنل مدیریت",
};

/** The lifecycle tools the audit view can filter to. */
export const MCP_MANAGEMENT_TOOLS: { value: string; label: string }[] = [
  { value: "mcp.client.create", label: "ایجاد کلاینت" },
  { value: "mcp.client.rotate", label: "چرخش کلید" },
  { value: "mcp.client.revoke", label: "ابطال کلاینت" },
];

export const MCP_EXPIRY_OPTIONS: { days: number; label: string }[] = [
  { days: 0, label: "بدون انقضا" },
  { days: 30, label: "۳۰ روز" },
  { days: 90, label: "۹۰ روز" },
  { days: 365, label: "۱ سال" },
];

export interface McpClientFormValues {
  name: string;
  description: string;
  /** Scopes checked by the operator; mcp:connect is always added at submit. */
  scopes: string[];
  /** Row id of the granted company, or "all" for every tenant company. */
  companyId: string;
  /** 0 = never expires. */
  expiryDays: number;
}

export interface McpClientFormErrors {
  name?: string;
  scopes?: string;
}

export function emptyMcpClientForm(): McpClientFormValues {
  return {
    name: "",
    description: "",
    scopes: [MCP_CONNECT_SCOPE, "company:read", "reports:read"],
    companyId: "all",
    expiryDays: 0,
  };
}

/** Backend rule: name is 3–100 chars after trimming. */
export function validateMcpClientForm(values: McpClientFormValues): McpClientFormErrors {
  const errors: McpClientFormErrors = {};
  const name = values.name.trim();
  if (name.length < 3) {
    errors.name = "نام کلاینت باید دست‌کم ۳ نویسه باشد.";
  } else if (name.length > 100) {
    errors.name = "نام کلاینت نمی‌تواند بیش از ۱۰۰ نویسه باشد.";
  }
  if (values.scopes.length === 0) {
    errors.scopes = "دست‌کم یک دسترسی انتخاب کنید.";
  }
  return errors;
}

/**
 * Convert the form into the create payload. `mcp:connect` is forced
 * (the backend rejects a scope set without it), the company grant is
 * omitted for "all", and the expiry is an ISO timestamp or null.
 */
export function mcpClientFormToCreate(
  values: McpClientFormValues,
  now: Date = new Date(),
): McpClientCreateRequest {
  const scopes = Array.from(new Set([MCP_CONNECT_SCOPE, ...values.scopes])).sort();
  return {
    name: values.name.trim(),
    description: values.description.trim() === "" ? null : values.description.trim(),
    scopes,
    company_id: values.companyId === "all" ? null : values.companyId,
    expires_at:
      values.expiryDays === 0
        ? null
        : new Date(now.getTime() + values.expiryDays * 24 * 60 * 60 * 1000).toISOString(),
  };
}
