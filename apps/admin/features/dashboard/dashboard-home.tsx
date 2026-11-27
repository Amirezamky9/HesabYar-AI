"use client";

/**
 * Dashboard home (US-551) — the unified dashboard: six live sections
 * (§7.3) + the US-302 overview widgets below.
 *
 * Sections, in order: پولِ امروز (today's succeeded receipts +
 * payments by journal), پولِ در راه (gateway receivables, this
 * month's settlement tranches, in-clearing received cheques,
 * internal installments due this month), نیازمند توجه (review-queue
 * counters), خزانه (cashbox balances, cheque exposure / in-clearing /
 * due-soon — all company-scoped — plus issued cheques due this
 * week), هشدارها (overdue tranches / installments, cheques due
 * soon, reorder crossed, uncounted cashboxes, stuck inbox),
 * ایمپورت‌های دستی (recent statement + settlement import batches).
 * The cash-forecast area is the US-553 DashboardCashCard (self-contained:
 * it fetches its own next-30-days forecast).
 *
 * Every live KPI states its period, company and unit and links to
 * its filtered operational page (§11.3). Figures without an API
 * source (bank book balances) are honest coming-soon rows, never
 * zeros. Each section's calls are failure-tolerant: one failed
 * source degrades its own rows, never the whole dashboard.
 *
 * The company comes from the shell's CompanyProvider; all calls are
 * company-scoped where the API supports it (cheques since US-551).
 * Tranches + settlement imports have no company field in the API —
 * they are installation-wide and the widget says so. Installment
 * plans carry `company_engine_id`, so the small plan list is
 * scoped on the client (the installments API has no company filter
 * yet — unlike cheques, where the backend filter exists).
 * Load state carries the company id it belongs to, so switching
 * company never shows another company's figures while the new data
 * loads.
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import { TriangleAlert } from "lucide-react";
import {
  api,
  ApiError,
  type BankImportBatchResponse,
  type CashBoxResponse,
  type ChequeResponse,
  type ClearingAgingResponse,
  type CollateralSummaryResponse,
  type InstallmentLegResponse,
  type InstallmentPlanResponse,
  type InvoiceResponse,
  type MaterialDetailResponse,
  type OverdueReportResponse,
  type PaymentResponse,
  type ReviewQueueSummaryResponse,
  type SettlementFileImportResponse,
  type SettlementTrancheResponse,
  type SetupStatusResponse,
  type StaleInboxResponse,
} from "@/lib/api-client";
import { Button, Skeleton } from "@/components/ui";
import { todayIso } from "@/lib/jalali";
import { formatJalaliLong, todayJalaliString } from "@/features/cheques/cheque-data";
import { apiErrorMessage, useCompany } from "./company-context";
// US-553: self-contained dashboard cash card (own file, own fetch).
import { DashboardCashCard } from "@/features/cash-forecast/dashboard-cash-card";
import {
  buildAlerts,
  fetchAllPages,
  importBatchRows,
  jalaliMonthBounds,
  packagingReorderAlerts,
  recentInvoices,
  recentPayments,
  summarizeCashboxes,
  summarizeDrafts,
  summarizeInClearingReceived,
  summarizeInvoices,
  summarizeIssuedDueSoon,
  summarizeLegsDueThisMonth,
  summarizeReceivedDueSoon,
  summarizeTodayPayments,
  summarizeTranches,
  type DraftSummary,
  type FetchAllResult,
  type InvoiceSummary,
} from "./dashboard-data";
import {
  AlertsWidget,
  ChequeDueSoonWidget,
  ChequeExposureWidget,
  ChequeInClearingWidget,
  ComingSoonWidget,
  DashboardSection,
  DraftsWidget,
  ManualImportsWidget,
  MoneyOnTheWayWidget,
  PayablesWidget,
  ReceivablesWidget,
  RecentInvoicesWidget,
  RecentPaymentsWidget,
  ReviewQueueWidget,
  SetupStatusWidget,
  TodayMoneyWidget,
  TreasuryWidget,
  WidgetSkeleton,
} from "./widgets";

/** One failure-tolerant section call: `{ ok: false }` degrades its own rows. */
type LoadResult<T> = { ok: true; value: T } | { ok: false; value: null };

