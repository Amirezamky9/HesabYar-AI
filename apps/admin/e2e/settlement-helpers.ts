import { expect, type Page } from "@playwright/test";
import { resolveTestJournalId } from "./journal-helpers";

/**
 * Settlement e2e helpers (US-525) — seed the SnappPay 4-tranche world
 * through the BFF + Tryton REST, then drive the settlement center UI.
 *
 * The scenario mirrors the US-522 contract test (100,000,000 IRR gross,
 * 10% fee, 10% fee VAT, 4 × 25% tranches on the 5th of the next four
 * Jalali months) — but everything goes through the real HTTP surface,
 * and the posting path (policy gate + wiring mappings + tie-out) is
 * seeded exactly as a production operator would configure it. The
 * sale itself is seeded too: a posted Tryton invoice (tranche payment
 * term, gateway as alternative payee) + the sale fee (policy A) —
 * the receivable lines the tie-out's clearing pre-check reconciles
 * against.
 *
 * Money is integer IRR everywhere; the UI types Toman and the backend
 * converts (×10).
 */

export const TRYTON_BASE_URL = process.env.TRYTON_BASE_URL ?? "http://127.0.0.1:8000";
export const TRYTON_DATABASE = process.env.TRYTON_DATABASE ?? "hesabyar_tryton_test";
const TRYTON_API_KEY = process.env.TRYTON_API_KEY ?? "hesabyar-dev-rest-key";

export const SNAPPAY_GROSS_IRR = 100_000_000;
export const SNAPPAY_NET_IRR = 89_000_000;
export const SNAPPAY_FEE_IRR = 10_000_000;
export const SNAPPAY_FEE_VAT_IRR = 1_000_000;
export const SNAPPAY_TRANCHE_NET_IRR = 22_250_000;
export const SNAPPAY_TRANCHE_GROSS_IRR = 25_000_000;
export const SNAPPAY_TRANCHE_FEE_IRR = 2_500_000;
export const SNAPPAY_TRANCHE_VAT_IRR = 250_000;

/** The dev company ("HesabYar Adapter Test") in the shared Tryton DB. */
export const DEV_COMPANY_ID = 1;

export const GATEWAY_PARTY_NAME = "HesabYar US-525 SnappPay Gateway";
export const CUSTOMER_PARTY_NAME = "HesabYar US-304 Test Customer";
/** The e2e's own clearing journal — NOT the US-502 fixture journal:
 * that one clears to 1.1.1 Cash, while the wiring (and the bank
 * receipt's leg gather) expect 1.1.6 Gateway Clearing. */
export const SNAPPAY_CLEARING_JOURNAL_NAME = "HesabYar US-525 SnappPay Clearing";

export const JALALI_MONTHS = [
  "فروردین",
  "اردیبهشت",
  "خرداد",
  "تیر",
  "مرداد",
  "شهریور",
  "مهر",
  "آبان",
  "آذر",
  "دی",
  "بهمن",
  "اسفند",
];

const FA_DIGITS = ["۰", "۱", "۲", "۳", "۴", "۵", "۶", "۷", "۸", "۹"];

export function faDigits(value: number | string): string {
  return String(value).replace(/[0-9]/g, (d) => FA_DIGITS[Number(d)]);
}

/** Gregorian → Jalali (verified: 2026-11-19 → 1405/08/28). */
export function gregorianToJalali(date: Date): { jy: number; jm: number; jd: number } {
  const gDays = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  const jDays = [31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29];
  const gy = date.getFullYear();
  const gm = date.getMonth() + 1;
  const gd = date.getDate();
  const y = gy - 1600;
  const m = gm - 1;
  const d = gd - 1;
  let gDayNo =
    365 * y + Math.floor((y + 3) / 4) - Math.floor((y + 99) / 100) + Math.floor((y + 399) / 400);
  for (let i = 0; i < m; i++) gDayNo += gDays[i];
  if (m > 1 && ((y % 4 === 0 && y % 100 !== 0) || y % 400 === 0)) gDayNo++;
  gDayNo += d;
  let jDayNo = gDayNo - 79;
  const jNp = Math.floor(jDayNo / 12053);
  jDayNo %= 12053;
  let jy = 979 + 33 * jNp + 4 * Math.floor(jDayNo / 1461);
  jDayNo %= 1461;
  if (jDayNo >= 366) {
    jy += Math.floor((jDayNo - 1) / 365);
    jDayNo = (jDayNo - 1) % 365;
  }
  let jm = 0;
  for (let i = 0; i < 11 && jDayNo >= jDays[i]; i++) {
    jDayNo -= jDays[i];
    jm++;
  }
  return { jy, jm: jm + 1, jd: jDayNo + 1 };
}

/** Jalali → Gregorian (inverse of gregorianToJalali; verified against
 * jdatetime, incl. leap-year Esfand: 1403/12/30 → 2025-03-20). */
