/**
 * Reports page (US-305).
 *
 * Live views are computed ONLY from list endpoints the API actually
 * enumerates: invoice summaries (by state, by party) and payment
 * summaries (by state, by kind), each over the whole list
 * (fetchAllPages, 500-row cap stated when it bites). Every figure
 * states its company, period and unit (§11.3) and links to its source
 * rows — state rows link to the filtered list, party rows to the
 * filtered list by party name, recent rows to their detail page.
 *
 * The posted-move ledger, trial balance, balance sheet, P&L and cash
 * flow are honest coming-soon: the drafts endpoint returns draft
 * moves only and there is no journals-list or report endpoint
 * (get_report was deferred in Phase 2), so the UI never computes
 * them client-side from partial data and never shows fabricated
 * figures.
 *
 * Each summary table exports CSV with normalized values (§5.5 rule
 * 10); the print button prints the active tab (chrome and controls
 * are hidden in print via print:hidden + styles/globals.css).
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Download, Printer } from "lucide-react";
import { ApiError, api, type InvoiceResponse, type PaymentResponse } from "@/lib/api-client";
import {
  fetchAllPages,
  recentInvoices,
  recentPayments,
} from "@/features/dashboard/dashboard-data";
import { formatIsoDate } from "@/lib/format-date";
import { formatIsoJalali, todayIso } from "@/lib/jalali";
import { formatMoney } from "@/lib/format-money";
import { fa } from "@/lib/utils";
import {
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  DataTable,
  StatusBadge,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  type DataTableColumn,
} from "@/components/ui";
import { CompanyPicker } from "@/features/shared/company-picker";
import { csvFilename, downloadCsv } from "@/features/shared/csv";
import { isPermissionDenied, persianApiErrorMessage } from "@/features/shared/api-error";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import { useCompany } from "@/features/shared/use-company";
import { PAYMENT_KIND_LABELS, PAYMENT_STATE_LABELS } from "@/features/payments/labels";
import {
  invoicePeriod,
  paymentPeriod,
  sumInvoiceTotals,
  sumPaymentTotals,
  summarizeInvoicesByParty,
  summarizeInvoicesByState,
  summarizePaymentsByKind,
  summarizePaymentsByState,
  type InvoicePartyRow,
  type InvoiceStateRow,
  type PaymentKindRow,
  type PaymentStateRow,
  type ReportPeriod,
} from "./report-data";
import {
  invoicePartySummaryToCsv,
  invoiceStateSummaryToCsv,
  paymentKindSummaryToCsv,
  paymentStateSummaryToCsv,
} from "./report-csv";
import { CashBoxReportSection } from "@/features/cash-box/cash-box-report";
import { ReportComingSoon } from "./coming-soon";
import { ReconciliationStatusReport } from "./reconciliation-status";

const PAGE_SIZE = 100;
const RECENT_LIMIT = 5;

type TabId = "invoices" | "payments" | "ledger" | "statements" | "bank" | "cash-box";

/** Jalali period label for a report scope line. */
function periodLabel(period: ReportPeriod): string {
  if (!period.fromIso || !period.toIso) return "بدون داده";
  if (period.fromIso === period.toIso) return formatIsoJalali(period.fromIso);
  return `${formatIsoJalali(period.fromIso)} تا ${formatIsoJalali(period.toIso)}`;
}

/** §11.3 provenance line: company · period · unit · source rows. */
function ScopeLine({
  companyName,
  period,
  sourceLabel,
}: {
  companyName: string;
  period: ReportPeriod;
  sourceLabel: string;
}) {
  return (
    <p className="text-sm text-muted-foreground">
      شرکت: {companyName} · دوره: {periodLabel(period)} · واحد: تومان (مبالغ به ریال ذخیره می‌شوند) ·{" "}
      {sourceLabel}
    </p>
  );
}

/** Small CSV export button for a summary card (hidden in print). */
function ExportButton({ label, onExport, disabled }: { label: string; onExport: () => void; disabled?: boolean }) {
  return (
    <Button variant="outline" size="sm" onClick={onExport} disabled={disabled} className="print:hidden">
      <Download className="size-4" aria-hidden />
      {label}
    </Button>
  );
}