function tolerant<T>(promise: Promise<T>): Promise<LoadResult<T>> {
  return promise
    .then((value) => ({ ok: true as const, value }))
    .catch(() => ({ ok: false as const, value: null }));
}

interface InstallmentsData {
  /** The active company's plans (the API list is tenant-wide). */
  plans: InstallmentPlanResponse[];
  /** Legs of the company's open internal plans (BNPL excluded — its
   * money arrives via gateway tranches, counted in پولِ در راه). */
  legs: InstallmentLegResponse[];
  /** True when more than 10 open plans exist (details capped at 10). */
  legsTruncated: boolean;
}

/** Plan-detail fetch is capped: legs come from at most 10 open plans. */
const MAX_PLAN_DETAILS = 10;

async function loadInstallments(companyEngineId: number): Promise<InstallmentsData> {
  const plans = await api.listInstallmentPlans({ limit: 50 });
  const own = plans.filter((plan) => plan.company_engine_id === companyEngineId);
  const open = own.filter(
    (plan) =>
      plan.kind === "internal" && (plan.status === "active" || plan.status === "overdue"),
  );
  const capped = open.slice(0, MAX_PLAN_DETAILS);
  const details = await Promise.all(capped.map((plan) => api.getInstallmentPlan(plan.id)));
  return {
    plans: own,
    legs: details.flatMap((detail) => detail.legs),
    legsTruncated: open.length > capped.length,
  };
}

interface DashboardData {
  drafts: DraftSummary;
  invoices: InvoiceSummary;
  payments: PaymentResponse[];
  recentInvoiceRows: InvoiceResponse[];
  recentPaymentRows: PaymentResponse[];
  truncated: boolean;
  setup: LoadResult<SetupStatusResponse>;
  reviewQueue: LoadResult<ReviewQueueSummaryResponse>;
  /** Cheque reads are company-scoped (US-551 backend filter). */
  cheques: LoadResult<ChequeResponse[]>;
  aging: LoadResult<ClearingAgingResponse[]>;
  collateral: LoadResult<CollateralSummaryResponse>;
  tranches: LoadResult<FetchAllResult<SettlementTrancheResponse>>;
  installments: LoadResult<InstallmentsData>;
  overdueInstallments: LoadResult<OverdueReportResponse>;
  cashboxes: LoadResult<CashBoxResponse[]>;
  materials: LoadResult<MaterialDetailResponse[]>;
  staleInbox: LoadResult<StaleInboxResponse>;
  statementImports: LoadResult<BankImportBatchResponse[]>;
  settlementImports: LoadResult<SettlementFileImportResponse[]>;
}

type LoadState =
  | { kind: "loading"; companyId: string | null }
  | { kind: "ok"; companyId: string; data: DashboardData }
  | { kind: "error"; companyId: string; message: string };