export function jalaliToGregorian(jy: number, jm: number, jd: number): { gy: number; gm: number; gd: number } {
  const gDays = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  const jDays = [31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29];
  const y = jy - 979;
  let jDayNo = 365 * y + Math.floor(y / 33) * 8 + Math.floor(((y % 33) + 3) / 4);
  for (let i = 0; i < jm - 1; i++) jDayNo += jDays[i];
  jDayNo += jd - 1;
  let gDayNo = jDayNo + 79;
  let gy = 1600 + 400 * Math.floor(gDayNo / 146097);
  gDayNo %= 146097;
  let leap = true;
  if (gDayNo >= 36525) {
    gDayNo--;
    gy += 100 * Math.floor(gDayNo / 36524);
    gDayNo %= 36524;
    if (gDayNo >= 365) gDayNo++;
    else leap = false;
  }
  gy += 4 * Math.floor(gDayNo / 1461);
  gDayNo %= 1461;
  if (gDayNo >= 366) {
    leap = false;
    gDayNo--;
    gy += Math.floor(gDayNo / 365);
    gDayNo %= 365;
  }
  let gm = 0;
  for (let i = 0; i < 12; i++) {
    const days = gDays[i] + (i === 1 && leap ? 1 : 0);
    if (gDayNo < days) break;
    gDayNo -= days;
    gm++;
  }
  return { gy, gm: gm + 1, gd: gDayNo + 1 };
}

/** Today's Jalali sales period, e.g. "1405/08". */
export function jalaliPeriodOfToday(): string {
  const { jy, jm } = gregorianToJalali(new Date());
  return `${jy}/${String(jm).padStart(2, "0")}`;
}

function isoDaysAgo(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString().slice(0, 10);
}

// -- BFF -------------------------------------------------------------------

async function bff<T>(page: Page, method: "GET" | "POST" | "PATCH" | "PUT", path: string, data?: unknown): Promise<T> {
  const res = await page.request.fetch(`/api/bff/${path}`, {
    method,
    data: data === undefined ? undefined : data,
  });
  expect(res.ok(), `${method} ${path} → ${res.status()}: ${await res.text()}`).toBe(true);
  return (await res.json()) as T;
}

async function bffStatus(page: Page, method: "GET" | "POST" | "PATCH" | "PUT", path: string, data?: unknown): Promise<number> {
  const res = await page.request.fetch(`/api/bff/${path}`, {
    method,
    data: data === undefined ? undefined : data,
  });
  return res.status();
}

// -- Tryton REST -----------------------------------------------------------

function trytonDomain(payload: unknown): string {
  return Buffer.from(JSON.stringify(payload))
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_");
}

// Node's fetch sends `Accept-Language: *` by default, and Tryton's
// REST protocol copies that header into the transaction context as
// `language` (trytond/protocols/rest.py) — writing a translatable
// field (e.g. a payment term's `name`) then 400s because "*" is not
// an installed language. Every Tryton call pins the language to the
// engine's default instead.
const TRYTON_HEADERS: Record<string, string> = {
  Authorization: `Bearer ${TRYTON_API_KEY}`,
  "Accept-Language": "en",
};

async function trytonSearch<T extends { id: number }>(
  model: string,
  domain: unknown[],
  fields: string[],
): Promise<T[]> {
  const res = await fetch(
    `${TRYTON_BASE_URL}/${TRYTON_DATABASE}/rest/model/${model}?d=${trytonDomain(domain)}&${fields.map((f) => `f=${f}`).join("&")}`,
    { headers: TRYTON_HEADERS },
  );
  if (!res.ok) throw new Error(`Tryton search ${model} failed: ${res.status}`);
  return (await res.json()) as T[];
}

async function trytonCreate(model: string, values: Record<string, unknown>, context?: Record<string, unknown>): Promise<number> {
  // Fields travel as repeated ?f= params; the transaction context
  // travels in the X-Tryton-Context header (base64url JSON, padded).
  const headers: Record<string, string> = {
    ...TRYTON_HEADERS,
    "Content-Type": "application/json",
  };
  if (context) headers["X-Tryton-Context"] = trytonDomain(context);
  const res = await fetch(`${TRYTON_BASE_URL}/${TRYTON_DATABASE}/rest/model/${model}?f=id`, {
    method: "POST",
    headers,
    body: JSON.stringify(values),
  });
  if (!res.ok) throw new Error(`Tryton create ${model} failed: ${res.status} ${await res.text()}`);
  const created = (await res.json()) as { id: number };
  return created.id;
}

// -- Tryton REST wire format -------------------------------------------------
// The engine's REST protocol encodes Numeric/Date values as
// `{"__class__": "Decimal", "decimal": "..."}` /
// `{"__class__": "date", "year":..., "month":..., "day":...}` —
// the same shapes the Python client's `contracts.encode_wire`
// produces. Plain JSON numbers/strings are NOT accepted for those
// field types.

function trytonDecimal(value: string): { __class__: "Decimal"; decimal: string } {
  return { __class__: "Decimal", decimal: value };
}

function trytonDate(iso: string): { __class__: "date"; year: number; month: number; day: number } {
  const [year, month, day] = iso.split("-").map(Number);
  return { __class__: "date", year, month, day };
}

function trytonDecimalToString(value: unknown): string {
  if (
    typeof value === "object" &&
    value !== null &&
    (value as { __class__?: unknown }).__class__ === "Decimal"
  ) {
    return (value as { decimal: string }).decimal;
  }
  return String(value);
}

function trytonDateToIso(value: unknown): string {
  if (
    typeof value === "object" &&
    value !== null &&
    (value as { __class__?: unknown }).__class__ === "date"
  ) {
    const d = value as { year: number; month: number; day: number };
    return `${d.year}-${String(d.month).padStart(2, "0")}-${String(d.day).padStart(2, "0")}`;
  }
  return String(value);
}

