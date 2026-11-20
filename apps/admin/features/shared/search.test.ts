import { describe, expect, it } from "vitest";
import { matchesSearch, normalizeSearchText } from "./search";

describe("normalizeSearchText", () => {
  it("converts Persian and Arabic-Indic digits to ASCII", () => {
    expect(normalizeSearchText("۱۲۳")).toBe("123");
    expect(normalizeSearchText("١٢٣")).toBe("123");
  });

  it("unifies Arabic Yeh/Kaf to Persian ی/ک", () => {
    expect(normalizeSearchText("على كريمى")).toBe("علی کریمی");
  });

  it("collapses whitespace and ZWNJ to single spaces", () => {
    expect(normalizeSearchText("  شرکت   ملی‌ نفت ")).toBe("شرکت ملی نفت");
  });
});

describe("matchesSearch", () => {
  it("matches a substring regardless of digit script", () => {
    expect(matchesSearch("مشتری ۱۲۳", "123")).toBe(true);
    expect(matchesSearch("مشتری 123", "۱۲۳")).toBe(true);
  });

  it("matches regardless of ی/ک spelling", () => {
    expect(matchesSearch("علی کریمی", "على كريمى")).toBe(true);
  });

  it("matches everything on an empty query", () => {
    expect(matchesSearch("هر چیزی", "")).toBe(true);
    expect(matchesSearch("هر چیزی", "   ")).toBe(true);
  });

  it("does not match unrelated text", () => {
    expect(matchesSearch("احمد رضایی", "محمد")).toBe(false);
  });
});
