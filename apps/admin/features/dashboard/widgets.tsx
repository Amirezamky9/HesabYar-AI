"use client";

/**
 * Dashboard widgets (US-302) — presentational only; the dashboard home
 * fetches live data and passes it in. Every live widget states its
 * period, company and unit (spec §11.3) and links to the underlying
 * filtered page. Widgets without an API source are honest coming-soon
 * tiles — they never show a number.
 */

import * as React from "react";
import Link from "next/link";
import {
  ArrowLeft,
  Banknote,
  CalendarClock,
  CheckCircle2,
  CircleAlert,
  CircleDollarSign,
  FileUp,
  HandCoins,
  Hourglass,
  Inbox,
  Landmark,
  NotebookPen,
  Scale,
  ShieldCheck,
  TicketCheck,
  TrendingUp,
  TriangleAlert,
  Vault,
  Wallet,
  type LucideIcon,
} from "lucide-react";
import type {
  ChequeResponse,
  ClearingAgingResponse,
  CollateralSummaryResponse,
  InvoiceResponse,
  PaymentResponse,
  ReviewQueueSummaryResponse,
  SetupStatusResponse,
} from "@/lib/api-client";
import { faNumber } from "@/lib/utils";
import { formatIsoDate } from "@/lib/format-date";
import { formatJalaliShort, sumIrr } from "@/features/cheques/cheque-data";
import { importStatusLabel, importStatusTone } from "@/features/settlement/settlement-data";
import {
  Badge,
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
  MoneyDisplay,
  Skeleton,
  StatusBadge,
} from "@/components/ui";
import {
  statementBatchStatusLabel,
  statementBatchStatusTone,
  summarizeReceivedDueSoon,
  type CashboxSummary,
  type ChequeTotal,
  type DashboardAlert,
  type DashboardAlertKind,
  type DraftSummary,
  type DueSoonSummary,
  type ImportBatchRow,
  type InvoiceSummary,
  type TodayMoneySummary,
  type TrancheSummary,
} from "./dashboard-data";
import {
  formatAgeHours,
  reviewTypeLabel,
} from "@/features/review-queue/review-queue-data";

/** «شرکت: … · دوره: … · واحد: تومان» — the §11.3 provenance line. */
export function WidgetMeta({
  companyName,
  periodLabel,
  unit = "تومان",
}: {
  companyName: string;
  periodLabel: string;
  unit?: string;
}) {
  return (
    <p className="text-xs text-muted-foreground">
      شرکت: {companyName} · دوره: {periodLabel} · واحد: {unit}
    </p>
  );
}

function WidgetCard({
  icon: Icon,
  title,
  meta,
  href,
  hrefLabel,
  testId,
  children,
}: {
  icon: LucideIcon;
  title: string;
  meta: React.ReactNode;
  href: string;
  hrefLabel: string;
  testId: string;
  children: React.ReactNode;
}) {
  return (
    <Card className="min-h-44" data-testid={testId}>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Icon className="size-5 text-brand" strokeWidth={1.5} aria-hidden />
          <CardTitle>{title}</CardTitle>
        </div>
        <CardDescription>{meta}</CardDescription>
      </CardHeader>
      <CardContent className="flex-1">{children}</CardContent>
      <CardFooter>
        <Link
          href={href}
          className="inline-flex min-h-11 items-center gap-1 text-sm font-medium text-brand hover:underline"
        >
          {hrefLabel}
          <ArrowLeft className="size-4" strokeWidth={2} aria-hidden />
        </Link>
      </CardFooter>
    </Card>
  );
}

/** Skeleton shaped like a widget card (zero layout shift while loading). */
export function WidgetSkeleton() {
  return (
    <Card className="min-h-44" aria-label="در حال بارگذاری">
      <CardHeader>
        <Skeleton className="h-5 w-32" />
        <Skeleton className="mt-2 h-3 w-48" />
      </CardHeader>
      <CardContent>
        <Skeleton className="h-8 w-24" />
        <Skeleton className="mt-2 h-4 w-40" />
      </CardContent>
    </Card>
  );
}

interface LiveWidgetProps {
  companyName: string;
  periodLabel: string;
}

/**
 * Review queue widget (US-504) — §7.3 «نیازمند توجه»: per-type
 * counters, the oldest waiting item, overdue counts and the
 * critical-overdue alert. Company-scoped like every other dashboard
 * widget; items filed without a company still appear on the queue
 * page (tenant-wide).
 */
