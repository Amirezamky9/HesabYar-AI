import { describe, expect, it } from "vitest";
import { ApiError } from "@/lib/api-client";
import {
  apiErrorMessage,
  auditActionLabel,
  validationIssueMessage,
} from "@/features/journal-entries/lib/error-messages";

describe("apiErrorMessage", () => {
  it("maps 403 to a readable Persian permission message", () => {
    const err = new ApiError(403, "FORBIDDEN", "The API key lacks the required scope(s): ledger:post.");
    expect(apiErrorMessage(err)).toBe("کلید شما دسترسی انجام این کار را ندارد.");
  });

  it("maps a 403 with an unknown code to the generic permission message", () => {
    const err = new ApiError(403, "SOMETHING_ELSE", "Forbidden");
    expect(apiErrorMessage(err)).toContain("دسترسی ندارید");
    expect(apiErrorMessage(err)).not.toContain("Forbidden");
  });

  it("maps 422 to a Persian input-validation message, never raw JSON", () => {
    const err = new ApiError(422, "HTTP_422", "Request failed (422)");
    const message = apiErrorMessage(err);
    expect(message).toBe("داده‌های واردشده معتبر نیست؛ ورودی‌ها را بررسی کنید.");
    expect(message).not.toContain("{");
  });

  it("maps 401 to the session-expired message", () => {
    const err = new ApiError(401, "UNAUTHORIZED", "Not signed in.");
    expect(apiErrorMessage(err)).toBe("نشست به پایان رسیده است؛ دوباره وارد شوید.");
  });

  it("maps known ledger codes to Persian", () => {
    expect(
      apiErrorMessage(new ApiError(400, "LEDGER_UNBALANCED", "Unbalanced")),
    ).toContain("نامتوازن");
    expect(
      apiErrorMessage(new ApiError(409, "LEDGER_POSTED_IMMUTABLE", "Posted")),
    ).toBe("سند ثبت‌شده قابل ویرایش نیست.");
    expect(
      apiErrorMessage(new ApiError(404, "LEDGER_JOURNAL_NOT_FOUND", "Not found")),
    ).toContain("پیدا نشد");
    expect(
      apiErrorMessage(new ApiError(400, "LEDGER_PERIOD_CLOSED", "Closed")),
    ).toContain("دورهٔ مالی بسته");
  });

  it("maps the BFF backend-unreachable problem to Persian", () => {
    expect(
      apiErrorMessage(new ApiError(502, "BACKEND_UNREACHABLE", "The HesabYar backend did not answer.")),
    ).toBe("ارتباط با سرور حسابیار برقرار نشد؛ اتصال را بررسی کنید.");
  });

  it("falls back to a generic Persian message that still names the code", () => {
    const message = apiErrorMessage(new ApiError(418, "TEAPOT", "I'm a teapot"));
    expect(message).toContain("TEAPOT");
    expect(message).not.toContain("teapot");
  });

  it("maps a network TypeError to the connection message", () => {
    expect(apiErrorMessage(new TypeError("fetch failed"))).toContain("ارتباط با سرور");
  });

  it("maps unknown throwables to a generic Persian message", () => {
    expect(apiErrorMessage("boom")).toBe("خطای غیرمنتظره‌ای رخ داد؛ دوباره تلاش کنید.");
    expect(apiErrorMessage(undefined)).toBe("خطای غیرمنتظره‌ای رخ داد؛ دوباره تلاش کنید.");
  });
});

describe("validationIssueMessage", () => {
  it("maps known issue codes", () => {
    expect(
      validationIssueMessage({ code: "LEDGER_UNBALANCED", message: "Unbalanced", field: null }),
    ).toBe("مجموع بدهکار با مجموع بستانکار برابر نیست.");
  });

  it("falls back to a generic Persian message for unknown codes", () => {
    expect(
      validationIssueMessage({ code: "SOME_NEW_CODE", message: "Something", field: null }),
    ).toBe("سند یک ایراد اعتبارسنجی دارد.");
  });
});

describe("auditActionLabel", () => {
  it("labels the journal audit actions in Persian", () => {
    expect(auditActionLabel("ledger.draft.create")).toBe("ایجاد پیش‌نویس");
    expect(auditActionLabel("ledger.post")).toBe("ثبت سند");
    expect(auditActionLabel("ledger.reverse")).toBe("برگشت سند");
  });

  it("passes unknown actions through unchanged", () => {
    expect(auditActionLabel("something.else")).toBe("something.else");
  });
});