const INVOICE_STATE_COLUMNS: DataTableColumn<InvoiceStateRow>[] = [
  {
    key: "state",
    header: "وضعیت",
    render: (row) => (
      <Link href={`/invoices?state=${row.state}`} className="hover:underline">
        <StatusBadge status={row.state} />
      </Link>
    ),
  },
  { key: "count", header: "تعداد", render: (row) => fa(row.count) },
  {
    key: "untaxed",
    header: "بدون مالیات",
    render: (row) => <span className="tabular-nums">{formatMoney(row.untaxedIrr)}</span>,
    className: "text-end",
    headerClassName: "text-end",
  },
  {
    key: "tax",
    header: "مالیات",
    render: (row) => <span className="tabular-nums">{formatMoney(row.taxIrr)}</span>,
    className: "text-end",
    headerClassName: "text-end",
  },
  {
    key: "total",
    header: "مبلغ کل",
    render: (row) => <span className="tabular-nums">{formatMoney(row.totalIrr)}</span>,
    className: "text-end",
    headerClassName: "text-end",
  },
];

const INVOICE_PARTY_COLUMNS: DataTableColumn<InvoicePartyRow>[] = [
  {
    key: "party",
    header: "طرف حساب",
    render: (row) => (
      <Link href={`/invoices?q=${encodeURIComponent(row.partyName)}`} className="hover:underline">
        {row.partyName}
      </Link>
    ),
  },
  { key: "count", header: "تعداد فاکتور", render: (row) => fa(row.count) },
  {
    key: "total",
    header: "مبلغ کل",
    render: (row) => <span className="tabular-nums">{formatMoney(row.totalIrr)}</span>,
    className: "text-end",
    headerClassName: "text-end",
  },
];

const RECENT_INVOICE_COLUMNS: DataTableColumn<InvoiceResponse>[] = [
  {
    key: "number",
    header: "شماره",
    render: (inv) => (
      <span dir="ltr" className="ltr-isolate tabular-nums">
        {inv.number ?? `#${fa(inv.id)}`}
      </span>
    ),
  },
  { key: "party", header: "طرف حساب", render: (inv) => inv.party_name },
  { key: "date", header: "تاریخ", render: (inv) => formatIsoDate(inv.invoice_date) || "—" },
  { key: "state", header: "وضعیت", render: (inv) => <StatusBadge status={inv.state} /> },
  {
    key: "total",
    header: "مبلغ کل",
    render: (inv) => <span className="tabular-nums">{formatMoney(inv.total_amount)}</span>,
    className: "text-end",
    headerClassName: "text-end",
  },
];

const PAYMENT_KIND_COLUMNS: DataTableColumn<PaymentKindRow>[] = [
  {
    key: "kind",
    header: "نوع",
    render: (row) => (
      <Link href={`/payments?kind=${row.kind}`} className="hover:underline">
        {PAYMENT_KIND_LABELS[row.kind] ?? row.kind}
      </Link>
    ),
  },
  { key: "count", header: "تعداد", render: (row) => fa(row.count) },
  {
    key: "total",
    header: "مبلغ کل",
    render: (row) => <span className="tabular-nums">{formatMoney(row.totalIrr)}</span>,
    className: "text-end",
    headerClassName: "text-end",
  },
];

const PAYMENT_STATE_COLUMNS: DataTableColumn<PaymentStateRow>[] = [
  {
    key: "state",
    header: "وضعیت",
    render: (row) => {
      const info = PAYMENT_STATE_LABELS[row.state];
      return (
        <Link href={`/payments?state=${row.state}`} className="hover:underline">
          {info ? (
            <StatusBadge status={row.state} label={info.label} tone={info.tone} />
          ) : (
            <StatusBadge status={row.state} />
          )}
        </Link>
      );
    },
  },
  { key: "count", header: "تعداد", render: (row) => fa(row.count) },
  {
    key: "total",
    header: "مبلغ کل",
    render: (row) => <span className="tabular-nums">{formatMoney(row.totalIrr)}</span>,
    className: "text-end",
    headerClassName: "text-end",
  },
];

const RECENT_PAYMENT_COLUMNS: DataTableColumn<PaymentResponse>[] = [
  {
    key: "number",
    header: "شماره",
    render: (p) => (
      <span dir="ltr" className="ltr-isolate tabular-nums">
        {p.number}
      </span>
    ),
  },
  { key: "party", header: "طرف حساب", render: (p) => p.party_name },
  { key: "kind", header: "نوع", render: (p) => PAYMENT_KIND_LABELS[p.kind] ?? p.kind },
  { key: "date", header: "تاریخ", render: (p) => formatIsoDate(p.date) },
  {
    key: "state",
    header: "وضعیت",
    render: (p) => {
      const info = PAYMENT_STATE_LABELS[p.state];
      return info ? (
        <StatusBadge status={p.state} label={info.label} tone={info.tone} />
      ) : (
        <StatusBadge status={p.state} />
      );
    },
  },
  {
    key: "amount",
    header: "مبلغ",
    render: (p) => <span className="tabular-nums">{formatMoney(p.amount)}</span>,
    className: "text-end",
    headerClassName: "text-end",
  },
];

