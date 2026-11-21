import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, bffRequest, newIdempotencyKey } from "./api-client";

function mockFetchOnce(status: number, body: unknown, headers: Record<string, string> = {}) {
  const res = new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(res));
  return fetch as unknown as ReturnType<typeof vi.fn>;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("bffRequest", () => {
  it("calls the same-origin BFF route and returns JSON", async () => {
    const fetchMock = mockFetchOnce(200, { items: [], next_cursor: null });
    const data = await bffRequest("companies?limit=1");
    expect(data).toEqual({ items: [], next_cursor: null });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/bff/companies?limit=1",
      expect.objectContaining({ method: "GET", credentials: "same-origin" }),
    );
  });

  it("throws ApiError with the backend's stable code on failure", async () => {
    mockFetchOnce(403, {
      type: "https://hesabyar.local/problems/forbidden",
      title: "Forbidden",
      status: 403,
      detail: "Missing scope company:read.",
      code: "FORBIDDEN",
      request_id: "req-1",
    });
    const err = await bffRequest("companies").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err as ApiError).toMatchObject({
      status: 403,
      code: "FORBIDDEN",
      requestId: "req-1",
    });
    expect((err as ApiError).isForbidden).toBe(true);
    expect((err as ApiError).isUnauthorized).toBe(false);
  });

  it("flags 401 as unauthorized (session gone)", async () => {
    mockFetchOnce(401, { code: "UNAUTHORIZED", detail: "Not signed in.", status: 401 });
    const err = await bffRequest("companies").catch((e: unknown) => e);
    expect((err as ApiError).isUnauthorized).toBe(true);
  });

  it("sends Idempotency-Key and JSON body on mutations", async () => {
    const fetchMock = mockFetchOnce(201, { id: 7 });
    await bffRequest("accounting/journal-drafts", {
      method: "POST",
      body: { company_id: "c1" },
      idempotencyKey: "key-123",
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/bff/accounting/journal-drafts",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ company_id: "c1" }),
        headers: expect.objectContaining({
          "Content-Type": "application/json",
          "Idempotency-Key": "key-123",
        }),
      }),
    );
  });
});

