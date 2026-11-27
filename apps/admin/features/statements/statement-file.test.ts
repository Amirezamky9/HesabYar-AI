import { describe, expect, it } from "vitest";
import {
  MAX_IMPORT_FILE_BYTES,
  MAX_IMPORT_ROWS,
  fileToBase64,
  mappingIsComplete,
} from "./statement-file";

describe("fileToBase64", () => {
  it("encodes ASCII bytes", async () => {
    const file = new File(["hello"], "a.csv", { type: "text/csv" });
    await expect(fileToBase64(file)).resolves.toBe("aGVsbG8=");
  });

  it("encodes Persian UTF-8 bytes", async () => {
    const file = new File(["تاریخ,شرح\n"], "baam.csv", { type: "text/csv" });
    const encoded = await fileToBase64(file);
    expect(atob(encoded)).toBe(
      "\xd8\xaa\xd8\xa7\xd8\xb1\xdb\x8c\xd8\xae,\xd8\xb4\xd8\xb1\xd8\xad\n",
    );
  });

  it("encodes an empty file as an empty string", async () => {
    const file = new File([], "empty.csv", { type: "text/csv" });
    await expect(fileToBase64(file)).resolves.toBe("");
  });

  it("handles files larger than the chunk size", async () => {
    const bytes = new Uint8Array(100_000).fill(0x41);
    const file = new File([bytes], "big.csv", { type: "text/csv" });
    const encoded = await fileToBase64(file);
    expect(encoded.length).toBeGreaterThan(100_000);
    expect(atob(encoded).length).toBe(100_000);
  });
});

describe("import caps", () => {
  it("matches the backend caps", () => {
    expect(MAX_IMPORT_FILE_BYTES).toBe(10 * 1024 * 1024);
    expect(MAX_IMPORT_ROWS).toBe(2000);
  });
});

describe("mappingIsComplete", () => {
  it("accepts a signed-amount mapping", () => {
    expect(
      mappingIsComplete({ date: "تاریخ", description: "شرح", amount: "مبلغ" }),
    ).toBe(true);
  });

  it("accepts a debit+credit pair", () => {
    expect(
      mappingIsComplete({
        date: "تاریخ",
        description: "شرح",
        debit: "برداشت",
        credit: "واریز",
      }),
    ).toBe(true);
  });

  it("rejects a missing date", () => {
    expect(mappingIsComplete({ description: "شرح", amount: "مبلغ" })).toBe(false);
  });

  it("rejects a missing description", () => {
    expect(mappingIsComplete({ date: "تاریخ", amount: "مبلغ" })).toBe(false);
  });

  it("rejects a missing amount and a half pair", () => {
    expect(mappingIsComplete({ date: "تاریخ", description: "شرح" })).toBe(false);
    expect(
      mappingIsComplete({ date: "تاریخ", description: "شرح", debit: "برداشت" }),
    ).toBe(false);
  });
});
