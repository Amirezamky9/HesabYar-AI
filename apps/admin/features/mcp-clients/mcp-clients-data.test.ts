import { describe, expect, it } from "vitest";
import {
  MCP_CLIENT_SCOPES,
  emptyMcpClientForm,
  mcpClientFormToCreate,
  scopeLabelFa,
  validateMcpClientForm,
} from "./mcp-clients-data";

describe("mcp-clients-data", () => {
  it("lists mcp:connect first and only read scopes after it", () => {
    expect(MCP_CLIENT_SCOPES[0]?.scope).toBe("mcp:connect");
    for (const { scope } of MCP_CLIENT_SCOPES.slice(1)) {
      expect(scope).toMatch(/:(read|validate)$/);
    }
  });

  it("labels known scopes in Persian and falls back to the raw code", () => {
    expect(scopeLabelFa("reports:read")).toBe("خواندن گزارش‌ها");
    expect(scopeLabelFa("something:else")).toBe("something:else");
  });

  it("rejects a name shorter than 3 characters", () => {
    const errors = validateMcpClientForm({ ...emptyMcpClientForm(), name: "ab" });
    expect(errors.name).toBeDefined();
  });

  it("rejects an empty scope set", () => {
    const errors = validateMcpClientForm({ ...emptyMcpClientForm(), scopes: [] });
    expect(errors.scopes).toBeDefined();
  });

  it("accepts a valid form", () => {
    const errors = validateMcpClientForm({
      ...emptyMcpClientForm(),
      name: "Claude Desktop",
    });
    expect(errors).toEqual({});
  });

  it("forces mcp:connect, trims the name and omits the company grant for all", () => {
    const payload = mcpClientFormToCreate({
      ...emptyMcpClientForm(),
      name: "  Claude Desktop  ",
      scopes: ["reports:read"],
      companyId: "all",
      expiryDays: 0,
    });
    expect(payload.name).toBe("Claude Desktop");
    expect(payload.scopes).toEqual(["mcp:connect", "reports:read"]);
    expect(payload.company_id).toBeNull();
    expect(payload.expires_at).toBeNull();
  });

  it("converts an expiry in days to an ISO timestamp", () => {
    const now = new Date("2026-11-21T10:00:00.000Z");
    const payload = mcpClientFormToCreate({
      ...emptyMcpClientForm(),
      name: "x-client",
      expiryDays: 30,
    }, now);
    expect(payload.expires_at).toBe("2026-12-21T10:00:00.000Z");
  });

  it("passes the company grant through when one company is chosen", () => {
    const payload = mcpClientFormToCreate({
      ...emptyMcpClientForm(),
      name: "x-client",
      companyId: "company-row-id",
    });
    expect(payload.company_id).toBe("company-row-id");
  });
});