export function ReviewQueueWidget({
  summary,
  companyName,
  periodLabel,
}: LiveWidgetProps & { summary: ReviewQueueSummaryResponse }) {
  const waiting = summary.total_open + summary.total_in_review;
  const nonEmpty = summary.counters.filter((counter) => counter.open_count + counter.in_review_count > 0);
  return (
    <WidgetCard
      icon={Inbox}
      testId="widget-review-queue"
      title="صف بازبینی"
      meta={<WidgetMeta companyName={companyName} periodLabel={periodLabel} unit="آیتم" />}
      href="/review-queue"
      hrefLabel="مشاهدهٔ صف بازبینی"
    >
      {waiting === 0 ? (
        <p className="text-sm text-muted-foreground">آیتم در انتظار بررسی نیست؛ صف خالی است.</p>
      ) : (
        <div className="space-y-2">
          <p className="text-2xl font-bold tabular-nums">{faNumber(waiting)} آیتم در انتظار بررسی</p>
          <ul className="space-y-1 text-sm text-muted-foreground">
            {nonEmpty.map((counter) => (
              <li key={counter.item_type} className="flex justify-between gap-2">
                <span>{reviewTypeLabel(counter.item_type)}</span>
                <span>{faNumber(counter.open_count + counter.in_review_count)}</span>
              </li>
            ))}
          </ul>
          {summary.total_overdue > 0 ? (
            <p className="text-sm text-warning-foreground">
              {faNumber(summary.total_overdue)} آیتم گذشته از مهلت
              {summary.oldest_open_age_hours !== null
                ? ` · قدیمی‌ترین: ${formatAgeHours(summary.oldest_open_age_hours)}`
                : ""}
            </p>
          ) : null}
          {summary.critical_overdue_count > 0 ? (
            <p className="text-sm font-medium text-destructive" role="alert">
              {faNumber(summary.critical_overdue_count)} آیتم بحرانی گذشته از مهلت — نیازمند توجه مدیر
            </p>
          ) : null}
        </div>
      )}
    </WidgetCard>
  );
}

export function DraftsWidget({
  summary,
  companyName,
  periodLabel,
}: LiveWidgetProps & { summary: DraftSummary }) {
  return (
    <WidgetCard
      icon={NotebookPen}
      testId="widget-drafts"
      title="پیش‌نویس‌های ثبت‌نشده"
      meta={<WidgetMeta companyName={companyName} periodLabel={periodLabel} />}
      href="/accounting/journals"
      hrefLabel="مشاهدهٔ روزنامه‌ها"
    >
      {summary.count === 0 ? (
        <p className="text-sm text-muted-foreground">پیش‌نویس ثبت‌نشده‌ای نیست؛ همهٔ اسناد ثبت شده‌اند.</p>
      ) : (
        <div className="space-y-2">
          <p className="text-2xl font-bold tabular-nums">{faNumber(summary.count)} پیش‌نویس</p>
          <p className="text-sm text-muted-foreground">
            مجموع بدهکار: <MoneyDisplay amountIrr={summary.totalDebitIrr} size="sm" />
          </p>
        </div>
      )}
    </WidgetCard>
  );
}

export function ReceivablesWidget({
  summary,
  companyName,
  periodLabel,
}: LiveWidgetProps & { summary: InvoiceSummary }) {
  return (
    <WidgetCard
      icon={HandCoins}
      testId="widget-receivables"
      title="مطالبات"
      meta={<WidgetMeta companyName={companyName} periodLabel={periodLabel} />}
      href="/invoices?type=out"
      hrefLabel="مشاهدهٔ صورت‌حساب‌های فروش"
    >
      {summary.receivableCount === 0 ? (
        <p className="text-sm text-muted-foreground">صورت‌حساب فروش باز و پرداخت‌نشده‌ای نیست.</p>
      ) : (
        <div className="space-y-2">
          <MoneyDisplay amountIrr={summary.receivableTotalIrr} size="md" />
          <p className="text-sm text-muted-foreground">
            {faNumber(summary.receivableCount)} صورت‌حساب باز (تأییدشده یا ثبت‌شده)
          </p>
        </div>
      )}
    </WidgetCard>
  );
}

export function PayablesWidget({
  summary,
  companyName,
  periodLabel,
}: LiveWidgetProps & { summary: InvoiceSummary }) {
  return (
    <WidgetCard
      icon={Wallet}
      testId="widget-payables"
      title="بدهی‌ها"
      meta={<WidgetMeta companyName={companyName} periodLabel={periodLabel} />}
      href="/invoices?type=in"
      hrefLabel="مشاهدهٔ صورت‌حساب‌های خرید"
    >
      {summary.payableCount === 0 ? (
        <p className="text-sm text-muted-foreground">صورت‌حساب خرید باز و پرداخت‌نشده‌ای نیست.</p>
      ) : (
        <div className="space-y-2">
          <MoneyDisplay amountIrr={summary.payableTotalIrr} size="md" />
          <p className="text-sm text-muted-foreground">
            {faNumber(summary.payableCount)} صورت‌حساب باز (تأییدشده یا ثبت‌شده)
          </p>
        </div>
      )}
    </WidgetCard>
  );
}