export function ReportsPage() {
  const router = useRouter();
  const { companies, companyId, setCompanyId, loading: companiesLoading, error: companiesError } = useCompany();
  const [tab, setTab] = React.useState<TabId>("invoices");
  const [invoices, setInvoices] = React.useState<InvoiceResponse[] | null>(null);
  const [payments, setPayments] = React.useState<PaymentResponse[] | null>(null);
  const [truncated, setTruncated] = React.useState(false);
  const [error, setError] = React.useState<unknown>(null);
  const [reloadKey, setReloadKey] = React.useState(0);

  React.useEffect(() => {
    if (!companyId) return;
    let cancelled = false;
    Promise.all([
      fetchAllPages((cursor) => api.listInvoices({ company: companyId, limit: PAGE_SIZE, cursor })),
      fetchAllPages((cursor) => api.listPayments({ company: companyId, limit: PAGE_SIZE, cursor })),
    ])
      .then(([invoiceResult, paymentResult]) => {
        if (cancelled) return;
        setInvoices(invoiceResult.items);
        setPayments(paymentResult.items);
        setTruncated(invoiceResult.truncated || paymentResult.truncated);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.replace("/login");
          return;
        }
        setError(err);
      });
    return () => {
      cancelled = true;
    };
  }, [companyId, reloadKey, router]);

  const loading = invoices === null || payments === null;
  const companyName = companies.find((c) => c.id === companyId)?.name ?? "";

  const invoiceByState = React.useMemo(() => summarizeInvoicesByState(invoices ?? []), [invoices]);
  const invoiceByParty = React.useMemo(() => summarizeInvoicesByParty(invoices ?? []), [invoices]);
  const paymentByState = React.useMemo(() => summarizePaymentsByState(payments ?? []), [payments]);
  const paymentByKind = React.useMemo(() => summarizePaymentsByKind(payments ?? []), [payments]);
  const invoiceRange = React.useMemo(() => invoicePeriod(invoices ?? []), [invoices]);
  const paymentRange = React.useMemo(() => paymentPeriod(payments ?? []), [payments]);
  const recentInvoiceRows = React.useMemo(
    () => recentInvoices(invoices ?? [], RECENT_LIMIT),
    [invoices],
  );
  const recentPaymentRows = React.useMemo(
    () => recentPayments(payments ?? [], RECENT_LIMIT),
    [payments],
  );

  const resetData = React.useCallback(() => {
    setError(null);
    setInvoices(null);
    setPayments(null);
    setTruncated(false);
  }, []);

  if (companiesLoading) {
    return (
      <div className="space-y-6" aria-label="در حال بارگذاری">
        <div className="h-8 w-40 animate-pulse rounded-lg bg-muted" />
        <div className="h-64 animate-pulse rounded-lg bg-muted" />
      </div>
    );
  }

  if (companiesError) {
    return (
      <ErrorState
        message={persianApiErrorMessage(companiesError, "دریافت شرکت‌ها ناموفق بود.")}
        onRetry={() => setReloadKey((k) => k + 1)}
      />
    );
  }

  if (companies.length === 0) {
    return <p className="text-sm text-muted-foreground">هیچ شرکتی در دسترس نیست.</p>;
  }

  if (error && isPermissionDenied(error)) {
    return <PermissionDeniedState message={persianApiErrorMessage(error, "دریافت گزارش‌ها ناموفق بود.")} />;
  }

  if (error) {
    return (
      <ErrorState
        message={persianApiErrorMessage(error, "دریافت گزارش‌ها ناموفق بود.")}
        onRetry={() => {
          resetData();
          setReloadKey((k) => k + 1);
        }}
      />
    );
  }

  return (
    <div className="space-y-6" data-testid="reports-page">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">گزارش‌ها</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            خلاصه‌های زنده از فاکتورها و پرداخت‌ها؛ صورت‌های مالی پس از اضافه‌شدن نقطهٔ پایانی گزارش در
            بک‌اند فعال می‌شوند.
          </p>
        </div>
        <Button variant="outline" onClick={() => window.print()} className="print:hidden">
          <Printer className="size-4" aria-hidden />
          چاپ گزارش
        </Button>
      </div>

      <div className="print:hidden">
        <CompanyPicker
          companies={companies}
          companyId={companyId}
          onChange={(id) => {
            setCompanyId(id);
            resetData();
          }}
        />
      </div>

      {truncated ? (
        <p
          className="rounded-lg border border-warning/40 bg-warning/10 px-4 py-3 text-sm text-warning-foreground"
          role="note"
        >
          فهرست فاکتورها یا پرداخت‌ها بسیار بزرگ است؛ خلاصه‌ها بر اساس ۵۰۰ رکورد اول محاسبه شده‌اند.
        </p>
      ) : null}

      <Tabs value={tab} onValueChange={(value) => setTab(value as TabId)}>
        <TabsList aria-label="بخش‌های گزارش" className="print:hidden">
          <TabsTrigger value="invoices">فاکتورها</TabsTrigger>
          <TabsTrigger value="payments">پرداخت‌ها</TabsTrigger>
          <TabsTrigger value="ledger">دفتر روزنامه</TabsTrigger>
          <TabsTrigger value="statements">صورت‌های مالی</TabsTrigger>
          <TabsTrigger value="bank">تطبیق بانکی</TabsTrigger>
          <TabsTrigger value="cash-box">صندوق</TabsTrigger>
        </TabsList>

        <TabsContent value="invoices" className="space-y-6">
          <ScopeLine
            companyName={companyName}
            period={invoiceRange}
            sourceLabel={`مبتنی بر ${fa(invoices?.length ?? 0)} فاکتور · جمع کل ${formatMoney(sumInvoiceTotals(invoices ?? []))}`}
          />
          <Card>
            <CardHeader className="flex flex-wrap items-center justify-between gap-4">
              <CardTitle>فاکتورها بر اساس وضعیت</CardTitle>
              <ExportButton
                label="خروجی CSV"
                disabled={invoiceByState.length === 0}
                onExport={() =>
                  downloadCsv(csvFilename("report-invoices-by-state", todayIso()), invoiceStateSummaryToCsv(invoiceByState))
                }
              />
            </CardHeader>
            <CardContent>
              <DataTable
                columns={INVOICE_STATE_COLUMNS}
                rows={invoiceByState}
                rowKey={(row) => row.state}
                loading={loading}
                emptyText="هنوز فاکتوری ثبت نشده است."
                caption="جمع هر وضعیت، مجموع فاکتورهای همان وضعیت است؛ یادداشت‌های اعتباری کسر نشده‌اند. برای دیدن فاکتورهای هر وضعیت، روی آن کلیک کنید."
              />
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="flex flex-wrap items-center justify-between gap-4">
              <CardTitle>فاکتورها بر اساس طرف حساب</CardTitle>
              <ExportButton
                label="خروجی CSV"
                disabled={invoiceByParty.length === 0}
                onExport={() =>
                  downloadCsv(csvFilename("report-invoices-by-party", todayIso()), invoicePartySummaryToCsv(invoiceByParty))
                }
              />
            </CardHeader>
            <CardContent>
              <DataTable
                columns={INVOICE_PARTY_COLUMNS}
                rows={invoiceByParty}
                rowKey={(row) => row.partyId}
                loading={loading}
                emptyText="هنوز فاکتوری ثبت نشده است."
                caption="برای دیدن فاکتورهای هر طرف حساب، روی نام آن کلیک کنید."
              />
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>آخرین فاکتورها</CardTitle>
            </CardHeader>
            <CardContent>
              <DataTable
                columns={RECENT_INVOICE_COLUMNS}
                rows={recentInvoiceRows}
                rowKey={(inv) => inv.id}
                loading={loading}
                emptyText="هنوز فاکتوری ثبت نشده است."
                onRowClick={(inv) => router.push(`/invoices/${inv.id}`)}
                caption="برای جزئیات هر فاکتور، روی ردیف آن کلیک کنید."
              />
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="payments" className="space-y-6">
          <ScopeLine
            companyName={companyName}
            period={paymentRange}
            sourceLabel={`مبتنی بر ${fa(payments?.length ?? 0)} پرداخت · جمع کل ${formatMoney(sumPaymentTotals(payments ?? []))}`}
          />
          <Card>
            <CardHeader className="flex flex-wrap items-center justify-between gap-4">
              <CardTitle>پرداخت‌ها بر اساس نوع</CardTitle>
              <ExportButton
                label="خروجی CSV"
                disabled={paymentByKind.length === 0}
                onExport={() =>
                  downloadCsv(csvFilename("report-payments-by-kind", todayIso()), paymentKindSummaryToCsv(paymentByKind))
                }
              />
            </CardHeader>
            <CardContent>
              <DataTable
                columns={PAYMENT_KIND_COLUMNS}
                rows={paymentByKind}
                rowKey={(row) => row.kind}
                loading={loading}
                emptyText="هنوز پرداختی ثبت نشده است."
                caption="دریافتی پول ورودی و پرداختی پول خروجی است. برای دیدن پرداخت‌های هر نوع، روی آن کلیک کنید."
              />
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="flex flex-wrap items-center justify-between gap-4">
              <CardTitle>پرداخت‌ها بر اساس وضعیت</CardTitle>
              <ExportButton
                label="خروجی CSV"
                disabled={paymentByState.length === 0}
                onExport={() =>
                  downloadCsv(csvFilename("report-payments-by-state", todayIso()), paymentStateSummaryToCsv(paymentByState))
                }
              />
            </CardHeader>
            <CardContent>
              <DataTable
                columns={PAYMENT_STATE_COLUMNS}
                rows={paymentByState}
                rowKey={(row) => row.state}
                loading={loading}
                emptyText="هنوز پرداختی ثبت نشده است."
                caption="برای دیدن پرداخت‌های هر وضعیت، روی آن کلیک کنید."
              />
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>آخرین پرداخت‌ها</CardTitle>
            </CardHeader>
            <CardContent>
              <DataTable
                columns={RECENT_PAYMENT_COLUMNS}
                rows={recentPaymentRows}
                rowKey={(p) => p.id}
                loading={loading}
                emptyText="هنوز پرداختی ثبت نشده است."
                onRowClick={(p) => router.push(`/payments/${p.id}`)}
                caption="برای جزئیات هر پرداخت، روی ردیف آن کلیک کنید."
              />
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="ledger" className="space-y-6">
          <ReportComingSoon
            title="دفتر روزنامهٔ ثبت‌شده‌ها"
            reason="فهرست سندهای ثبت‌شده هنوز در API نیست؛ نقطهٔ پایانی دفتر روزنامه فقط پیش‌نویس‌ها را برمی‌گرداند و سند ثبت‌شده را نمی‌توان فهرست کرد. تا وقتی نقطهٔ پایانی فهرست سندهای ثبت‌شده در بک‌اند اضافه نشود، این دفتر خالی می‌ماند تا عدد ناقص نشان داده نشود."
            testId="report-coming-soon-ledger"
          >
            <p className="text-sm">
              <Link href="/accounting/journals" className="font-medium text-primary hover:underline">
                مشاهدهٔ پیش‌نویس‌های سند در دفتر روزنامه
              </Link>
            </p>
          </ReportComingSoon>
        </TabsContent>

        <TabsContent value="statements" className="space-y-6">
          <div className="grid gap-4 md:grid-cols-2">
            <ReportComingSoon
              title="تراز آزمایشی"
              reason="تراز آزمایشی نیازمند نقطهٔ پایانی گزارش در بک‌اند است؛ ماندهٔ همهٔ حساب‌ها از فهرست‌های صفحه‌بندی‌شده محاسبه نمی‌شود تا عدد ناقص نشان داده نشود."
              testId="report-coming-soon-trial-balance"
            />
            <ReportComingSoon
              title="ترازنامه"
              reason="ترازنامه نیازمند نقطهٔ پایانی گزارش در بک‌اند است و بدون دادهٔ کامل ماندهٔ حساب‌ها نمی‌توان آن را درست محاسبه کرد."
              testId="report-coming-soon-balance-sheet"
            />
            <ReportComingSoon
              title="صورت سود و زیان"
              reason="صورت سود و زیان نیازمند نقطهٔ پایانی گزارش در بک‌اند است؛ درآمد و هزینهٔ دوره از فهرست‌های صفحه‌بندی‌شده محاسبه نمی‌شود."
              testId="report-coming-soon-pl"
            />
            <ReportComingSoon
              title="صورت جریان وجوه نقد"
              reason="صورت جریان وجوه نقد نیازمند نقطهٔ پایانی گزارش و دادهٔ کامل حرکت‌های نقدی است و هنوز در API نیست."
              testId="report-coming-soon-cash-flow"
            />
          </div>
        </TabsContent>

        <TabsContent value="bank" className="space-y-6">
          <ReconciliationStatusReport companyId={companyId} />
        </TabsContent>
        <TabsContent value="cash-box" className="space-y-6">
          <CashBoxReportSection companyId={companyId} companyName={companyName} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