/**
 * Call a model button action over REST:
 * `POST /<db>/rest/model/<model>/<id>/<action>?f=...` with an empty
 * JSON body. The route returns the updated record (the adapter's
 * `_invoice_action` does the same; no company context needed).
 */
async function trytonAction<T>(
  model: string,
  recordId: number,
  action: string,
  fields: string[],
): Promise<T> {
  const res = await fetch(
    `${TRYTON_BASE_URL}/${TRYTON_DATABASE}/rest/model/${model}/${recordId}/${action}?${fields.map((f) => `f=${f}`).join("&")}`,
    {
      method: "POST",
      headers: {
        ...TRYTON_HEADERS,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({}),
    },
  );
  if (!res.ok) {
    throw new Error(
      `Tryton action ${model}.${action}(${recordId}) failed: ${res.status} ${await res.text()}`,
    );
  }
  return (await res.json()) as T;
}

/**
 * The gateway party for THIS run (name carries the run tag). A fresh
 * party per run is REQUIRED, not cosmetic: the clearing pre-check
 * gathers receivable lines by (account, party, maturity_date) with
 * no payment scoping, so two runs' sales sharing one party and the
 * same dues (every run confirms "today") pollute each other's
 * gather — the second run finds 6 lines netting 44.5M instead of
 * its own 3 netting 22.25M, and the tie-out fails. (Production has
 * the same shape for two same-day sales to one gateway — the
 * gather needs payment scoping in src/; the e2e isolates by party.)
 */
async function ensureGatewayParty(runTag: string): Promise<number> {
  const name = `${GATEWAY_PARTY_NAME} ${runTag}`;
  const found = await trytonSearch<{ id: number }>(
    "party.party",
    [["name", "=", name]],
    ["id"],
  );
  if (found.length > 0) return found[0].id;
  return trytonCreate("party.party", { name });
}

/**
 * The clearing payment journal for the snapppay wiring: manual
 * process, IRR, clearing account 1.1.6 Gateway Clearing. The journal
 * is dedicated to this e2e (own name) and its clearing account is
 * VERIFIED on reuse — the clearing move posts Dr to the journal's
 * clearing account, while the bank receipt gathers the tranche's
 * clearing legs on the wiring's clearing account (1.1.6); a mismatch
 * fails the tie-out with "0 existing legs". (The US-502 fixture
 * journal this reused before clears to 1.1.1 Cash — its fixture
 * sets clearing_account to the cash account on purpose.)
 */
async function ensureClearingJournal(): Promise<number> {
  const clearing = await trytonSearch<{ id: number }>(
    "account.account",
    [["code", "=", "1.1.6"]],
    ["id"],
  );
  if (clearing.length === 0) throw new Error("Clearing account 1.1.6 not found in Tryton");
  const clearingAccountId = clearing[0].id;
  const found = await trytonSearch<{ id: number; clearing_account: number | null }>(
    "account.payment.journal",
    [["name", "=", SNAPPAY_CLEARING_JOURNAL_NAME]],
    ["id", "clearing_account"],
  );
  if (found.length > 0) {
    if (found[0].clearing_account !== clearingAccountId) {
      throw new Error(
        `Clearing journal ${SNAPPAY_CLEARING_JOURNAL_NAME} (id ${found[0].id}) has ` +
          `clearing_account ${found[0].clearing_account}, expected 1.1.6 ` +
          `(id ${clearingAccountId}) — fix it by hand.`,
      );
    }
    return found[0].id;
  }
  const currencies = await trytonSearch<{ id: number }>("currency.currency", [["code", "=", "IRR"]], ["id"]);
  if (currencies.length === 0) throw new Error("IRR currency not found in Tryton");
  const generalJournal = await resolveTestJournalId();
  return trytonCreate(
    "account.payment.journal",
    {
      name: SNAPPAY_CLEARING_JOURNAL_NAME,
      currency: currencies[0].id,
      company: DEV_COMPANY_ID,
      process_method: "manual",
      clearing_account: clearingAccountId,
      clearing_journal: generalJournal,
    },
    { company: DEV_COMPANY_ID },
  );
}

// -- Wiring seed -----------------------------------------------------------

interface MappingSeed {
  mapping_kind: string;
  channel_key: string;
  source_key: string;
  target_type: string;
  target_ref: string;
}

async function ensureMapping(page: Page, seed: MappingSeed): Promise<void> {
  const resolvePath =
    `integrations/mappings/resolve?mapping_kind=${encodeURIComponent(seed.mapping_kind)}` +
    `&channel_key=${encodeURIComponent(seed.channel_key)}` +
    `&source_key=${encodeURIComponent(seed.source_key)}`;
  const status = await bffStatus(page, "GET", resolvePath);
  if (status === 200) {
    const current = await bff<{ target_type: string; target_ref: string }>(page, "GET", resolvePath);
    if (current.target_type === seed.target_type && current.target_ref === seed.target_ref) return;
    // A stale mapping points elsewhere: create a new version on top.
  } else if (status !== 404) {
    throw new Error(`Mapping resolve ${seed.mapping_kind}/${seed.source_key} → ${status}`);
  }
  await bff(page, "POST", "integrations/mappings", {
    mapping_kind: seed.mapping_kind,
    channel_key: seed.channel_key,
    source_key: seed.source_key,
    target_type: seed.target_type,
    target_ref: seed.target_ref,
    suggestion_mode: "auto",
    source: "manual",
    notes: "US-525 e2e seed",
  });
}

interface GatewayContractSeed {
  version: number;
  valid_from: string;
  fee_percent: string;
  fee_vat_rate: string | null;
  settlement_model: string;
  tranche_legs: { month_offset: number; payout_day: number; percent: string }[];
  payout_window_start_day: number | null;
  payout_window_end_day: number | null;
  grace_days_after_due: number;
  auto_post_policy: string;
  auto_post_max_amount_irr: number | null;
}

const SNAPPAY_LEGS = [
  { month_offset: 1, payout_day: 5, percent: "25" },
  { month_offset: 2, payout_day: 5, percent: "25" },
  { month_offset: 3, payout_day: 5, percent: "25" },
  { month_offset: 4, payout_day: 5, percent: "25" },
];

function snappPayContractMatches(contract: GatewayContractSeed): boolean {
  // Decimal strings round-trip as "10" or "10.0" — compare numerically.
  const num = (v: string | null) => (v === null ? null : Number(v));
  return (
    num(contract.fee_percent) === 10 &&
    num(contract.fee_vat_rate) === 10 &&
    contract.settlement_model === "tranches_4" &&
    contract.tranche_legs.length === 4 &&
    contract.tranche_legs.every(
      (leg, i) =>
        leg.month_offset === SNAPPAY_LEGS[i].month_offset &&
        leg.payout_day === SNAPPAY_LEGS[i].payout_day &&
        num(leg.percent) === Number(SNAPPAY_LEGS[i].percent),
    ) &&
    contract.payout_window_start_day === 1 &&
    contract.payout_window_end_day === 5 &&
    contract.grace_days_after_due === 3 &&
    contract.auto_post_policy === "auto_post_within_policy" &&
    contract.auto_post_max_amount_irr === 1_000_000_000
  );
}

/**
 * The active SnappPay contract with the test params. Versions move
 * forward in time, so a re-run reuses the matching active version
 * instead of creating a duplicate (same valid_from is rejected).
 */
async function ensureSnappPayContract(page: Page): Promise<{ version: number }> {
  const status = await bffStatus(page, "GET", "integrations/channels/snapppay/contracts/active");
  if (status === 200) {
    const active = await bff<GatewayContractSeed>(page, "GET", "integrations/channels/snapppay/contracts/active");
    if (snappPayContractMatches(active)) return { version: active.version };
    if (active.valid_from >= isoDaysAgo(0)) {
      throw new Error(
        `Active snapppay contract v${active.version} (valid_from ${active.valid_from}) does not match the e2e params and a new version cannot take effect today — fix it by hand.`,
      );
    }
  } else if (status !== 404) {
    throw new Error(`Active snapppay contract lookup → ${status}`);
  }
  const created = await bff<{ version: number }>(page, "POST", "integrations/channels/snapppay/contracts", {
    valid_from: status === 200 ? isoDaysAgo(0) : isoDaysAgo(30),
    fee_percent: "10",
    fee_vat_rate: "10",
    fee_vat_claimable: false,
    fee_timing: "proportional",
    fee_accrual_policy: "at_sale",
    settlement_model: "tranches_4",
    tranche_legs: SNAPPAY_LEGS,
    payout_window_start_day: 1,
    payout_window_end_day: 5,
    payout_calendar: "jalali",
    working_day_adjustment: false,
    grace_days_after_due: 3,
    match_tolerance_irr: 0,
    tieout_tolerance_irr: 0,
    auto_post_policy: "auto_post_within_policy",
    auto_post_max_amount_irr: 1_000_000_000,
    source_note: "US-525 e2e seed",
  });
  return { version: created.version };
}

/**
 * Seed the full SnappPay posting path: channels, enabled snapppay,
 * a fresh 4-tranche contract version (fee 10%, VAT 10%, auto-post
 * within a 1,000,000,000 IRR cap), installation auto-post opt-in,
 * the payment.settlement.received policy row, and the 9 wiring
 * mappings (run gateway party + payment journal + general journal +
 * 6 accounts). `runTag` (the transaction id) scopes the gateway
 * party to this run — see ensureGatewayParty.
 */
export async function seedSettlementWiring(
  page: Page,
  runTag: string,
): Promise<{ contractVersion: number }> {
  await bff(page, "POST", "integrations/channels/seed");
  await bff(page, "PATCH", "integrations/channels/snapppay", { enabled: true });

  const contract = await ensureSnappPayContract(page);

  const settings = await bff<{ weekly_holiday: string; matching_window_days: number }>(
    page,
    "GET",
    "integrations/settings",
  );
  await bff(page, "PUT", "integrations/settings", {
    weekly_holiday: settings.weekly_holiday,
    matching_window_days: settings.matching_window_days,
    auto_post_enabled: true,
  });
  await bff(page, "PUT", "integrations/policy-gate/payment.settlement.received", {
    allowed: true,
    max_amount_irr: 1_000_000_000,
    require_tieout: true,
  });

  const [gatewayPartyId, clearingJournalId, generalJournalId] = await Promise.all([
    ensureGatewayParty(runTag),
    ensureClearingJournal(),
    resolveTestJournalId(),
  ]);
  await ensureMapping(page, {
    mapping_kind: "external_party",
    channel_key: "snapppay",
    source_key: "snapppay",
    target_type: "party",
    target_ref: String(gatewayPartyId),
  });
  await ensureMapping(page, {
    mapping_kind: "gateway_journal",
    channel_key: "snapppay",
    source_key: "snapppay",
    target_type: "journal",
    target_ref: String(clearingJournalId),
  });
  // The general journal is optional in the mapping center, but the
  // engine only auto-resolves it when the company has exactly ONE
  // general journal — the dev company has three, so the batch move
  // fails with LEDGER_INVALID_JOURNAL without this mapping.
  await ensureMapping(page, {
    mapping_kind: "gateway_journal",
    channel_key: "snapppay",
    source_key: "general_journal",
    target_type: "journal",
    target_ref: String(generalJournalId),
  });
  const accounts: [string, string][] = [
    ["receivable", "1.2.8"],
    ["clearing", "1.1.6"],
    ["bank", "1.1.1"],
    ["fee_expense", "5.2.4"],
    ["fee_vat", "6.3.6"],
    ["revenue", "4.1.1"],
  ];
  for (const [sourceKey, code] of accounts) {
    await ensureMapping(page, {
      mapping_kind: "gateway_journal",
      channel_key: "snapppay",
      source_key: sourceKey,
      target_type: "account",
      target_ref: code,
    });
  }
  return { contractVersion: contract.version };
}

// -- Sale seed (Tryton invoice + fee) --------------------------------------
// The tie-out's clearing pre-check reconciles each tranche against
// the Tryton receivable lines for (receivable account, gateway
// party, maturity_date = tranche expected date): the posted sale
// invoice's lines (gross, per tranche) + the sale fee's legs (fee +
// VAT credits, per tranche). Without the sale, the gather finds zero
// lines and the batch fails with `posting_failed` — so the e2e seeds
// the sale exactly as the US-522 contract test does.

/**
 * The SnappPay tranche due dates for a sale: the 5th of the next four
 * Jalali months after the sale month — the exact rule the backend
 * derivation applies (derivation.py `_due_date_for_leg`: contract
 * payout_calendar=jalali, working_day_adjustment=false), mirrored
 * here so the payment term's maturities land on the tranche dues.
 * `saleDateIso` is the UTC date of `gateway_confirmed_at` (the
 * backend anchors on `confirmed_at.date()`).
 */
export function snappPayTrancheDueDates(saleDateIso: string): string[] {
  // Noon UTC: `gregorianToJalali` reads local getters, and noon UTC
  // is the same calendar day in every timezone within ±12h.
  const { jy, jm } = gregorianToJalali(new Date(`${saleDateIso}T12:00:00Z`));
  return SNAPPAY_LEGS.map((leg) => {
    const total = jy * 12 + (jm - 1) + leg.month_offset;
    const dueYear = Math.floor(total / 12);
    const dueMonth = (total % 12) + 1;
    const { gy, gm, gd } = jalaliToGregorian(dueYear, dueMonth, leg.payout_day);
    return `${gy}-${String(gm).padStart(2, "0")}-${String(gd).padStart(2, "0")}`;
  });
}

function daysBetween(fromIso: string, toIso: string): number {
  const ms = Date.parse(`${toIso}T00:00:00Z`) - Date.parse(`${fromIso}T00:00:00Z`);
  return Math.round(ms / 86_400_000);
}

/** The US304 seed customer + its first invoice address (created if missing). */
async function ensureSaleCustomer(): Promise<{ partyId: number; addressId: number }> {
  let partyId: number | null = null;
  const envId = process.env.US304_PARTY_ID;
  if (envId) {
    const found = await trytonSearch<{ id: number }>(
      "party.party",
      [["id", "=", Number(envId)]],
      ["id"],
    );
    if (found.length > 0) partyId = found[0].id;
  }
  if (partyId === null) {
    const found = await trytonSearch<{ id: number }>(
      "party.party",
      [["name", "=", CUSTOMER_PARTY_NAME]],
      ["id"],
    );
    if (found.length === 0) {
      throw new Error(`Sale customer ${CUSTOMER_PARTY_NAME} not found in Tryton (US304 seed missing?)`);
    }
    partyId = found[0].id;
  }
  // NB: `party.address` has no REST-readable `name` field (a read for
  // it 500s) — ask for `id` only, like the adapter's resolver.
  const addresses = await trytonSearch<{ id: number }>(
    "party.address",
    [["party", "=", partyId]],
    ["id"],
  );
  if (addresses.length > 0) return { partyId, addressId: addresses[0].id };
  // An invoice needs an invoice address — create one like the US304 seed.
  const addressId = await trytonCreate("party.address", {
    party: partyId,
    street: "US-525 e2e invoice address",
    city: "Tehran",
  });
  return { partyId, addressId };
}

async function resolveAccountByCode(code: string): Promise<number> {
  const found = await trytonSearch<{ id: number }>(
    "account.account",
    [
      ["code", "=", code],
      ["company", "=", DEV_COMPANY_ID],
    ],
    ["id"],
  );
  if (found.length === 0) throw new Error(`Account ${code} not found for company ${DEV_COMPANY_ID}`);
  return found[0].id;
}

async function resolveRevenueJournal(): Promise<number> {
  // NB: account.journal has NO company field (a `company` domain
  // clause 500s) — journals are shared; the type alone selects it.
  const found = await trytonSearch<{ id: number }>(
    "account.journal",
    [["type", "=", "revenue"]],
    ["id"],
  );
  if (found.length !== 1) {
    throw new Error(`Expected exactly 1 revenue journal, got ${found.length}`);
  }
  return found[0].id;
}

async function resolveCurrency(code: string): Promise<number> {
  const found = await trytonSearch<{ id: number }>(
    "currency.currency",
    [["code", "=", code]],
    ["id"],
  );
  if (found.length === 0) throw new Error(`Currency ${code} not found in Tryton`);
  return found[0].id;
}

/**
 * A 4-leg payment term mirroring the US-522 contract test's
 * `_create_payment_term`: 3 × 25% on total + remainder, each leg's
 * maturity the corresponding tranche due (day-offset from the sale
 * date — the clearing pre-check gathers receivable lines by
 * maturity_date = tranche expected date).
 */
async function createSnappPayPaymentTerm(
  saleDateIso: string,
  dues: string[],
  runTag: string,
): Promise<number> {
  const lines = dues.map((due, index) => {
    const leg: Record<string, unknown> = {
      type: index < 3 ? "percent_on_total" : "remainder",
      relativedeltas: [
        ["create", [{ months: 0, weeks: 0, days: daysBetween(saleDateIso, due) }]],
      ],
    };
    if (index < 3) {
      leg.ratio = trytonDecimal("0.25");
      leg.divisor = trytonDecimal("4");
    }
    return leg;
  });
  return trytonCreate(
    "account.invoice.payment_term",
    {
      name: `HesabYar US-525 SnappPay 4 Tranches ${runTag}`,
      lines: [["create", lines]],
    },
    { company: DEV_COMPANY_ID },
  );
}

export interface SeededSale {
  invoiceId: number;
  saleDateIso: string;
  dues: string[];
}

/**
 * Create + validate + post the 100M sale invoice in Tryton, mirroring
 * the US-522 contract test's `_post_sale_invoice`: party=customer,
 * `alternative_payees=[gateway]` (moves the posted receivable lines'
 * party from the customer to the gateway), payment term = the 4
 * tranche dues (each receivable line's maturity_date = its tranche's
 * expected date). The HesabYar invoice REST API cannot do this (its
 * draft request has no payment-term / alternative-payee fields), so
 * this goes to Tryton REST directly. Verifies the posted receivable
 * lines match the dues before returning — a seed mistake fails here
 * with a clear message instead of as a `posting_failed` review item.
 */
async function postSaleInvoice(
  saleDateIso: string,
  dues: string[],
  runTag: string,
): Promise<SeededSale> {
  const [customer, gatewayPartyId, receivableId, revenueId, currencyId, journalId] =
    await Promise.all([
      ensureSaleCustomer(),
      ensureGatewayParty(runTag),
      resolveAccountByCode("1.2.8"),
      resolveAccountByCode("4.1.1"),
      resolveCurrency("IRR"),
      resolveRevenueJournal(),
    ]);
  const termId = await createSnappPayPaymentTerm(saleDateIso, dues, runTag);
  const invoiceId = await trytonCreate(
    "account.invoice",
    {
      company: DEV_COMPANY_ID,
      type: "out",
      party: customer.partyId,
      invoice_address: customer.addressId,
      currency: currencyId,
      account: receivableId,
      journal: journalId,
      invoice_date: trytonDate(saleDateIso),
      description: `US-525 SnappPay sale ${runTag}`,
      lines: [
        [
          "create",
          [
            {
              account: revenueId,
              quantity: 1,
              unit_price: trytonDecimal(String(SNAPPAY_GROSS_IRR)),
              description: "US-525 SnappPay sale",
            },
          ],
        ],
      ],
      payment_term: termId,
      alternative_payees: [["add", [gatewayPartyId]]],
    },
    { company: DEV_COMPANY_ID },
  );
  const validated = await trytonAction<{ id: number; state: string }>(
    "account.invoice",
    invoiceId,
    "validate_invoice",
    ["id", "state"],
  );
  expect(validated.state, `invoice ${invoiceId} did not validate`).toBe("validated");
  const posted = await trytonAction<{ id: number; state: string; move: number | null }>(
    "account.invoice",
    invoiceId,
    "post",
    ["id", "state", "move"],
  );
  expect(posted.state, `invoice ${invoiceId} did not post`).toBe("posted");
  if (posted.move === null || posted.move === undefined) {
    throw new Error(`Posted invoice ${invoiceId} has no move`);
  }
  const lines = await trytonSearch<{
    id: number;
    debit: unknown;
    credit: unknown;
    party: number;
    maturity_date: unknown;
  }>(
    "account.move.line",
    [
      ["move", "=", posted.move],
      ["account", "=", receivableId],
    ],
    ["debit", "credit", "party", "maturity_date"],
  );
  expect(lines, `invoice ${invoiceId} must post 4 receivable lines`).toHaveLength(4);
  const sorted = [...lines].sort((a, b) =>
    trytonDateToIso(a.maturity_date).localeCompare(trytonDateToIso(b.maturity_date)),
  );
  sorted.forEach((line, index) => {
    expect(
      Number(trytonDecimalToString(line.debit)),
      `receivable line ${index + 1} debit`,
    ).toBe(SNAPPAY_TRANCHE_GROSS_IRR);
    expect(
      Number(trytonDecimalToString(line.credit)),
      `receivable line ${index + 1} credit`,
    ).toBe(0);
    expect(line.party, `receivable line ${index + 1} party`).toBe(gatewayPartyId);
    expect(
      trytonDateToIso(line.maturity_date),
      `receivable line ${index + 1} maturity`,
    ).toBe(dues[index]);
  });
  return { invoiceId, saleDateIso, dues };
}

/**
 * Post the sale fee (policy A, at sale) via the BFF — `post_sale_fee`
 * creates the per-tranche fee legs (Cr receivable, party=gateway,
 * maturity=tranche due) the clearing pre-check reconciles against.
 * Body field is `date` (the request schema's validation alias).
 */
export async function postSaleFee(page: Page, paymentId: string, saleDateIso: string): Promise<void> {
  const result = await bff<{
    fee_irr: number;
    fee_vat_irr: number;
    tryton_move_id: number | null;
  }>(page, "POST", `settlements/payments/${paymentId}/post-sale-fee`, {
    company_id: DEV_COMPANY_ID,
    date: saleDateIso,
    actor: "US-525 e2e",
  });
  expect(result.fee_irr, "sale fee recognised").toBe(SNAPPAY_FEE_IRR);
  expect(result.fee_vat_irr, "sale fee VAT recognised").toBe(SNAPPAY_FEE_VAT_IRR);
  expect(result.tryton_move_id, "fee move posted").not.toBeNull();
}

/**
 * Seed the full SnappPay sale: the Tryton invoice (tranche payment
 * term + gateway payee) → the HesabYar payment linked to it →
 * the sale fee (policy A).
 */
export async function seedSnappPaySale(
  page: Page,
  transactionId: string,
): Promise<RecordedPayment & { invoiceId: number }> {
  const confirmedAt = new Date().toISOString();
  const saleDateIso = confirmedAt.slice(0, 10);
  const dues = snappPayTrancheDueDates(saleDateIso);
  const sale = await postSaleInvoice(saleDateIso, dues, transactionId);
  const recorded = await recordSnappPayPayment(page, transactionId, {
    trytonInvoiceId: sale.invoiceId,
    confirmedAt,
    expectedDues: dues,
  });
  await postSaleFee(page, recorded.paymentId, saleDateIso);
  return { ...recorded, invoiceId: sale.invoiceId };
}

// -- Payment + file ----------------------------------------------------------

export interface RecordedPayment {
  paymentId: string;
  transactionId: string;
  trancheIds: string[];
  salesPeriod: string;
}

export interface RecordPaymentOptions {
  /** Posted Tryton sale invoice to link the payment to. */
  trytonInvoiceId?: number;
  /** Confirmation timestamp (ISO); defaults to now. The sale date is its UTC date. */
  confirmedAt?: string;
  /** Tranche due dates the backend derivation must produce (cross-check). */
  expectedDues?: string[];
}

/** Record one 100M SnappPay payment via the BFF; returns its schedule. */
export async function recordSnappPayPayment(
  page: Page,
  transactionId: string,
  options: RecordPaymentOptions = {},
): Promise<RecordedPayment> {
  const confirmedAt = options.confirmedAt ?? new Date().toISOString();
  const recorded = await bff<{
    payment: { id: string; sales_period_jalali: string };
    tranches: { id: string; expected_date: string }[];
  }>(page, "POST", "settlements/payments", {
    channel_key: "snapppay",
    gateway_transaction_id: transactionId,
    gross_irr: SNAPPAY_GROSS_IRR,
    gateway_confirmed_at: confirmedAt,
    source: "store_webhook",
    store_order_id: `ORD-525-${transactionId}`,
    ...(options.trytonInvoiceId !== undefined
      ? { tryton_invoice_id: options.trytonInvoiceId }
      : {}),
  });
  expect(recorded.tranches).toHaveLength(4);
  if (options.expectedDues !== undefined) {
    const derived = recorded.tranches.map((t) => t.expected_date);
    expect(
      derived,
      "derived tranche dues must match the payment term's maturities",
    ).toEqual(options.expectedDues);
  }
  return {
    paymentId: recorded.payment.id,
    transactionId,
    trancheIds: recorded.tranches.map((t) => t.id),
    salesPeriod: recorded.payment.sales_period_jalali,
  };
}

/**
 * The detail CSV the gateway would send: 4 rows, one per tranche,
 * each net matching its tranche exactly, with a shared tracking ref
 * (the deposit auto-matches on it, C5 rule 1).
 *
 * The row date is the TRANSACTION (sale) date — today — not the
 * tranche due date: §3.2 reassigns the payment's sales period to the
 * file row's month ("the file's month wins"), so due dates would
 * wrongly move the payment to the next month.
 */
export function buildSettlementCsv(transactionId: string, trackingRef: string): string {
  const { jy, jm, jd } = gregorianToJalali(new Date());
  const saleDate = `${jy}/${String(jm).padStart(2, "0")}/${String(jd).padStart(2, "0")}`;
  const header = "شماره تراکنش,تاریخ,مبلغ ناخالص,کارمزد,مالیات,مبلغ خالص,شماره پیگیری";
  const rows = [1, 2, 3, 4].map(
    () =>
      `${transactionId},${saleDate},${SNAPPAY_TRANCHE_GROSS_IRR},${SNAPPAY_TRANCHE_FEE_IRR},${SNAPPAY_TRANCHE_VAT_IRR},${SNAPPAY_TRANCHE_NET_IRR},${trackingRef}`,
  );
  return [header, ...rows].join("\n");
}

/** The schedule rows for one transaction id (one row per tranche). */
export function trancheRowsFor(page: Page, transactionId: string) {
  return page.locator("tr", { has: page.getByRole("link", { name: transactionId, exact: true }) });
}

/**
 * The schedule table loads 20 tranches per page (cursor "load more"
 * via «صفحهٔ بعد», appending rows). The dev DB accumulates one
 * payment (4 tranches) per e2e run in the same sales period, so this
 * run's tranches can span a page boundary — click through until the
 * button disappears, so the row counts cover the whole filter.
 * Caps at 10 extra pages so a runaway cursor cannot loop forever.
 */
export async function loadAllSchedulePages(page: Page): Promise<void> {
  for (let i = 0; i < 10; i++) {
    const nextButton = page.getByRole("button", { name: "صفحهٔ بعد" });
    if ((await nextButton.count()) === 0) return;
    const rowsBefore = await page.locator("tbody tr").count();
    await nextButton.click();
    // Wait for the next page to land: either rows append, or the
    // load finishes with no new rows and no next button (the total
    // landed exactly on a page boundary). The button reads «در حال
    // بارگذاری…» while fetching, so "no next button" alone is not
    // a finished signal — it must also not be loading.
    await expect
      .poll(
        async () => {
          const rows = await page.locator("tbody tr").count();
          if (rows > rowsBefore) return true;
          const busy = await page.getByRole("button", { name: "در حال بارگذاری…" }).count();
          const next = await page.getByRole("button", { name: "صفحهٔ بعد" }).count();
          return busy === 0 && next === 0;
        },
        { timeout: 90_000 },
      )
      .toBe(true);
  }
  throw new Error("Schedule still has a next page after 10 loads — refusing to loop forever.");
}

// -- Contract-version date picker -------------------------------------------

/** Jalali month names, as rendered by the calendar (lib/jalali JALALI_MONTHS). */
const JALALI_MONTH_NAMES = [
  "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
  "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
];

/** Add `days` to a Gregorian ISO date (noon-UTC arithmetic, TZ-safe). */
function addDaysIso(isoDate: string, days: number): string {
  const d = new Date(`${isoDate}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

/**
 * The earliest valid_from for a NEW contract version on a channel:
 * one day after the active version's valid_from. The backend requires
 * versions to move forward in time (valid_from must be strictly after
 * the active version's — application/commands/integrations.py), and
 * the form defaults to today, which 422s whenever the active version
 * is already effective today (run 16: zarinpal v1 effective
 * 2026-11-19, `INTEGRATION_CONTRACT_INVALID`). Reading the active
 * version keeps the test green across same-day re-runs, where a fixed
 * "tomorrow" would collide with the version the previous run created.
 * No active contract (404) → tomorrow.
 */
export async function nextContractValidFrom(page: Page, channelKey: string): Promise<string> {
  const res = await page.request.fetch(
    `/api/bff/integrations/channels/${encodeURIComponent(channelKey)}/contracts/active`,
  );
  if (res.status() === 404) return addDaysIso(new Date().toISOString().slice(0, 10), 1);
  expect(res.ok(), `GET active contract ${channelKey} → ${res.status()}: ${await res.text()}`).toBe(true);
  const body = (await res.json()) as { valid_from: string };
  return addDaysIso(body.valid_from, 1);
}

/**
 * Pick a Gregorian ISO date in a JalaliDatePicker: opens the calendar,
 * steps forward month by month to the target month (the picker opens
 * on the current value's month, which is today here; the target is
 * always in the future), then clicks the day cell. Day cells carry
 * aria-labels like «۲۹ آبان» plus an optional holiday suffix, hence
 * the anchored regex (the space after the day number keeps «۲» from
 * matching «۲۹»).
 */
export async function pickJalaliDate(page: Page, pickerId: string, targetIso: string): Promise<void> {
  const target = new Date(`${targetIso}T12:00:00Z`);
  const { jy, jm, jd } = gregorianToJalali(target);
  const monthName = JALALI_MONTH_NAMES[jm - 1];
  const today = gregorianToJalali(new Date());
  const steps = (jy - today.jy) * 12 + (jm - today.jm);
  if (steps < 0) throw new Error(`pickJalaliDate: ${targetIso} is before the current month.`);
  await page.locator(`#${pickerId} button`).first().click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible({ timeout: 30_000 });
  for (let i = 0; i < steps; i++) {
    await dialog.getByRole("button", { name: "ماه بعد" }).click();
  }
  await expect(dialog.getByText(`${monthName} ${faDigits(jy)}`, { exact: true })).toBeVisible({
    timeout: 30_000,
  });
  await dialog
    .getByRole("gridcell", { name: new RegExp(`^${faDigits(jd)} ${monthName}`) })
    .click();
  await expect(dialog).toBeHidden({ timeout: 30_000 });
}