describe("api methods", () => {
  it("builds list query params and encodes ids", async () => {
    const fetchMock = mockFetchOnce(200, { items: [], next_cursor: null });
    await api.listAccounts({ company: "c-1", code: "1.1", limit: 25 });
    const url = (fetchMock.mock.calls[0]?.[0] ?? "") as string;
    expect(url).toContain("/api/bff/accounting/accounts?");
    expect(url).toContain("company=c-1");
    expect(url).toContain("code=1.1");
    expect(url).toContain("limit=25");
  });

  it("generates a UUID idempotency key per mutation by default", async () => {
    const fetchMock = mockFetchOnce(201, { id: 1 });
    await api.createJournalDraft({ company_id: "c1", date: "2026-11-17", lines: [] });
    const init = fetchMock.mock.calls[0]?.[1] as { headers: Record<string, string> };
    expect(init.headers["Idempotency-Key"]).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
    );
  });

  it("exposes all endpoints including the Phase 4 compliance, Phase 5 review-queue, Phase 5-2 cash-box and Phase 5-6 report/forecast/import methods", () => {
    // Count check: 3 health + 2 companies + 2 accounts + 2 fiscal-years +
    // 2 fiscal-periods + 1 setup-status + 7 journals + 4 invoices +
    // 2 payments + 4 statements + 2 audit = 31 methods (list+get pairs),
    // plus 22 Phase 4 compliance methods (US-401..US-404) = 53,
    // plus 24 Phase 5-6 methods (US-550 reports 5, US-551 dashboard 1,
    // US-552 review queue 2, US-553 cash forecast 2, US-554 imports 14)
    // = 231.
    const methods = Object.keys(api);
    for (const name of [
      "getHealth",
      "getReadiness",
      "getInstallation",
      "listCompanies",
      "getCompany",
      "listAccounts",
      "getAccount",
      "listFiscalYears",
      "getFiscalYear",
      "listFiscalPeriods",
      "getFiscalPeriod",
      "getSetupStatus",
      "listJournalDrafts",
      "createJournalDraft",
      "getJournalDraft",
      "updateJournalDraft",
      "validateJournalDraft",
      "postJournalDraft",
      "reverseJournal",
      "listInvoices",
      "getInvoice",
      "createInvoiceDraft",
      "validateInvoice",
      "listPayments",
      "getPayment",
      "listStatements",
      "getStatementImportStatus",
      "getStatement",
      "reconcileStatement",
      "listStatementMatches",
      "applyManualMatch",
      // Phase 5-2 bank import (US-514, B1/B2)
      "detectStatementFile",
      "listColumnMappings",
      "createColumnMapping",
      "supersedeColumnMapping",
      "createStatementImport",
      "listStatementImports",
      "getStatementImport",
      "confirmStatementImport",
      "cancelStatementImport",
      "listStatementJournals",
      "getReconciliationStatus",
      "listMatchRules",
      "setMatchRuleMode",
      "listAuditEvents",
      "getAuditEvent",
      // Phase 4 compliance (US-401 standards)
      "listStandards",
      "getStandard",
      "getValidationRun",
      // Phase 4 compliance (US-402 tax policy)
      "listTaxSources",
      "listTaxVersions",
      "getTaxVersion",
      "resolveTaxDecision",
      // Phase 4 compliance (US-403 report validations)
      "getTrialBalance",
      "createReportValidationRun",
      "getReportValidationRun",
      // Phase 4 compliance (US-404 Moadian)
      "listMoadianProfiles",
      "createMoadianProfile",
      "updateMoadianProfile",
      "listMoadianDocuments",
      "createMoadianDocument",
      "listMoadianSubmissions",
      "createMoadianSubmission",
      "getMoadianSubmission",
      "approveMoadianSubmission",
      "queueMoadianSubmission",
      "listMoadianStatusEvents",
      "runMoadianReconcile",
      // Phase 5 review queue (US-504)
      "listReviewItemTypes",
      "listReviewItems",
      "createReviewItem",
      "getReviewItem",
      "performReviewAction",
      "assignReviewItem",
      "getReviewQueueSummary",
      "listSlaPolicies",
      "updateSlaPolicy",
      // Phase 5-6 mapping promotion (US-552)
      "promoteIntegrationMapping",
      // Phase 5 wave 2 manual entries (US-513)
      "createManualOrder",
      "createManualRefund",
      "createManualCancellation",
      "listManualEntries",
      "getManualEntry",
      // Phase 5-2 packaging (US-515)
      "listPackagingLocations",
      "getPackagingSettings",
      "savePackagingSettings",
      "listPackagingMaterials",
      "createPackagingMaterial",
      "getPackagingMaterial",
      "updatePackagingMaterial",
      "setReorderPoint",
      "getMaterialCoverage",
      "recordConsumption",
      "listConsumptions",
      "createOrderCostCenter",
      "getOrderCost",
      "listShipmentCosts",
      "recordShipmentCost",
      "computePriceQuote",
      "listCarrierRates",
      "createCarrierRate",
      "updateCarrierRate",
      "createCount",
      "listCounts",
      "getCount",
      "setCountLine",
      "confirmCount",
      "cancelCount",
      "runSupplyCheck",
      // Phase 5-2 cash box (US-516)
      "getCashBoxSettings",
      "updateCashBoxSettings",
      "listCashBoxStatementJournals",
      "getCashBoxReport",
      "listCashBoxes",
      "createCashBox",
      "getCashBox",
      "updateCashBox",
      "startCountSession",
      "listCountSessions",
      "getCountSession",
      "enterCount",
      "signSession",
      "postVarianceJournal",
      "recountSession",
      // Phase 5-3 settlement + gateway channels (US-521–525)
      "listPaymentChannels",
      "getPaymentChannel",
      "updatePaymentChannel",
      "seedBuiltinChannels",
      "listGatewayContracts",
      "getActiveGatewayContract",
      "createGatewayContract",
      "recordGatewayPayment",
      "listGatewayPayments",
      "getGatewayPayment",
      "cancelGatewayPayment",
      "bulkRederivePayments",
      "reassignSalesPeriod",
      "listPaymentPostings",
      "listSettlementTranches",
      "getSettlementTranche",
      "writeOffTranche",
      "listTrancheMatches",
      "listSettlementEvents",
      "getSettlementEvent",
      "listEventMatches",
      "listSettlementExceptions",
      "getSettlementException",
      "resolveSettlementException",
      "listSettlementFeeRecords",
      "listSettlementImports",
      "getSettlementImport",
      "importSettlementFile",
      "approveSettlementImport",
      "rejectSettlementImport",
      "runImportTieOut",
      "runSettlementTick",
      "listBankDeposits",
      "getBankDeposit",
      "recordBankDeposit",
      "listDepositMatches",
      "matchDepositManually",
      // Phase 5-4 cheque center (US-535)
      "listCheques",
      "getCheque",
      "createCheque",
      "getChequeSettings",
      "listCheckbooks",
      "getCheckbook",
      "getCollateralSummary",
      "getEndorsementSummary",
      "listChequeIntake",
      "confirmChequeReceipt",
      "depositCheque",
      "clearCheque",
      "bounceCheque",
      "endorseCheque",
      "returnCheque",
      "rejectChequeMismatch",
      "issueCheque",
      "voidIssuedCheque",
      "callInCheque",
      "unexpectedCallInCheque",
      "endorsedBounceCheque",
      "recordInquiryReceipt",
      "runSayyadInquiry",
      "runEhrazinoInquiry",
      "listChequeInquiries",
      "confirmClearingMatch",
      "listInClearingAging",
      "getInquiryLedger",
      "runReceiptRuleAudit",
      "runChequeClearing",
      // Phase 5-5 installments center (US-545)
      "listInstallmentPlans",
      "getInstallmentPlan",
      "createInstallmentPlan",
      "createInstallmentReceipt",
      "rejectBnplReceipt",
      "listInstallmentContracts",
      "getInstallmentContract",
      "createInstallmentContract",
      "getPayoffQuote",
      "createEarlySettlement",
      "listOverdueLegs",
      "runDunning",
      "listPlanAmendments",
      "triggerPlanReminder",
      "listPlanReminders",
      "listSmsReminders",
      "listSmsProviders",
      // Phase 5-6 financial reports (US-550)
      "getBalanceSheet",
      "getCashFlow",
      "getGeneralLedger",
      "getIncomeStatement",
      "getNotesBalance",
      // Phase 5-6 unified dashboard (US-551)
      "listOverdueInstallments",
      // Phase 5-6 one review queue (US-552; promoteIntegrationMapping
      // is already listed above)
      "listStaleInboxEvents",
      // Phase 5-6 cash forecast + match trend (US-553)
      "getCashForecast",
      "getMonthlyMatchTrend",
      // Phase 5-6 manual imports (US-554)
      "approveImportBatch",
      "createSettlementColumnMapping",
      "detectImportFile",
      "getImportBatch",
      "getImportBatchPreview",
      "getImportBatchVariance",
      "getSettlementColumnMapping",
      "listImportBatches",
      "listSettlementColumnMappings",
      "rejectImportBatch",
      "rerunImportBatch",
      "retryBatchPosting",
      "supersedeSettlementColumnMapping",
      "uploadImportBatch",
    ]) {
      expect(methods).toContain(name);
    }
    expect(methods).toHaveLength(231);
  });
});

describe("newIdempotencyKey", () => {
  it("returns distinct UUIDs", () => {
    expect(newIdempotencyKey()).not.toBe(newIdempotencyKey());
  });
});