export function RecentInvoicesWidget({
  invoices,
  companyName,
  periodLabel,
}: LiveWidgetProps & { invoices: InvoiceResponse[] }) {
  return (
    <WidgetCard
      icon={Banknote}
      testId="widget-recent-invoices"
      title="صورت‌حساب‌های اخیر"
      meta={<WidgetMeta companyName={companyName} periodLabel={periodLabel} />}
      href="/invoices"
      hrefLabel="مشاهدهٔ همهٔ صورت‌حساب‌ها"
    >
      {invoices.length === 0 ? (
        <p className="text-sm text-muted-foreground">هنوز صورت‌حسابی ثبت نشده است.</p>
      ) : (
        <ul className="space-y-3">
          {invoices.map((invoice) => (
            <li key={invoice.id} className="flex items-center justify-between gap-4 text-sm">
              <div className="min-w-0">
                <p className="truncate font-medium">{invoice.party_name}</p>
                <p className="text-xs text-muted-foreground">
                  {invoice.number ? (
                    <span className="ltr-isolate">#{invoice.number}</span>
                  ) : (
                    "بدون شماره"
                  )}
                  {invoice.invoice_date ? ` · ${formatIsoDate(invoice.invoice_date)}` : ""}
                </p>
              </div>
              <div className="flex shrink-0 flex-col items-end gap-1">
                <MoneyDisplay amountIrr={invoice.total_amount} size="sm" />
                <StatusBadge status={invoice.state} />
              </div>
            </li>
          ))}
        </ul>
      )}
    </WidgetCard>
  );
}

export function RecentPaymentsWidget({
  payments,
  companyName,
  periodLabel,
}: LiveWidgetProps & { payments: PaymentResponse[] }) {
  return (
    <WidgetCard
      icon={Landmark}
      testId="widget-recent-payments"
      title="پرداخت‌های اخیر"
      meta={<WidgetMeta companyName={companyName} periodLabel={periodLabel} />}
      href="/payments"
      hrefLabel="مشاهدهٔ همهٔ پرداخت‌ها"
    >
      {payments.length === 0 ? (
        <p className="text-sm text-muted-foreground">هنوز پرداختی ثبت نشده است.</p>
      ) : (
        <ul className="space-y-3">
          {payments.map((payment) => (
            <li key={payment.id} className="flex items-center justify-between gap-4 text-sm">
              <div className="min-w-0">
                <p className="truncate font-medium">{payment.party_name}</p>
                <p className="text-xs text-muted-foreground">
                  <span className="ltr-isolate">#{payment.number}</span>
                  {` · ${formatIsoDate(payment.date)}`}
                </p>
              </div>
              <div className="flex shrink-0 flex-col items-end gap-1">
                <MoneyDisplay amountIrr={payment.amount} size="sm" />
                <StatusBadge status={payment.state} />
              </div>
            </li>
          ))}
        </ul>
      )}
    </WidgetCard>
  );
}

export function SetupStatusWidget({
  setup,
  companyName,
  periodLabel,
}: LiveWidgetProps & { setup: SetupStatusResponse | null }) {
  return (
    <WidgetCard
      icon={CheckCircle2}
      testId="widget-setup"
      title="وضعیت راه‌اندازی"
      meta={<WidgetMeta companyName={companyName} periodLabel={periodLabel} unit="—" />}
      href="/accounting/fiscal-years"
      hrefLabel="مشاهدهٔ سال‌های مالی و وضعیت"
    >
      {setup === null ? (
        <p className="text-sm text-muted-foreground">وضعیت راه‌اندازی در دسترس نیست.</p>
      ) : (
        <dl className="space-y-2 text-sm">
          <div className="flex items-center justify-between gap-4">
            <dt className="text-muted-foreground">نمودار حساب‌ها</dt>
            <dd className="font-medium">
              {setup.chart_installed
                ? `${faNumber(setup.account_count)} حساب نصب‌شده`
                : "نصب نشده است"}
            </dd>
          </div>
          <div className="flex items-center justify-between gap-4">
            <dt className="text-muted-foreground">سال مالی باز</dt>
            <dd className="font-medium">
              {setup.open_fiscal_year ? (
                <span className="ltr-isolate">{setup.open_fiscal_year.name}</span>
              ) : (
                "ندارد"
              )}
            </dd>
          </div>
          <div className="flex items-center justify-between gap-4">
            <dt className="text-muted-foreground">دوره‌های باز</dt>
            <dd className="font-medium">{faNumber(setup.open_period_count)} دوره</dd>
          </div>
          <div className="flex items-center justify-between gap-4">
            <dt className="text-muted-foreground">آمادگی کلی</dt>
            <dd>
              <StatusBadge status={setup.ready ? "ready" : "not_ready"} />
            </dd>
          </div>
        </dl>
      )}
    </WidgetCard>
  );
}

/**
 * Cheque exposure widget (US-535; §7.3) — «چقدر چک دادیم» (issued
 * commercial cheques, report 2) and «چقدر چک پشتوانه کردیم»
 * (collateral, report 6), kept strictly apart. US-551 scopes the
 * figures to the active company at the backend (the list +
 * collateral-summary company filter); the meta line names it.
 */