export function DashboardHome() {
  const router = useRouter();
  const { company, companyId, status: companyStatus, errorMessage: companyError, refresh } = useCompany();
  const [state, setState] = React.useState<LoadState>({ kind: "loading", companyId: null });
  const [nonce, setNonce] = React.useState(0);

  React.useEffect(() => {
    if (companyStatus !== "ready" || companyId === null || company === null) return;
    let cancelled = false;
    const engineId = company.engine_id;

    // The core batch (drafts, invoices, payments) is hard-fail: the
    // page cannot render without it. Every section call resolves to
    // a failure marker instead of rejecting the whole batch — an
    // older key may lack a scope (settlement/installments), and a
    // slow Tryton call must not kill the HesabYar-DB sections.
    Promise.all([
      fetchAllPages((cursor) => api.listJournalDrafts({ company: companyId, limit: 50, cursor })),
      fetchAllPages((cursor) => api.listInvoices({ company: companyId, limit: 50, cursor })),
      api.listPayments({ company: companyId, limit: 50 }),
      tolerant(api.getSetupStatus(companyId)),
      tolerant(api.getReviewQueueSummary({ company: companyId })),
      tolerant(api.listCheques({ company: companyId, limit: 100 })),
      tolerant(api.listInClearingAging({ company: companyId })),
      tolerant(api.getCollateralSummary({ company: companyId })),
      tolerant(fetchAllPages((cursor) => api.listSettlementTranches({ limit: 50, cursor }))),
      tolerant(loadInstallments(engineId)),
      tolerant(api.listOverdueInstallments({ limit: 50 })),
      tolerant(api.listCashBoxes({ company: companyId }).then((page) => page.items)),
      tolerant(api.listPackagingMaterials(companyId, { limit: 50 }).then((page) => page.items)),
      tolerant(api.listStaleInboxEvents({ hours: 24, limit: 10 })),
      tolerant(api.listStatementImports({ company: companyId })),
      tolerant(api.listSettlementImports({ limit: 10 }).then((page) => page.items)),
    ])
      .then(
        ([
          draftPages,
          invoicePages,
          paymentPage,
          setup,
          reviewQueue,
          cheques,
          aging,
          collateral,
          tranches,
          installments,
          overdueInstallments,
          cashboxes,
          materials,
          staleInbox,
          statementImports,
          settlementImports,
        ]) => {
          if (cancelled) return;
          setState({
            kind: "ok",
            companyId,
            data: {
              drafts: summarizeDrafts(draftPages.items),
              invoices: summarizeInvoices(invoicePages.items),
              payments: paymentPage.items,
              recentInvoiceRows: recentInvoices(invoicePages.items, 5),
              recentPaymentRows: recentPayments(paymentPage.items, 5),
              truncated: draftPages.truncated || invoicePages.truncated,
              setup,
              reviewQueue,
              cheques,
              aging,
              collateral,
              tranches,
              installments,
              overdueInstallments,
              cashboxes,
              materials,
              staleInbox,
              statementImports,
              settlementImports,
            },
          });
        },
      )
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.push("/login");
          return;
        }
        setState({
          kind: "error",
          companyId,
          message: apiErrorMessage(err, "دریافت داده‌های داشبورد ناموفق بود."),
        });
      });
    return () => {
      cancelled = true;
    };
  }, [companyStatus, companyId, company, nonce, router]);

  const retry = () => {
    setState({ kind: "loading", companyId });
    setNonce((n) => n + 1);
  };

  const companyName = company?.name ?? "";
  // Data for another company is stale: show skeletons, never stale figures.
  const stale = state.companyId !== companyId;
  const data = state.kind === "ok" && !stale ? state.data : null;
  const setup = data?.setup.ok ? data.setup.value : null;
  const periodLabel = setup?.open_fiscal_year
    ? `سال مالی ${setup.open_fiscal_year.name}`
    : "همهٔ دوره‌ها";
  const todayJalali = todayJalaliString();
  const todayLabel = formatJalaliLong(todayJalali);
  const monthBounds = jalaliMonthBounds(new Date());

  // -- section summaries (pure, computed from the loaded lists) --------------
  const todayMoney = data ? summarizeTodayPayments(data.payments, todayIso()) : null;
  const trancheSummary = data?.tranches.ok
    ? summarizeTranches(data.tranches.value.items, monthBounds.startIso, monthBounds.endIso)
    : null;
  const inClearing = data?.cheques.ok ? summarizeInClearingReceived(data.cheques.value) : null;
  const legsDue =
    data?.installments.ok
      ? summarizeLegsDueThisMonth(
          data.installments.value.legs,
          monthBounds.startJalali,
          monthBounds.endJalali,
        )
      : null;
  const cashboxSummary = data?.cashboxes.ok ? summarizeCashboxes(data.cashboxes.value) : null;
  const issuedDueSoon = data?.cheques.ok
    ? summarizeIssuedDueSoon(data.cheques.value, todayJalali)
    : null;
  const receivedDueSoon = data?.cheques.ok
    ? summarizeReceivedDueSoon(data.cheques.value, todayJalali)
    : null;
  const reorderCrossed = data?.materials.ok ? packagingReorderAlerts(data.materials.value) : null;
  // The overdue report is tenant-wide: join it to the company's plans
  // (fetched above) so the alert counts only this company's legs. When
  // either call failed the alert is omitted — never a tenant-wide
  // figure under a company label.
  const overdueScoped =
    data?.installments.ok && data?.overdueInstallments.ok
      ? (() => {
          const ownPlanIds = new Set(data.installments.value.plans.map((plan) => plan.id));
          const rows = data.overdueInstallments.value.rows.filter((row) =>
            ownPlanIds.has(row.plan_id),
          );
          return {
            count: rows.length,
            totalIrr: rows.reduce((sum, row) => sum + row.balance_irr, 0),
          };
        })()
      : null;
  const alerts =
    data === null
      ? []
      : buildAlerts({
          overdueTranches:
            trancheSummary === null
              ? { count: 0, totalIrr: 0 }
              : { count: trancheSummary.overdueCount, totalIrr: trancheSummary.overdueTotalIrr },
          chequesDueSoon:
            receivedDueSoon === null
              ? { count: 0, totalIrr: 0 }
              : { count: receivedDueSoon.count, totalIrr: receivedDueSoon.totalIrr },
          overdueInstallments: overdueScoped ?? { count: 0, totalIrr: 0 },
          reorderCrossed: reorderCrossed ?? [],
          uncountedCashboxes: cashboxSummary?.uncounted ?? [],
          staleInbox: data.staleInbox.ok
            ? { count: data.staleInbox.value.stale_count, hours: 24 }
            : { count: 0, hours: 24 },
        });
  const alertSourcesFailed =
    data === null
      ? []
      : [
          data.tranches.ok ? null : "اقساط تسویه",
          data.cheques.ok ? null : "چک‌ها",
          data.installments.ok && data.overdueInstallments.ok ? null : "اقساط",
          data.materials.ok ? null : "بسته‌بندی",
          data.cashboxes.ok ? null : "صندوق‌ها",
          data.staleInbox.ok ? null : "صندوق ورودی",
        ].filter((label): label is string => label !== null);
  const importRows =
    data === null
      ? []
      : importBatchRows(
          data.statementImports.ok ? data.statementImports.value : [],
          data.settlementImports.ok ? data.settlementImports.value : [],
          5,
        );

  if (companyStatus === "loading") {
    return (
      <div className="space-y-6" aria-label="در حال بارگذاری داشبورد">
        <div className="space-y-2">
          <h1 className="text-2xl font-bold">داشبورد</h1>
          <Skeleton className="h-4 w-72" />
        </div>
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <WidgetSkeleton key={i} />
          ))}
        </div>
      </div>
    );
  }

  if (companyStatus === "error") {
    return (
      <div className="space-y-4" role="alert">
        <h1 className="text-2xl font-bold">داشبورد</h1>
        <p className="flex items-start gap-2 text-sm text-destructive">
          <TriangleAlert className="mt-1 size-4 shrink-0" aria-hidden />
          {companyError ?? "دریافت فهرست شرکت‌ها ناموفق بود."}
        </p>
        <Button variant="outline" onClick={refresh}>
          تلاش دوباره
        </Button>
      </div>
    );
  }

  if (companyStatus === "empty" || company === null) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-bold">داشبورد</h1>
        <p className="text-sm text-muted-foreground">
          هیچ شرکتی در دسترس این کلید نیست؛ ابتدا یک شرکت بسازید یا کلیدی با دسترسی به شرکت بگیرید.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold">داشبورد</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          نمای کلی {companyName}
          {setup?.open_fiscal_year ? ` · سال مالی باز: ${setup.open_fiscal_year.name}` : ""}
        </p>
      </div>

      {state.kind === "error" ? (
        <div className="space-y-4" role="alert">
          <p className="flex items-start gap-2 text-sm text-destructive">
            <TriangleAlert className="mt-1 size-4 shrink-0" aria-hidden />
            {state.message}
          </p>
          <Button variant="outline" onClick={retry}>
            تلاش دوباره
          </Button>
        </div>
      ) : state.kind === "loading" || stale || data === null ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3" aria-label="در حال بارگذاری">
          {Array.from({ length: 6 }).map((_, i) => (
            <WidgetSkeleton key={i} />
          ))}
        </div>
      ) : (
        <>
          {data.truncated ? (
            <p className="rounded-lg border border-warning/40 bg-warning/10 px-4 py-3 text-sm text-warning-foreground" role="note">
              فهرست صورت‌حساب‌ها یا پیش‌نویس‌ها بسیار بزرگ است؛ مجموع‌ها بر اساس ۵۰۰ رکورد اول محاسبه شده‌اند.
            </p>
          ) : null}

          <DashboardSection
            id="section-today"
            title="پولِ امروز"
            description="دریافتی‌ها و پرداختی‌های موفق امروز، به تفکیک دفتر پرداخت."
            testId="dashboard-section-today"
          >
            {todayMoney === null ? null : (
              <TodayMoneyWidget
                summary={todayMoney}
                todayLabel={todayLabel}
                companyName={companyName}
                periodLabel={periodLabel}
              />
            )}
          </DashboardSection>

          <DashboardSection
            id="section-on-the-way"
            title="پولِ در راه"
            description="پولی که قرار است برسد: اقساط تسویهٔ درگاه، چک‌های در وصول و اقساط این ماه."
            testId="dashboard-section-on-the-way"
          >
            <MoneyOnTheWayWidget
              tranches={trancheSummary}
              tranchesTruncated={data.tranches.ok ? data.tranches.value.truncated : false}
              inClearing={inClearing}
              legsDue={legsDue}
              legsTruncated={data.installments.ok ? data.installments.value.legsTruncated : false}
              monthLabel={monthBounds.monthLabel}
              companyName={companyName}
              periodLabel={periodLabel}
            />
          </DashboardSection>

          <DashboardSection
            id="section-attention"
            title="نیازمند توجه"
            description="صف بازبینی: آیتم‌های در انتظار بررسی، قدیمی‌ترین آیتم و موارد گذشته از مهلت."
            testId="dashboard-section-attention"
          >
            {data.reviewQueue.ok ? (
              <ReviewQueueWidget
                summary={data.reviewQueue.value}
                companyName={companyName}
                periodLabel={periodLabel}
              />
            ) : (
              <ComingSoonWidget
                title="صف بازبینی"
                reason="دریافت شمارنده‌های صف بازبینی ناموفق بود؛ از صفحهٔ صف بازبینی دوباره تلاش کنید."
                icon="reports"
                href="/review-queue"
                linkLabel="مشاهدهٔ صف بازبینی"
              />
            )}
          </DashboardSection>

          <DashboardSection
            id="section-treasury"
            title="خزانه"
            description="موجودی صندوق‌ها، تعهد چک و چک‌های سررسید این هفته."
            testId="dashboard-section-treasury"
          >
            <TreasuryWidget
              cashboxes={cashboxSummary}
              issuedDueSoon={issuedDueSoon}
              companyName={companyName}
              periodLabel={periodLabel}
            />
            {data.cheques.ok && data.collateral.ok ? (
              <ChequeExposureWidget
                cheques={data.cheques.value}
                collateral={data.collateral.value}
                companyName={companyName}
                periodLabel={periodLabel}
              />
            ) : (
              <ComingSoonWidget
                title="تعهد چک"
                reason="دریافت داده‌های چک ناموفق بود؛ ممکن است کلید دسترسی خواندن چک‌ها را نداشته باشد."
                icon="reports"
                href="/cheques"
                linkLabel="مشاهدهٔ مرکز چک"
              />
            )}
            {data.aging.ok ? (
              <ChequeInClearingWidget
                aging={data.aging.value}
                companyName={companyName}
                periodLabel={periodLabel}
              />
            ) : (
              <ComingSoonWidget
                title="چک‌های در جریان وصول"
                reason="دریافت گزارش کهنگی وصول ناموفق بود؛ از صفحهٔ گزارش‌های چک دوباره تلاش کنید."
                icon="reports"
                href="/cheques/reports?report=in-clearing"
                linkLabel="مشاهدهٔ گزارش وصول"
              />
            )}
            {data.cheques.ok ? (
              <ChequeDueSoonWidget
                cheques={data.cheques.value}
                todayJalali={todayJalali}
                companyName={companyName}
                periodLabel={periodLabel}
              />
            ) : (
              <ComingSoonWidget
                title="چک‌های نزدیک به سررسید"
                reason="دریافت داده‌های چک ناموفق بود؛ ممکن است کلید دسترسی خواندن چک‌ها را نداشته باشد."
                icon="reports"
                href="/cheques/reports?report=due-received"
                linkLabel="مشاهدهٔ گزارش سررسید"
              />
            )}
          </DashboardSection>

          <DashboardSection
            id="section-cash-forecast"
            title="پیش‌بینی نقدینگی"
            description="تقویم پیش‌بینی نقد — ورودی‌ها و خروجی‌های موردانتظار ۳۰ تا ۱۲۰ روز آینده."
            testId="dashboard-section-cash-forecast"
          >
            <DashboardCashCard companyId={companyId} />
          </DashboardSection>

          <DashboardSection
            id="section-alerts"
            title="هشدارها"
            description="مواردی که نیازمند اقدام‌اند: سررسیدگذشته‌ها، صندوق ورودی گیرکرده و نقطه‌های سفارش."
            testId="dashboard-section-alerts"
          >
            <AlertsWidget
              alerts={alerts}
              failedSources={alertSourcesFailed}
              companyName={companyName}
              periodLabel={periodLabel}
            />
          </DashboardSection>

          <DashboardSection
            id="section-imports"
            title="ایمپورت‌های دستی"
            description="آخرین بسته‌های ایمپورت صورت‌حساب و تسویه، با وضعیت و اثر حسابداری."
            testId="dashboard-section-imports"
          >
            <ManualImportsWidget
              rows={importRows}
              statementFailed={!data.statementImports.ok}
              settlementFailed={!data.settlementImports.ok}
              companyName={companyName}
              periodLabel={periodLabel}
            />
          </DashboardSection>

          <DashboardSection
            id="section-overview"
            title="وضعیت کلی"
            description="پیش‌نویس‌ها، مطالبات، بدهی‌ها و وضعیت راه‌اندازی."
            testId="dashboard-section-overview"
          >
            <DraftsWidget summary={data.drafts} companyName={companyName} periodLabel={periodLabel} />
            <ReceivablesWidget summary={data.invoices} companyName={companyName} periodLabel={periodLabel} />
            <PayablesWidget summary={data.invoices} companyName={companyName} periodLabel={periodLabel} />
            <RecentInvoicesWidget
              invoices={data.recentInvoiceRows}
              companyName={companyName}
              periodLabel={periodLabel}
            />
            <RecentPaymentsWidget
              payments={data.recentPaymentRows}
              companyName={companyName}
              periodLabel={periodLabel}
            />
            {data.setup.ok ? (
              <SetupStatusWidget setup={data.setup.value} companyName={companyName} periodLabel={periodLabel} />
            ) : (
              <ComingSoonWidget
                title="وضعیت راه‌اندازی"
                reason="وضعیت راه‌اندازی در دسترس نیست؛ از بخش سال‌های مالی دوباره تلاش کنید."
                icon="reports"
              />
            )}
          </DashboardSection>
        </>
      )}
    </div>
  );
}
