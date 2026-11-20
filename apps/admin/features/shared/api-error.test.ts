import { describe, expect, it } from "vitest";
import { ApiError } from "@/lib/api-client";
import { isNotFoundError, isPermissionDenied, persianApiErrorMessage } from "./api-error";

describe("persianApiErrorMessage", () => {
  it("maps 401 to a session-expired message", () => {
    expect(persianApiErrorMessage(new ApiError(401, "UNAUTHORIZED", "no session"), "پشتیبان")).toContain(
      "نشست شما به پایان رسیده است",
    );
  });

  it("maps 403 to a permission message", () => {
    expect(persianApiErrorMessage(new ApiError(403, "FORBIDDEN", "missing scope"), "پشتیبان")).toContain(
      "دسترسی ندارد",
    );
  });

  it("maps 404 to a not-found message", () => {
    expect(persianApiErrorMessage(new ApiError(404, "LEDGER_INVOICE_NOT_FOUND", "gone"), "پشتیبان")).toContain(
      "پیدا نشد",
    );
  });

  it("maps COMPANY_REQUIRED to a company-selection message", () => {
    expect(persianApiErrorMessage(new ApiError(400, "COMPANY_REQUIRED", "company?"), "پشتیبان")).toContain(
      "شرکتی انتخاب نشده است",
    );
  });

  it("maps NOT_IMPLEMENTED to a coming-soon message", () => {
    expect(persianApiErrorMessage(new ApiError(501, "NOT_IMPLEMENTED", "stub"), "پشتیبان")).toContain(
      "هنوز در دسترس نیست",
    );
  });

  it("falls back to the screen fallback for unknown codes", () => {
    expect(persianApiErrorMessage(new ApiError(500, "SOMETHING_ELSE", "boom"), "دریافت فاکتورها ناموفق بود.")).toBe(
      "دریافت فاکتورها ناموفق بود.",
    );
  });

  it("maps a network failure to a connectivity message", () => {
    expect(persianApiErrorMessage(new TypeError("fetch failed"), "پشتیبان")).toContain("ارتباط با سرور برقرار نشد");
  });

  it("never leaks raw JSON or English detail into the message", () => {
    const msg = persianApiErrorMessage(new ApiError(403, "FORBIDDEN", "Missing scope invoice:read."), "پشتیبان");
    expect(msg).not.toContain("invoice:read");
    expect(msg).not.toContain("{");
  });
});

describe("isPermissionDenied / isNotFoundError", () => {
  it("detects 403 and 404 distinctly", () => {
    expect(isPermissionDenied(new ApiError(403, "FORBIDDEN", ""))).toBe(true);
    expect(isPermissionDenied(new ApiError(404, "X", ""))).toBe(false);
    expect(isNotFoundError(new ApiError(404, "X", ""))).toBe(true);
    expect(isNotFoundError(new ApiError(403, "FORBIDDEN", ""))).toBe(false);
    expect(isPermissionDenied(new Error("boom"))).toBe(false);
  });
});