export function ChequeExposureWidget({
  cheques,
  collateral,
  companyName,
  periodLabel,
}: LiveWidgetProps & { cheques: ChequeResponse[]; collateral: CollateralSummaryResponse }) {
  const issued = cheques.filter((c) => c.kind === "issued" && c.purpose === "payment" && c.state === "issued");
  const collateralCount = collateral.cheques.length;
  return (
    <WidgetCard
      icon={TicketCheck}
      testId="widget-cheque-exposure"
      title="تعهد چک"
      meta={<WidgetMeta companyName={companyName} periodLabel={periodLabel} />}
      href="/cheques"
      hrefLabel="مشاهدهٔ مرکز چک"
    >
      <dl className="space-y-3 text-sm">
        <div className="flex items-center justify-between gap-4">
          <dt className="text-muted-foreground">چک‌های پرداختی صادرشده</dt>
          <dd>
            <MoneyDisplay amountIrr={sumIrr(issued)} size="sm" />
            <span className="text-xs text-muted-foreground"> · {faNumber(issued.length)} چک</span>
          </dd>
        </div>
        <div className="flex items-center justify-between gap-4">
          <dt className="text-muted-foreground">چک‌های پشتوانه (انتظامی)</dt>
          <dd>
            <MoneyDisplay amountIrr={collateral.received_total_irr + collateral.issued_total_irr} size="sm" />
            <span className="text-xs text-muted-foreground"> · {faNumber(collateralCount)} چک</span>
          </dd>
        </div>
        <p className="text-xs text-muted-foreground">
          پشتوانه خارج از ترازنامه است و با تعهد تجاری جمع نمی‌شود.
        </p>
      </dl>
    </WidgetCard>
  );
}

/** In-clearing aging widget (US-535; §7.3) — from the aging endpoint. */
export function ChequeInClearingWidget({
  aging,
  companyName,
  periodLabel,
}: LiveWidgetProps & { aging: ClearingAgingResponse[] }) {
  const stale = aging.filter((a) => a.stale);
  const oldest = aging.reduce((max, a) => Math.max(max, a.days_in_clearing), 0);
  return (
    <WidgetCard
      icon={Hourglass}
      testId="widget-cheque-in-clearing"
      title="چک‌های در جریان وصول"
      meta={<WidgetMeta companyName={companyName} periodLabel={periodLabel} unit="چک" />}
      href="/cheques/reports?report=in-clearing"
      hrefLabel="مشاهدهٔ گزارش وصول"
    >
      {aging.length === 0 ? (
        <p className="text-sm text-muted-foreground">چکی در جریان وصول نیست.</p>
      ) : (
        <div className="space-y-2">
          <p className="text-2xl font-bold tabular-nums">{faNumber(aging.length)} چک در وصول</p>
          <p className="text-sm text-muted-foreground">
            قدیمی‌ترین: {faNumber(oldest)} روز در وصول
          </p>
          {stale.length > 0 ? (
            <p className="text-sm font-medium text-warning-foreground" role="alert">
              {faNumber(stale.length)} چک کهنه — نیازمند پیگیری (هرگز خودکار برگشتی نمی‌شود)
            </p>
          ) : null}
        </div>
      )}
    </WidgetCard>
  );
}

/** Due-soon widget (US-535; §7.3) — received commercial cheques due
 * within 7 days (or overdue), from the registry rows. The filter
 * lives in dashboard-data (`summarizeReceivedDueSoon`) so the
 * alerts section counts the same rows. */
export function ChequeDueSoonWidget({
  cheques,
  todayJalali,
  companyName,
  periodLabel,
}: LiveWidgetProps & { cheques: ChequeResponse[]; todayJalali: string }) {
  const dueSoon = summarizeReceivedDueSoon(cheques, todayJalali);
  return (
    <WidgetCard
      icon={CalendarClock}
      testId="widget-cheque-due-soon"
      title="چک‌های نزدیک به سررسید"
      meta={<WidgetMeta companyName={companyName} periodLabel={periodLabel} />}
      href="/cheques/reports?report=due-received"
      hrefLabel="مشاهدهٔ گزارش سررسید"
    >
      {dueSoon.count === 0 ? (
        <p className="text-sm text-muted-foreground">در ۷ روز آینده (و سررسیدگذشته) چک دریافتی بازی نیست.</p>
      ) : (
        <div className="space-y-2">
          <MoneyDisplay amountIrr={dueSoon.totalIrr} size="md" />
          <p className="text-sm text-muted-foreground">
            {faNumber(dueSoon.count)} چک · نزدیک‌ترین سررسید:{" "}
            {dueSoon.nearestDueJalali ? formatJalaliShort(dueSoon.nearestDueJalali) : "—"}
          </p>
        </div>
      )}
    </WidgetCard>
  );
}

/** Honest coming-soon tile: no number, no fake KPI — just the reason.
 * `href` links the tile to the closest live view (e.g. /reports). */
