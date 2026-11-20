/**
 * Compliance center overview (US-405) — spec §18 module 8.
 *
 * Every figure comes from a live API call: the standards registry
 * (corpus / implemented / verified), the tax-policy registry (gated
 * parameters), Moadian (profiles + submissions) and the most recent
 * report-validation run (executed rules). Nothing is hard-coded and
 * nothing is shown as verified unless the API says so.
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { ApiError, api } from "@/lib/api-client";
import { fa } from "@/lib/utils";
import { Button, Card, CardContent, CardHeader, CardTitle, Skeleton } from "@/components/ui";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import { isPermissionDenied, persianApiErrorMessage } from "@/features/shared/api-error";
import { loadLastReportRunId, registryCoverage, runCoverage } from "./compliance-data";

function StatCard({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <Card>
      <CardContent className="space-y-1 pt-6">
        <p className="text-sm text-muted-foreground">{label}</p>
        <p className="text-2xl font-bold tabular-nums">{value}</p>
        <p className="text-xs text-muted-foreground">{hint}</p>
      </CardContent>
    </Card>
  );
}

function SectionCard({
  title,
  description,
  href,
  linkLabel,
  testId,
}: {
  title: string;
  description: string;
  href: string;
  linkLabel: string;
  testId: string;
}) {
  return (
    <Card data-testid={testId}>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-sm text-muted-foreground">{description}</p>
        <Link
          href={href}
          className="inline-flex min-h-11 items-center gap-1 text-sm font-medium text-primary hover:underline"
        >
          {linkLabel}
          <ArrowLeft className="size-4" strokeWidth={2} aria-hidden />
        </Link>
      </CardContent>
    </Card>
  );
}

export function ComplianceOverviewPage() {
  const router = useRouter();
  const [standards, setStandards] = React.useState<Awaited<ReturnType<typeof api.listStandards>> | null>(null);
  const [taxVersions, setTaxVersions] = React.useState<Awaited<ReturnType<typeof api.listTaxVersions>> | null>(null);
  const [profiles, setProfiles] = React.useState<Awaited<ReturnType<typeof api.listMoadianProfiles>> | null>(null);
  const [submissions, setSubmissions] = React.useState<Awaited<ReturnType<typeof api.listMoadianSubmissions>> | null>(null);
  const [lastRun, setLastRun] = React.useState<Awaited<ReturnType<typeof api.getReportValidationRun>> | null>(null);
  const [lastRunMissing, setLastRunMissing] = React.useState(false);
  const [error, setError] = React.useState<unknown>(null);
  const [reloadKey, setReloadKey] = React.useState(0);

  React.useEffect(() => {
    let cancelled = false;
    const lastRunId = loadLastReportRunId();
    Promise.all([
      api.listStandards({ limit: 100 }),
      api.listTaxVersions({ limit: 100 }),
      api.listMoadianProfiles({ limit: 100 }),
      api.listMoadianSubmissions({ limit: 100 }),
      lastRunId ? api.getReportValidationRun(lastRunId).catch(() => null) : Promise.resolve(null),
    ])
      .then(([std, tax, prof, subs, run]) => {
        if (cancelled) return;
        setStandards(std);
        setTaxVersions(tax);
        setProfiles(prof);
        setSubmissions(subs);
        if (lastRunId && !run) setLastRunMissing(true);
        setLastRun(run);
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
  }, [reloadKey, router]);

  const loading = standards === null || taxVersions === null || profiles === null || submissions === null;

  if (error && isPermissionDenied(error)) {
    return <PermissionDeniedState message={persianApiErrorMessage(error, "دریافت مرکز انطباق ناموفق بود.")} />;
  }

  if (error) {
    return (
      <ErrorState
        message={persianApiErrorMessage(error, "دریافت مرکز انطباق ناموفق بود.")}
        onRetry={() => setReloadKey((k) => k + 1)}
      />
    );
  }

  if (loading) {
    return (
      <div className="space-y-6" aria-label="در حال بارگذاری">
        <Skeleton className="h-8 w-56" />
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-24" />
          ))}
        </div>
      </div>
    );
  }

  const coverage = registryCoverage(standards.items);
  const executed = lastRun ? runCoverage(lastRun).rulesExecuted : null;
  const blockedStandards = standards.items.filter(
    (s) => s.latest_verification_status === "BLOCKED_SOURCE_GAP",
  ).length;
  const verifiedStandards = standards.items.filter(
    (s) => s.latest_verification_status === "VERIFIED_TEXT",
  ).length;
  const gatedTax = taxVersions.items.filter((v) => v.verification_status !== "VERIFIED_TEXT").length;
  const acceptedSubmissions = submissions.items.filter((s) => s.status === "accepted").length;

  return (
    <div className="space-y-6" data-testid="compliance-page">
      <div>
        <h1 className="text-2xl font-bold">استانداردها و انطباق</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          رجیستری استانداردها، سیاست مالیاتی، اعتبارسنجی‌ها و وضعیت مودیان — همه از دادهٔ زندهٔ API.
        </p>
      </div>

      <section aria-label="پوشش انطباق">
        <h2 className="mb-3 text-lg font-semibold">پوشش انطباق</h2>
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
          <StatCard label="استانداردهای ثبت‌شده" value={fa(coverage.corpusStandards)} hint="منبع رجیستری" />
          <StatCard label="استانداردهای دارای قاعده" value={fa(coverage.standardsWithRules)} hint="دارای قاعدهٔ ماشین‌خوان" />
          <StatCard label="قواعد پیاده‌شده" value={fa(coverage.rulesImplemented)} hint="در رجیستری ثبت شده" />
          <StatCard label="قواعد تأییدشده" value={fa(coverage.rulesVerified)} hint="بازبینی متن تأییدشده" />
          <StatCard
            label="قواعد اجراشده"
            value={executed === null ? "—" : fa(executed)}
            hint={
              lastRunMissing
                ? "آخرین اجرای ذخیره‌شده پیدا نشد؛ یک اعتبارسنجی تازه اجرا کنید."
                : executed === null
                  ? "هنوز اعتبارسنجی‌ای اجرا نشده است."
                  : "در آخرین اعتبارسنجی گزارش"
            }
          />
        </div>
        {executed === null ? (
          <p className="mt-3 text-sm text-muted-foreground">
            برای دیدن «قواعد اجراشده»، از بخش{" "}
            <Link href="/compliance/validations" className="font-medium text-primary hover:underline">
              اعتبارسنجی‌ها
            </Link>{" "}
            یک اعتبارسنجی گزارش اجرا کنید.
          </p>
        ) : null}
      </section>

      <section aria-label="وضعیت بخش‌ها">
        <h2 className="mb-3 text-lg font-semibold">وضعیت بخش‌ها</h2>
        <div className="grid gap-4 md:grid-cols-2">
          <SectionCard
            title="استانداردها"
            description={`${fa(verifiedStandards)} استاندارد با متن تأییدشده و ${fa(blockedStandards)} استاندارد مسدود به‌دلیل کمبود منبع. استاندارد ۱۵ برای دوره‌های ۱۴۰۵ به بعد مسدود است.`}
            href="/compliance/standards"
            linkLabel="مشاهدهٔ رجیستری استانداردها"
            testId="compliance-section-standards"
          />
          <SectionCard
            title="سیاست مالیاتی"
            description={`${fa(taxVersions.items.length)} پارامتر ۱۴۰۵ ثبت شده است؛ ${fa(gatedTax)} مورد فقط با منبع مکمل تأیید شده‌اند و برای استفادهٔ تولیدی مسدودند.`}
            href="/compliance/tax-policy"
            linkLabel="مشاهدهٔ سیاست مالیاتی"
            testId="compliance-section-tax"
          />
          <SectionCard
            title="اعتبارسنجی‌ها"
            description="اعتبارسنجی تراز آزمایشی زنده بر اساس استانداردها، با نتیجهٔ هر قاعده و منشأ کامل ارقام."
            href="/compliance/validations"
            linkLabel="اجرای اعتبارسنجی"
            testId="compliance-section-validations"
          />
          <SectionCard
            title="مودیان"
            description={`${fa(profiles.items.length)} پروفایل و ${fa(submissions.items.length)} ارسال ثبت شده است؛ ${fa(acceptedSubmissions)} مورد پذیرفته شده‌اند. فقط شبیه‌ساز آزمایشی فعال است؛ اتصال تولیدی وجود ندارد.`}
            href="/compliance/moadian"
            linkLabel="مشاهدهٔ مودیان"
            testId="compliance-section-moadian"
          />
        </div>
      </section>

      <p className="rounded-lg border px-4 py-3 text-sm text-muted-foreground" role="note">
        مودیان فقط در محیط آزمایشی (شبیه‌ساز) کار می‌کند؛ ارسال تولیدی مسدود است تا وقتی اتصال رسمی برقرار شود.
        پارامترهای مالیاتی ۱۴۰۵ فقط مرجع‌اند و نرخ تأییدشدهٔ تولیدی محسوب نمی‌شوند.
      </p>

      <Button variant="outline" onClick={() => setReloadKey((k) => k + 1)} className="print:hidden">
        به‌روزرسانی
      </Button>
    </div>
  );
}
