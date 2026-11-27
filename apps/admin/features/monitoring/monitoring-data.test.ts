import { describe, expect, it } from "vitest";
import type {
  MonitoringAlertResponse,
  MonitoringProcessHealth,
} from "@/lib/api-client";
import {
  alertCountBySeverity,
  alertRuleLabelFa,
  alertSeverityLabelFa,
  alertStatusLabelFa,
  backupDatabaseLabelFa,
  backupStatusLabelFa,
  channelFormToRequest,
  emptyChannelForm,
  emptyTokenForm,
  formatDaysLeft,
  formatPercent,
  formatRial,
  processLabelFa,
  processStatusLabelFa,
  tokenFormToRequest,
  validateChannelForm,
  validateTokenForm,
  worstProcessStatus,
} from "./monitoring-data";

function process(name: string, status: string): MonitoringProcessHealth {
  return { name, status: status as MonitoringProcessHealth["status"], detail: null, checked_at: null };
}

function alert(severity: string, status: string): MonitoringAlertResponse {
  return {
    id: `${severity}-${status}`,
    rule: "inbox_stale",
    severity: severity as MonitoringAlertResponse["severity"],
    status: status as MonitoringAlertResponse["status"],
    title_fa: "هشدار",
    message_fa: "پیام",
    dedup_key: "inbox_stale",
    runbook_ref: null,
    context: {},
    fired_at: null,
    acknowledged_at: null,
    acknowledged_by: null,
    resolved_at: null,
    resolved_by: null,
  };
}

describe("labels", () => {
  it("maps processes, rules, severities and statuses to Persian", () => {
    expect(processLabelFa("worker")).toBe("کارگر");
    expect(processLabelFa("mcp")).toBe("سرور MCP");
    expect(processLabelFa("mystery")).toBe("mystery");
    expect(processStatusLabelFa("degraded")).toBe("کاهش‌یافته");
    expect(alertRuleLabelFa("token_expiring")).toBe("انقضای توکن");
    expect(alertRuleLabelFa("error_rate_5xx")).toBe("نرخ خطای ۵xx");
    expect(alertSeverityLabelFa("critical")).toBe("بحرانی");
    expect(alertStatusLabelFa("acknowledged")).toBe("تأیید شده");
    expect(backupStatusLabelFa("unknown")).toBe("نامشخص");
    expect(backupDatabaseLabelFa("tryton")).toBe("ترایتون");
  });
});

describe("worstProcessStatus", () => {
  it("returns ok when everything is ok", () => {
    expect(worstProcessStatus([process("api", "ok"), process("worker", "ok")])).toBe("ok");
  });

  it("ranks error above degraded above unknown above ok", () => {
    expect(
      worstProcessStatus([process("api", "unknown"), process("worker", "degraded")]),
    ).toBe("degraded");
    expect(
      worstProcessStatus([process("api", "degraded"), process("worker", "error")]),
    ).toBe("error");
    expect(worstProcessStatus([process("api", "unknown")])).toBe("unknown");
  });

  it("returns unknown for an empty list", () => {
    expect(worstProcessStatus([])).toBe("unknown");
  });
});

describe("alertCountBySeverity", () => {
  it("counts firing alerts only, by severity", () => {
    const counts = alertCountBySeverity([
      alert("critical", "open"),
      alert("critical", "acknowledged"),
      alert("warning", "open"),
      alert("critical", "resolved"), // resolved does not count
      alert("info", "open"),
    ]);
    expect(counts).toEqual({ critical: 2, warning: 1, info: 1 });
  });
});

describe("formatDaysLeft", () => {
  it("renders past, today and future expiry", () => {
    expect(formatDaysLeft(-2)).toBe("منقضی شده (۲ روز پیش)");
    expect(formatDaysLeft(0)).toBe("امروز منقضی می‌شود");
    expect(formatDaysLeft(10)).toBe("۱۰ روز دیگر");
  });
});

describe("formatRial", () => {
  it("renders a Rial amount with Persian digits", () => {
    expect(formatRial(1250000)).toBe("۱٬۲۵۰٬۰۰۰ ریال");
    expect(formatRial(0)).toBe("۰ ریال");
  });
});

describe("formatPercent", () => {
  it("renders a ratio with a Persian decimal separator", () => {
    expect(formatPercent(0.05)).toBe("۵٫۰٪");
    expect(formatPercent(0.075)).toBe("۷٫۵٪");
    expect(formatPercent(0)).toBe("۰٫۰٪");
  });
});

describe("validateChannelForm", () => {
  it("accepts a valid webhook channel", () => {
    expect(
      validateChannelForm({
        ...emptyChannelForm(),
        name: "وب‌هوک عملیات",
        target: "https://hooks.example.com/alerts",
      }),
    ).toEqual({});
  });

  it("refuses http webhooks, bad mobiles and bad emails", () => {
    expect(
      validateChannelForm({
        ...emptyChannelForm(),
        name: "x",
        target: "http://hooks.example.com/x",
      }).target,
    ).toBe("نشانی وب‌هوک باید یک URL با https باشد.");
    expect(
      validateChannelForm({
        ...emptyChannelForm(),
        kind: "sms",
        name: "x",
        target: "123",
        providerKey: "simulator",
      }).target,
    ).toBe("شماره موبایل باید ۱۱ رقم باشد و با ۰۹ شروع شود.");
    expect(
      validateChannelForm({
        ...emptyChannelForm(),
        kind: "email",
        name: "x",
        target: "not-an-email",
      }).target,
    ).toBe("نشانی ایمیل معتبر نیست.");
  });

  it("accepts Persian-digit mobiles", () => {
    expect(
      validateChannelForm({
        ...emptyChannelForm(),
        kind: "sms",
        name: "x",
        target: "۰۹۱۲۳۴۵۶۷۸۹",
        providerKey: "simulator",
      }),
    ).toEqual({});
  });

  it("requires a provider for SMS channels", () => {
    expect(
      validateChannelForm({
        ...emptyChannelForm(),
        kind: "sms",
        name: "x",
        target: "09123456789",
        providerKey: "",
      }).providerKey,
    ).toBe("کانال پیامکی به ارائه‌دهنده نیاز دارد.");
  });

  it("normalizes the mobile to Latin digits in the request", () => {
    expect(
      channelFormToRequest({
        ...emptyChannelForm(),
        kind: "sms",
        name: "x",
        target: "۰۹۱۲-۳۴۵ ۶۷۸۹",
        providerKey: "simulator",
      }).target,
    ).toBe("09123456789");
  });
});

describe("validateTokenForm", () => {
  it("accepts a future expiry", () => {
    expect(
      validateTokenForm({ ...emptyTokenForm(), label: "توکن فینوتک", expiresDate: "2030-01-01" }),
    ).toEqual({});
  });

  it("refuses a past expiry", () => {
    expect(
      validateTokenForm({ ...emptyTokenForm(), label: "x", expiresDate: "2020-01-01" })
        .expiresDate,
    ).toBe("تاریخ انقضا نمی‌تواند در گذشته باشد.");
  });

  it("sends end-of-day UTC for the chosen date", () => {
    expect(
      tokenFormToRequest({ ...emptyTokenForm(), label: "x", expiresDate: "2030-01-01" })
        .expires_at,
    ).toBe("2030-01-01T23:59:59Z");
  });
});