export function ComingSoonWidget({
  title,
  reason,
  icon,
  href,
  linkLabel,
}: {
  title: string;
  reason: string;
  icon: "cash" | "reports" | "tax";
  href?: string;
  linkLabel?: string;
}) {
  const icons = { cash: Banknote, reports: Scale, tax: ShieldCheck } as const;
  const Icon = icons[icon];
  return (
    <Card className="min-h-44" data-testid={`widget-coming-soon-${icon}`}>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Icon className="size-5 text-muted-foreground" strokeWidth={1.5} aria-hidden />
          <CardTitle>{title}</CardTitle>
          <Badge variant="outline">به‌زودی</Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="flex items-start gap-2 text-sm text-muted-foreground">
          <CircleAlert className="mt-1 size-4 shrink-0" strokeWidth={1.5} aria-hidden />
          {reason}
        </p>
        {href ? (
          <p className="text-sm">
            <Link href={href} className="font-medium text-primary hover:underline">
              {linkLabel ?? "مشاهدهٔ گزارش‌ها"}
            </Link>
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// US-551 — the six unified-dashboard sections (§7.3)
// ---------------------------------------------------------------------------

/** One dashboard section: a labelled heading + a widget grid. */
export function DashboardSection({
  id,
  title,
  description,
  testId,
  children,
}: {
  id: string;
  title: string;
  description?: string;
  testId: string;
  children: React.ReactNode;
}) {
  return (
    <section aria-labelledby={id} data-testid={testId} className="space-y-4">
      <div>
        <h2 id={id} className="text-lg font-bold">
          {title}
        </h2>
        {description ? <p className="mt-1 text-sm text-muted-foreground">{description}</p> : null}
      </div>
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">{children}</div>
    </section>
  );
}

/** One KPI row that links to its filtered operational page. */
function KpiRow({
  label,
  amountIrr,
  countLabel,
  href,
}: {
  label: string;
  amountIrr: number;
  countLabel: string;
  href: string;
}) {
  return (
    <Link
      href={href}
      className="flex min-h-11 items-center justify-between gap-4 rounded-lg px-2 py-1 hover:bg-muted/50"
    >
      <span className="text-sm text-muted-foreground">
        {label}
        <span className="block text-xs">{countLabel}</span>
      </span>
      <MoneyDisplay amountIrr={amountIrr} size="sm" />
    </Link>
  );
}

/**
 * پولِ امروز — today's succeeded receipts + payments, grouped by
 * payment journal (the channel). Only `succeeded` payments count:
 * a draft or failed payment is not money that moved.
 */
export function TodayMoneyWidget({
  summary,
  todayLabel,
  companyName,
  periodLabel,
}: LiveWidgetProps & { summary: TodayMoneySummary; todayLabel: string }) {
  const empty = summary.receiptCount === 0 && summary.paymentCount === 0;
  return (
    <WidgetCard
      icon={CircleDollarSign}
      testId="widget-today-money"
      title={`پولِ امروز · ${todayLabel}`}
      meta={<WidgetMeta companyName={companyName} periodLabel={periodLabel} />}
      href="/payments?state=succeeded"
      hrefLabel="مشاهدهٔ پرداخت‌ها"
    >
      {empty ? (
        <p className="text-sm text-muted-foreground">امروز دریافتی یا پرداختی موفقی ثبت نشده است.</p>
      ) : (
        <div className="space-y-3">
          <div>
            <p className="text-sm text-muted-foreground">دریافتی امروز</p>
            <MoneyDisplay amountIrr={summary.receiptTotalIrr} size="md" />
            <p className="text-sm text-muted-foreground">{faNumber(summary.receiptCount)} پرداخت موفق</p>
          </div>
          <div>
            <p className="text-sm text-muted-foreground">پرداختی امروز</p>
            <MoneyDisplay amountIrr={summary.paymentTotalIrr} size="md" />
            <p className="text-sm text-muted-foreground">{faNumber(summary.paymentCount)} پرداخت موفق</p>
          </div>
          {summary.receiptsByJournal.length > 0 ? (
            <ul className="space-y-1 border-t border-border pt-2 text-sm">
              {summary.receiptsByJournal.slice(0, 3).map((row) => (
                <li key={row.journal} className="flex justify-between gap-2">
                  <span className="text-muted-foreground">{row.journal || "بدون دفتر"} · دریافتی</span>
                  <MoneyDisplay amountIrr={row.totalIrr} size="sm" />
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      )}
    </WidgetCard>
  );
}

/** A KPI row whose source failed: no number, just the honest gap. */
function UnavailableRow({ label }: { label: string }) {
  return (
    <div className="flex min-h-11 items-center justify-between gap-4 rounded-lg px-2 py-1">
      <span className="text-sm text-muted-foreground">{label}</span>
      <span className="text-sm text-muted-foreground">در دسترس نیست</span>
    </div>
  );
}

/**
 * پولِ در راه — gateway receivables, this month's expected
 * settlement tranches, in-clearing received cheques and internal
 * installments due this month. Every row links to its page. A null
 * summary means its source failed: the row says so instead of
 * showing a zero. Tranches are installation-wide (the settlement
 * API has no company field) and the widget says so.
 */
export function MoneyOnTheWayWidget({
  tranches,
  tranchesTruncated,
  inClearing,
  legsDue,
  legsTruncated,
  monthLabel,
  companyName,
  periodLabel,
}: LiveWidgetProps & {
  tranches: TrancheSummary | null;
  tranchesTruncated: boolean;
  inClearing: ChequeTotal | null;
  legsDue: ChequeTotal | null;
  legsTruncated: boolean;
  monthLabel: string;
}) {
  const allLoaded = tranches !== null && inClearing !== null && legsDue !== null;
  const empty =
    allLoaded &&
    tranches.receivableCount === 0 &&
    inClearing.count === 0 &&
    legsDue.count === 0;
  return (
    <WidgetCard
      icon={TrendingUp}
      testId="widget-money-on-the-way"
      title="پولِ در راه"
      meta={<WidgetMeta companyName={companyName} periodLabel={periodLabel} />}
      href="/settlements/schedule"
      hrefLabel="مشاهدهٔ زمان‌بندی تسویه"
    >
      {empty ? (
        <p className="text-sm text-muted-foreground">
          در این ماه پولی در راه نیست؛ نه قسط تسویه‌ای باز است، نه چکی در وصول، نه قسطی سررسید دارد.
        </p>
      ) : (
        <div className="space-y-1">
          {tranches === null ? (
            <>
              <UnavailableRow label="مطالبات درگاه" />
              <UnavailableRow label={`اقساط تسویهٔ ${monthLabel}`} />
            </>
          ) : (
            <>
              <KpiRow
                label="مطالبات درگاه"
                amountIrr={tranches.receivableTotalIrr}
                countLabel={`${faNumber(tranches.receivableCount)} قسط باز`}
                href="/settlements/schedule"
              />
              <KpiRow
                label={`اقساط تسویهٔ ${monthLabel}`}
                amountIrr={tranches.dueThisMonthTotalIrr}
                countLabel={`${faNumber(tranches.dueThisMonthCount)} قسط`}
                href="/settlements/schedule"
              />
            </>
          )}
          {inClearing === null ? (
            <UnavailableRow label="چک‌های در جریان وصول" />
          ) : (
            <KpiRow
              label="چک‌های در جریان وصول"
              amountIrr={inClearing.totalIrr}
              countLabel={`${faNumber(inClearing.count)} چک دریافتی`}
              href="/cheques/reports?report=in-clearing"
            />
          )}
          {legsDue === null ? (
            <UnavailableRow label={`اقساط سررسید ${monthLabel}`} />
          ) : (
            <KpiRow
              label={`اقساط سررسید ${monthLabel}`}
              amountIrr={legsDue.totalIrr}
              countLabel={`${faNumber(legsDue.count)} قسط داخلی`}
              href="/installments"
            />
          )}
          {tranchesTruncated ? (
            <p className="pt-1 text-xs text-muted-foreground">
              اقساط تسویه بسیار زیاد است؛ مطالبات درگاه بر اساس ۵۰۰ قسط اول محاسبه شده‌اند.
            </p>
          ) : null}
          {legsTruncated ? (
            <p className="pt-1 text-xs text-muted-foreground">
              اقساط بر اساس ۱۰ برنامهٔ اول محاسبه شده‌اند؛ برنامه‌های بیشتر در صفحهٔ اقساط است.
            </p>
          ) : null}
          <p className="pt-1 text-xs text-muted-foreground">
            اقساط تسویهٔ درگاه در سطح نصب هستند و فیلتر شرکت ندارند.
          </p>
        </div>
      )}
    </WidgetCard>
  );
}

const ALERT_TITLES: Record<DashboardAlertKind, string> = {
  "overdue-installment": "اقساط سررسیدگذشته",
  "overdue-tranche": "اقساط تسویهٔ سررسیدگذشته",
  "stale-inbox": "صندوق ورودی گیرکرده",
  "cheque-due-soon": "چک‌های نزدیک به سررسید",
  "packaging-reorder": "بسته‌بندی زیر نقطهٔ سفارش",
  "cashbox-uncounted": "صندوق‌های شمرده‌نشده",
};

function alertDetail(alert: DashboardAlert): React.ReactNode {
  switch (alert.kind) {
    case "overdue-installment":
      return `${faNumber(alert.count)} قسط · ماندهٔ سررسیدگذشته`;
    case "overdue-tranche":
      return `${faNumber(alert.count)} قسط تسویه`;
    case "stale-inbox":
      return `${faNumber(alert.count)} رویداد در انتظار پردازش`;
    case "cheque-due-soon":
      return `${faNumber(alert.count)} چک در ۷ روز آینده یا سررسیدگذشته`;
    case "packaging-reorder":
      return `${faNumber(alert.count)} قلم زیر نقطهٔ سفارش`;
    case "cashbox-uncounted":
      return `${faNumber(alert.count)} صندوق بیش از مهلت شمرده نشده است`;
  }
}

/**
 * هشدارها — every live alert, critical first. An empty section means
 * nothing needs attention. Each row links to its operational page.
 * `failedSources` names the alert sources that failed to load, so a
 * silent gap never passes for a clean bill of health.
 */
export function AlertsWidget({
  alerts,
  failedSources,
  companyName,
  periodLabel,
}: LiveWidgetProps & { alerts: DashboardAlert[]; failedSources: string[] }) {
  return (
    <WidgetCard
      icon={TriangleAlert}
      testId="widget-alerts"
      title="هشدارها"
      meta={<WidgetMeta companyName={companyName} periodLabel={periodLabel} unit="هشدار" />}
      href="/review-queue"
      hrefLabel="مشاهدهٔ صف بازبینی"
    >
      {alerts.length === 0 ? (
        <p className="text-sm text-muted-foreground">هشداری نیست؛ همه‌چیز در وضعیت عادی است.</p>
      ) : (
        <ul className="space-y-1">
          {alerts.map((alert) => (
            <li key={alert.id}>
              <Link
                href={alert.href}
                className="flex min-h-11 items-start gap-2 rounded-lg px-2 py-1 hover:bg-muted/50"
              >
                <TriangleAlert
                  className={`mt-1 size-4 shrink-0 ${alert.severity === "critical" ? "text-destructive" : "text-warning-foreground"}`}
                  strokeWidth={2}
                  aria-hidden
                />
                <span className="min-w-0 flex-1">
                  <span
                    className={`block text-sm font-medium ${alert.severity === "critical" ? "text-destructive" : ""}`}
                  >
                    {ALERT_TITLES[alert.kind]}
                  </span>
                  <span className="block text-xs text-muted-foreground">{alertDetail(alert)}</span>
                </span>
                {alert.totalIrr !== null ? <MoneyDisplay amountIrr={alert.totalIrr} size="sm" /> : null}
              </Link>
            </li>
          ))}
        </ul>
      )}
      {failedSources.length > 0 ? (
        <p className="pt-2 text-xs text-muted-foreground">
          برخی منابع هشدار در دسترس نبودند ({failedSources.join("، ")})؛ ممکن است هشداری جا مانده باشد.
        </p>
      ) : null}
      {alerts.some((alert) => alert.kind === "stale-inbox") ? (
        <p className="pt-1 text-xs text-muted-foreground">
          هشدار صندوق ورودی در سطح نصب است، نه شرکت.
        </p>
      ) : null}
    </WidgetCard>
  );
}

/**
 * خزانه — cashbox balances (live, company-scoped) + issued cheques
 * due this week (live, company-scoped) + the cheque exposure /
 * in-clearing widgets beside it. Bank book balances have NO API
 * source yet, so that figure is an honest coming-soon row — never
 * a zero presented as a real balance. A null summary means its
 * source failed: the row says so instead of showing a zero.
 */
export function TreasuryWidget({
  cashboxes,
  issuedDueSoon,
  companyName,
  periodLabel,
}: LiveWidgetProps & { cashboxes: CashboxSummary | null; issuedDueSoon: DueSoonSummary | null }) {
  return (
    <WidgetCard
      icon={Vault}
      testId="widget-treasury"
      title="خزانه"
      meta={<WidgetMeta companyName={companyName} periodLabel={periodLabel} />}
      href="/banking/cash-boxes"
      hrefLabel="مشاهدهٔ صندوق‌ها"
    >
      <div className="space-y-3">
        <div>
          <p className="text-sm text-muted-foreground">موجودی صندوق‌ها</p>
          {cashboxes === null ? (
            <p className="text-sm text-muted-foreground">دریافت موجودی صندوق‌ها ناموفق بود.</p>
          ) : cashboxes.boxCount === 0 ? (
            <p className="text-sm text-muted-foreground">صندوق فعالی ثبت نشده است.</p>
          ) : (
            <>
              <MoneyDisplay amountIrr={cashboxes.totalBalanceIrr} size="md" />
              <p className="text-sm text-muted-foreground">{faNumber(cashboxes.boxCount)} صندوق فعال</p>
              {cashboxes.unknownBalanceCount > 0 ? (
                <p className="text-xs text-muted-foreground">
                  ماندهٔ {faNumber(cashboxes.unknownBalanceCount)} صندوق در دسترس نیست و در مجموع نیامده است.
                </p>
              ) : null}
            </>
          )}
        </div>
        <div className="border-t border-border pt-3">
          <p className="text-sm text-muted-foreground">چک‌های پرداختی سررسید این هفته</p>
          {issuedDueSoon === null ? (
            <p className="text-sm text-muted-foreground">دریافت چک‌های پرداختی ناموفق بود.</p>
          ) : issuedDueSoon.count === 0 ? (
            <p className="text-sm text-muted-foreground">در ۷ روز آینده (و سررسیدگذشته) چک پرداختی بازی نیست.</p>
          ) : (
            <>
              <MoneyDisplay amountIrr={issuedDueSoon.totalIrr} size="md" />
              <p className="text-sm text-muted-foreground">
                {faNumber(issuedDueSoon.count)} چک · نزدیک‌ترین سررسید:{" "}
                {issuedDueSoon.nearestDueJalali ? formatJalaliShort(issuedDueSoon.nearestDueJalali) : "—"}
              </p>
            </>
          )}
        </div>
        <p className="flex items-start gap-2 border-t border-border pt-3 text-xs text-muted-foreground">
          <CircleAlert className="mt-0.5 size-4 shrink-0" strokeWidth={1.5} aria-hidden />
          ماندهٔ دفتری بانک‌ها و ماندهٔ قابل‌برداشت واقعی هنوز نقطهٔ پایانی در API ندارند؛
          پس از اضافه‌شدن گزارش ماندهٔ بانکی اینجا نمایش داده می‌شوند.
        </p>
      </div>
    </WidgetCard>
  );
}

/**
 * ایمپورت‌های دستی — the most recent import batches across both
 * flows (bank statements US-514 + settlement files US-523), with
 * status and computed effect. The US-554 batch model (one flow,
 * accounting-effect preview) replaces this merged view. When a flow
 * failed to load, the widget says which — an empty list means no
 * imports, not a failed fetch.
 */
export function ManualImportsWidget({
  rows,
  statementFailed,
  settlementFailed,
  companyName,
  periodLabel,
}: LiveWidgetProps & { rows: ImportBatchRow[]; statementFailed: boolean; settlementFailed: boolean }) {
  const bothFailed = statementFailed && settlementFailed;
  return (
    <WidgetCard
      icon={FileUp}
      testId="widget-manual-imports"
      title="ایمپورت‌های دستی"
      meta={<WidgetMeta companyName={companyName} periodLabel={periodLabel} unit="بسته" />}
      href="/banking/statements/import"
      hrefLabel="ایمپورت صورت‌حساب جدید"
    >
      {bothFailed ? (
        <p className="text-sm text-muted-foreground">دریافت ایمپورت‌های دستی ناموفق بود؛ دوباره تلاش کنید.</p>
      ) : rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">هنوز ایمپورت دستی ثبت نشده است.</p>
      ) : (
        <ul className="space-y-3">
          {rows.map((row) => (
            <li key={row.id}>
              <Link href={row.href} className="block rounded-lg px-2 py-1 hover:bg-muted/50">
                <span className="flex items-center justify-between gap-2">
                  <span className="min-w-0 truncate text-sm font-medium">{row.title}</span>
                  {row.kind === "statement" ? (
                    <StatusBadge
                      status={row.status}
                      label={statementBatchStatusLabel(row.status)}
                      tone={statementBatchStatusTone(row.status)}
                    />
                  ) : (
                    <StatusBadge
                      status={row.status}
                      label={importStatusLabel(row.status)}
                      tone={importStatusTone(row.status)}
                    />
                  )}
                </span>
                <span className="mt-1 block text-xs text-muted-foreground">
                  {row.kind === "statement" ? "صورت‌حساب بانکی" : "فایل تسویه"}
                  {row.createdAt ? ` · ${formatIsoDate(row.createdAt.slice(0, 10))}` : ""}
                </span>
                <span className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                  {row.depositsIrr !== null ? (
                    <span>
                      واریز: <MoneyDisplay amountIrr={row.depositsIrr} size="sm" />
                    </span>
                  ) : null}
                  {row.withdrawalsIrr !== null ? (
                    <span>
                      برداشت: <MoneyDisplay amountIrr={row.withdrawalsIrr} size="sm" />
                    </span>
                  ) : null}
                  {row.rowCount !== null ? <span>{faNumber(row.rowCount)} ردیف</span> : null}
                  {row.matchedCount !== null && row.kind === "settlement" ? (
                    <span>{faNumber(row.matchedCount)} منطبق</span>
                  ) : null}
                  {row.errorCount !== null && row.errorCount > 0 ? (
                    <span className="text-destructive">{faNumber(row.errorCount)} خطا</span>
                  ) : null}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
      {!bothFailed && (statementFailed || settlementFailed) ? (
        <p className="pt-2 text-xs text-muted-foreground">
          {statementFailed
            ? "دریافت ایمپورت‌های صورت‌حساب ناموفق بود."
            : "دریافت ایمپورت‌های تسویه ناموفق بود."}
        </p>
      ) : null}
    </WidgetCard>
  );
}

// The cash-forecast card lives in features/cash-forecast/
// (DashboardCashCard, US-553) and is placed by dashboard-home.tsx —
// the US-551 placeholder was removed at the US-553 merge.
