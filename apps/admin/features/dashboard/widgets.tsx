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
  HandCoins,
  Hourglass,
  Inbox,
  Landmark,
  NotebookPen,
  Scale,
  ShieldCheck,
  TicketCheck,
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
import {
  dueBucket,
  formatJalaliShort,
  sumIrr,
} from "@/features/cheques/cheque-data";
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
import type { DraftSummary, InvoiceSummary } from "./dashboard-data";
import { formatAgeHours, reviewTypeLabel } from "@/features/review-queue/review-queue-data";

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
 * (collateral, report 6), kept strictly apart. The cheque list has
 * no company filter in the API, so the figures are tenant-wide and
 * the meta line says so.
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
 * within 7 days (or overdue), from the registry rows. */
export function ChequeDueSoonWidget({
  cheques,
  todayJalali,
  companyName,
  periodLabel,
}: LiveWidgetProps & { cheques: ChequeResponse[]; todayJalali: string }) {
  const dueSoon = cheques
    .filter((c) => c.kind === "received" && c.purpose === "payment" && ["held", "in_clearing", "registered"].includes(c.state))
    .filter((c) => ["overdue", "due-7"].includes(dueBucket(c.due_date, todayJalali)))
    .sort((a, b) => (a.due_date < b.due_date ? -1 : 1));
  const nearest = dueSoon[0];
  return (
    <WidgetCard
      icon={CalendarClock}
      testId="widget-cheque-due-soon"
      title="چک‌های نزدیک به سررسید"
      meta={<WidgetMeta companyName={companyName} periodLabel={periodLabel} />}
      href="/cheques/reports?report=due-received"
      hrefLabel="مشاهدهٔ گزارش سررسید"
    >
      {dueSoon.length === 0 ? (
        <p className="text-sm text-muted-foreground">در ۷ روز آینده (و سررسیدگذشته) چک دریافتی بازی نیست.</p>
      ) : (
        <div className="space-y-2">
          <MoneyDisplay amountIrr={sumIrr(dueSoon)} size="md" />
          <p className="text-sm text-muted-foreground">
            {faNumber(dueSoon.length)} چک · نزدیک‌ترین سررسید:{" "}
            {nearest ? formatJalaliShort(nearest.due_date) : "—"}
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
