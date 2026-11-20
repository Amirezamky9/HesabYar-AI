"use client";

/**
 * Dashboard home (US-302) — §11.3 widgets built ONLY from live API data.
 *
 * Data: unposted journal drafts, receivables/payables + recent invoices
 * (invoices list), recent payments, setup status. Figures without an API
 * source (cash position, trial-balance-based figures, tax/Moadian
 * status) are honest coming-soon tiles. Every live tile states its
 * period, company and unit and links to the underlying page.
 *
 * The company comes from the shell's CompanyProvider; all calls are
 * company-scoped (the API answers 400 COMPANY_REQUIRED otherwise).
 * Load state carries the company id it belongs to, so switching company
 * never shows another company's figures while the new data loads.
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import { TriangleAlert } from "lucide-react";
import {
  api,
  ApiError,
  type ChequeResponse,
  type ClearingAgingResponse,
  type CollateralSummaryResponse,
  type InvoiceResponse,
  type PaymentResponse,
  type ReviewQueueSummaryResponse,
  type SetupStatusResponse,
} from "@/lib/api-client";
import { Button, Skeleton } from "@/components/ui";
import { todayJalaliString } from "@/features/cheques/cheque-data";
import { apiErrorMessage, useCompany } from "./company-context";
import {
  fetchAllPages,
  recentInvoices,
  recentPayments,
  summarizeDrafts,
  summarizeInvoices,
  type DraftSummary,
  type InvoiceSummary,
} from "./dashboard-data";
import {
  ChequeDueSoonWidget,
  ChequeExposureWidget,
  ChequeInClearingWidget,
  ComingSoonWidget,
  DraftsWidget,
  PayablesWidget,
  ReceivablesWidget,
  RecentInvoicesWidget,
  RecentPaymentsWidget,
  ReviewQueueWidget,
  SetupStatusWidget,
  WidgetSkeleton,
} from "./widgets";

interface DashboardData {
  drafts: DraftSummary;
  invoices: InvoiceSummary;
  recentInvoiceRows: InvoiceResponse[];
  recentPaymentRows: PaymentResponse[];
  truncated: boolean;
  /** Null when the setup call failed — the tile says so honestly. */
  setup: SetupStatusResponse | null;
  setupFailed: boolean;
  /** Null when the review-queue call failed — the tile says so honestly. */
  reviewQueue: ReviewQueueSummaryResponse | null;
  reviewQueueFailed: boolean;
  /** Cheque widgets (US-535) — tenant-wide: the cheque list has no
   * company filter, so the tiles say «همهٔ شرکت‌ها». Each call is
   * failure-tolerant like the review-queue summary. */
  cheques: ChequeResponse[];
  chequesFailed: boolean;
  aging: ClearingAgingResponse[];
  agingFailed: boolean;
  collateral: CollateralSummaryResponse | null;
  collateralFailed: boolean;
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
    if (companyStatus !== "ready" || companyId === null) return;
    let cancelled = false;

    // Setup status loads alongside the money widgets but must not kill
    // them on failure: its promise resolves to a failure marker instead
    // of rejecting the whole batch. The review-queue summary and the
    // cheque widgets behave the same way (older keys may lack the
    // cheques:read scope).
    Promise.all([
      fetchAllPages((cursor) => api.listJournalDrafts({ company: companyId, limit: 50, cursor })),
      fetchAllPages((cursor) => api.listInvoices({ company: companyId, limit: 50, cursor })),
      api.listPayments({ company: companyId, limit: 50 }),
      api
        .getSetupStatus(companyId)
        .then((setup) => ({ ok: true as const, setup }))
        .catch(() => ({ ok: false as const, setup: null })),
      api
        .getReviewQueueSummary({ company: companyId })
        .then((summary) => ({ ok: true as const, summary }))
        .catch(() => ({ ok: false as const, summary: null })),
      api
        .listCheques({ limit: 100 })
        .then((cheques) => ({ ok: true as const, cheques }))
        .catch(() => ({ ok: false as const, cheques: [] as ChequeResponse[] })),
      api
        .listInClearingAging()
        .then((aging) => ({ ok: true as const, aging }))
        .catch(() => ({ ok: false as const, aging: [] as ClearingAgingResponse[] })),
      api
        .getCollateralSummary()
        .then((collateral) => ({ ok: true as const, collateral }))
        .catch(() => ({ ok: false as const, collateral: null })),
    ])
      .then(([draftPages, invoicePages, paymentPage, setupResult, reviewQueueResult, chequesResult, agingResult, collateralResult]) => {
        if (cancelled) return;
        setState({
          kind: "ok",
          companyId,
          data: {
            drafts: summarizeDrafts(draftPages.items),
            invoices: summarizeInvoices(invoicePages.items),
            recentInvoiceRows: recentInvoices(invoicePages.items, 5),
            recentPaymentRows: recentPayments(paymentPage.items, 5),
            truncated: draftPages.truncated || invoicePages.truncated,
            setup: setupResult.setup,
            setupFailed: !setupResult.ok,
            reviewQueue: reviewQueueResult.summary,
            reviewQueueFailed: !reviewQueueResult.ok,
            cheques: chequesResult.cheques,
            chequesFailed: !chequesResult.ok,
            aging: agingResult.aging,
            agingFailed: !agingResult.ok,
            collateral: collateralResult.collateral,
            collateralFailed: !collateralResult.ok,
          },
        });
      })
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
  }, [companyStatus, companyId, nonce, router]);

  const retry = () => {
    setState({ kind: "loading", companyId });
    setNonce((n) => n + 1);
  };

  const companyName = company?.name ?? "";
  // Data for another company is stale: show skeletons, never stale figures.
  const stale = state.companyId !== companyId;
  const setup = state.kind === "ok" && !stale ? state.data.setup : null;
  const periodLabel = setup?.open_fiscal_year
    ? `سال مالی ${setup.open_fiscal_year.name}`
    : "همهٔ دوره‌ها";

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
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">داشبورد</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          نمای کلی {companyName}
          {setup?.open_fiscal_year ? ` · سال مالی باز: ${setup.open_fiscal_year.name}` : ""}
        </p>
      </div>

      {state.kind === "loading" || stale ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3" aria-label="در حال بارگذاری">
          {Array.from({ length: 6 }).map((_, i) => (
            <WidgetSkeleton key={i} />
          ))}
        </div>
      ) : state.kind === "error" ? (
        <div className="space-y-4" role="alert">
          <p className="flex items-start gap-2 text-sm text-destructive">
            <TriangleAlert className="mt-1 size-4 shrink-0" aria-hidden />
            {state.message}
          </p>
          <Button variant="outline" onClick={retry}>
            تلاش دوباره
          </Button>
        </div>
      ) : (
        <>
          {state.data.truncated ? (
            <p className="rounded-lg border border-warning/40 bg-warning/10 px-4 py-3 text-sm text-warning-foreground" role="note">
              فهرست صورت‌حساب‌ها یا پیش‌نویس‌ها بسیار بزرگ است؛ مجموع‌ها بر اساس ۵۰۰ رکورد اول محاسبه شده‌اند.
            </p>
          ) : null}
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {state.data.reviewQueueFailed || state.data.reviewQueue === null ? (
              <ComingSoonWidget
                title="صف بازبینی"
                reason="دریافت شمارنده‌های صف بازبینی ناموفق بود؛ از صفحهٔ صف بازبینی دوباره تلاش کنید."
                icon="reports"
                href="/review-queue"
                linkLabel="مشاهدهٔ صف بازبینی"
              />
            ) : (
              <ReviewQueueWidget
                summary={state.data.reviewQueue}
                companyName={companyName}
                periodLabel={periodLabel}
              />
            )}
            {state.data.chequesFailed || state.data.collateralFailed ? (
              <ComingSoonWidget
                title="تعهد چک"
                reason="دریافت داده‌های چک ناموفق بود؛ ممکن است کلید دسترسی خواندن چک‌ها را نداشته باشد."
                icon="reports"
                href="/cheques"
                linkLabel="مشاهدهٔ مرکز چک"
              />
            ) : state.data.collateral === null ? (
              <ComingSoonWidget
                title="تعهد چک"
                reason="خلاصهٔ پشتوانه در دسترس نیست."
                icon="reports"
                href="/cheques"
                linkLabel="مشاهدهٔ مرکز چک"
              />
            ) : (
              <ChequeExposureWidget
                cheques={state.data.cheques}
                collateral={state.data.collateral}
                companyName="همهٔ شرکت‌ها"
                periodLabel="همهٔ دوره‌ها"
              />
            )}
            {state.data.agingFailed ? (
              <ComingSoonWidget
                title="چک‌های در جریان وصول"
                reason="دریافت گزارش کهنگی وصول ناموفق بود؛ از صفحهٔ گزارش‌های چک دوباره تلاش کنید."
                icon="reports"
                href="/cheques/reports?report=in-clearing"
                linkLabel="مشاهدهٔ گزارش وصول"
              />
            ) : (
              <ChequeInClearingWidget
                aging={state.data.aging}
                companyName="همهٔ شرکت‌ها"
                periodLabel="همهٔ دوره‌ها"
              />
            )}
            {state.data.chequesFailed ? (
              <ComingSoonWidget
                title="چک‌های نزدیک به سررسید"
                reason="دریافت داده‌های چک ناموفق بود؛ ممکن است کلید دسترسی خواندن چک‌ها را نداشته باشد."
                icon="reports"
                href="/cheques/reports?report=due-received"
                linkLabel="مشاهدهٔ گزارش سررسید"
              />
            ) : (
              <ChequeDueSoonWidget
                cheques={state.data.cheques}
                todayJalali={todayJalaliString()}
                companyName="همهٔ شرکت‌ها"
                periodLabel="همهٔ دوره‌ها"
              />
            )}
            <DraftsWidget summary={state.data.drafts} companyName={companyName} periodLabel={periodLabel} />
            <ReceivablesWidget summary={state.data.invoices} companyName={companyName} periodLabel={periodLabel} />
            <PayablesWidget summary={state.data.invoices} companyName={companyName} periodLabel={periodLabel} />
            <RecentInvoicesWidget
              invoices={state.data.recentInvoiceRows}
              companyName={companyName}
              periodLabel={periodLabel}
            />
            <RecentPaymentsWidget
              payments={state.data.recentPaymentRows}
              companyName={companyName}
              periodLabel={periodLabel}
            />
            {state.data.setupFailed ? (
              <ComingSoonWidget
                title="وضعیت راه‌اندازی"
                reason="وضعیت راه‌اندازی در دسترس نیست؛ از بخش سال‌های مالی دوباره تلاش کنید."
                icon="reports"
              />
            ) : (
              <SetupStatusWidget setup={state.data.setup} companyName={companyName} periodLabel={periodLabel} />
            )}
            <ComingSoonWidget
              title="موجودی نقد و بانک"
              reason="ماندهٔ حساب‌های نقدی و بانکی هنوز نقطهٔ پایانی در API ندارد؛ این کاشی پس از اضافه‌شدن گزارش مانده فعال می‌شود."
              icon="cash"
            />
            <ComingSoonWidget
              title="تراز آزمایشی و صورت‌های مالی"
              reason="تراز آزمایشی، سود و زیان و ترازنامه نیازمند نقطهٔ پایانی گزارش‌هاست و از فهرست‌های صفحه‌بندی‌شده محاسبه نمی‌شوند."
              icon="reports"
              href="/reports"
              linkLabel="مشاهدهٔ گزارش‌های زنده"
            />
            <ComingSoonWidget
              title="وضعیت مالیات و مودیان"
              reason="وضعیت انطباق مالیاتی و مودیان هنوز در API نیست؛ پس از اتصال ماژول انطباق اینجا نمایش داده می‌شود."
              icon="tax"
            />
          </div>
        </>
      )}
    </div>
  );
}
