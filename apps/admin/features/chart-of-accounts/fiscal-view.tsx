"use client";

/**
 * Fiscal years / periods / setup-status view (US-302) — from the US-203
 * endpoints, company-scoped. One page, three tabs:
 *
 * - سال‌های مالی: GET /api/v1/accounting/fiscal-years (open/closed state)
 * - دوره‌های مالی: GET /api/v1/accounting/fiscal-periods (per fiscal year)
 * - وضعیت راه‌اندازی: GET /api/v1/accounting/setup-status
 *
 * Dates display Jalali (from the Gregorian ISO dates the API stores);
 * sorting/filtering use the normalized ISO values (§5.5 rule 10). Load
 * states carry the company (and year) id they belong to, so switching
 * company or year never shows another scope's data while loading.
 */

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowLeft, CheckCircle2, TriangleAlert, XCircle } from "lucide-react";
import {
  api,
  ApiError,
  type FiscalPeriodResponse,
  type FiscalYearResponse,
  type SetupStatusResponse,
} from "@/lib/api-client";
import { formatIsoDate } from "@/lib/format-date";
import { faNumber } from "@/lib/utils";
import {
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  DataTable,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Skeleton,
  StatusBadge,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui";
import { apiErrorMessage, useCompany } from "@/features/dashboard/company-context";

/** Tryton period type -> Persian label (unknown types render raw). */
function periodTypeLabel(type: string): string {
  if (type === "standard") return "استاندارد";
  if (type === "adjustment") return "تعدیلی";
  return type;
}

type TabId = "years" | "periods" | "setup";

type YearsState =
  | { kind: "loading"; companyId: string | null }
  | { kind: "ok"; companyId: string; years: FiscalYearResponse[] }
  | { kind: "error"; companyId: string; message: string };

type SetupState =
  | { kind: "loading"; companyId: string | null }
  | { kind: "ok"; companyId: string; setup: SetupStatusResponse }
  | { kind: "error"; companyId: string; message: string };

type PeriodsState =
  | { kind: "loading"; companyId: string | null; yearId: number | null }
  | { kind: "ok"; companyId: string; yearId: number; periods: FiscalPeriodResponse[] }
  | { kind: "error"; companyId: string; yearId: number; message: string };

export function FiscalView() {
  const router = useRouter();
  const { company, companyId, status: companyStatus, errorMessage: companyError, refresh } = useCompany();
  const [tab, setTab] = React.useState<TabId>("years");
  const [yearsState, setYearsState] = React.useState<YearsState>({ kind: "loading", companyId: null });
  const [selectedYearId, setSelectedYearId] = React.useState<number | null>(null);
  const [periodsState, setPeriodsState] = React.useState<PeriodsState>({
    kind: "loading",
    companyId: null,
    yearId: null,
  });
  const [setupState, setSetupState] = React.useState<SetupState>({ kind: "loading", companyId: null });
  const [nonce, setNonce] = React.useState(0);

  React.useEffect(() => {
    if (companyStatus !== "ready" || companyId === null) return;
    let cancelled = false;
    api
      .listFiscalYears({ company: companyId, limit: 50 })
      .then((page) => {
        if (cancelled) return;
        setYearsState({ kind: "ok", companyId, years: page.items });
        const open = page.items.find((y) => y.is_open) ?? page.items[0] ?? null;
        setSelectedYearId(open ? open.id : null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.push("/login");
          return;
        }
        setYearsState({
          kind: "error",
          companyId,
          message: apiErrorMessage(err, "دریافت سال‌های مالی ناموفق بود."),
        });
      });
    api
      .getSetupStatus(companyId)
      .then((setup) => {
        if (!cancelled) setSetupState({ kind: "ok", companyId, setup });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.push("/login");
          return;
        }
        setSetupState({
          kind: "error",
          companyId,
          message: apiErrorMessage(err, "دریافت وضعیت راه‌اندازی ناموفق بود."),
        });
      });
    return () => {
      cancelled = true;
    };
  }, [companyStatus, companyId, nonce, router]);

  React.useEffect(() => {
    if (companyStatus !== "ready" || companyId === null || selectedYearId === null) return;
    let cancelled = false;
    const yearId = selectedYearId;
    api
      .listFiscalPeriods({ company: companyId, fiscal_year: String(yearId), limit: 100 })
      .then((page) => {
        if (!cancelled) setPeriodsState({ kind: "ok", companyId, yearId, periods: page.items });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.push("/login");
          return;
        }
        setPeriodsState({
          kind: "error",
          companyId,
          yearId,
          message: apiErrorMessage(err, "دریافت دوره‌های مالی ناموفق بود."),
        });
      });
    return () => {
      cancelled = true;
    };
  }, [companyStatus, companyId, selectedYearId, router]);

  const retry = () => {
    setYearsState({ kind: "loading", companyId });
    setSetupState({ kind: "loading", companyId });
    setPeriodsState({ kind: "loading", companyId, yearId: selectedYearId });
    setNonce((n) => n + 1);
  };

  const years = yearsState.kind === "ok" && yearsState.companyId === companyId ? yearsState.years : null;
  const yearsStale = yearsState.companyId !== companyId;
  const setup = setupState.kind === "ok" && setupState.companyId === companyId ? setupState.setup : null;
  const setupStale = setupState.companyId !== companyId;
  const periodsStale = periodsState.companyId !== companyId || periodsState.yearId !== selectedYearId;
  const periods =
    periodsState.kind === "ok" && !periodsStale ? periodsState.periods : null;
  const selectedYear = years?.find((y) => y.id === selectedYearId) ?? null;

  if (companyStatus === "loading") {
    return (
      <div className="space-y-6" aria-label="در حال بارگذاری سال‌های مالی">
        <h1 className="text-2xl font-bold">سال‌های مالی</h1>
        <Skeleton className="h-10 w-full max-w-96" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (companyStatus === "error") {
    return (
      <div className="space-y-4" role="alert">
        <h1 className="text-2xl font-bold">سال‌های مالی</h1>
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
        <h1 className="text-2xl font-bold">سال‌های مالی</h1>
        <p className="text-sm text-muted-foreground">هیچ شرکتی در دسترس این کلید نیست.</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">سال‌های مالی</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {company.name} · سال‌ها و دوره‌های مالی و وضعیت راه‌اندازی نمودار حساب‌ها
        </p>
      </div>

      <Tabs value={tab} onValueChange={(value) => setTab(value as TabId)}>
        <TabsList aria-label="بخش‌های سال مالی">
          <TabsTrigger value="years">سال‌های مالی</TabsTrigger>
          <TabsTrigger value="periods">دوره‌های مالی</TabsTrigger>
          <TabsTrigger value="setup">وضعیت راه‌اندازی</TabsTrigger>
        </TabsList>

        <TabsContent value="years" className="space-y-4">
          {yearsState.kind === "error" && !yearsStale ? (
            <div className="space-y-4" role="alert">
              <p className="flex items-start gap-2 text-sm text-destructive">
                <TriangleAlert className="mt-1 size-4 shrink-0" aria-hidden />
                {yearsState.message}
              </p>
              <Button variant="outline" onClick={retry}>
                تلاش دوباره
              </Button>
            </div>
          ) : years === null ? (
            <div className="space-y-2" aria-label="در حال بارگذاری سال‌های مالی">
              {Array.from({ length: 3 }).map((_, i) => (
                <Skeleton key={i} className="h-11 w-full" />
              ))}
            </div>
          ) : (
            <>
              <DataTable<FiscalYearResponse>
                columns={[
                  {
                    key: "name",
                    header: "نام",
                    render: (row) => <span className="ltr-isolate font-medium">{row.name}</span>,
                  },
                  { key: "start", header: "شروع", render: (row) => formatIsoDate(row.start_date) },
                  { key: "end", header: "پایان", render: (row) => formatIsoDate(row.end_date) },
                  {
                    key: "state",
                    header: "وضعیت",
                    render: (row) => (
                      <StatusBadge status={row.is_open ? "open" : "closed"} label={row.is_open ? "باز" : "بسته"} />
                    ),
                  },
                  {
                    key: "actions",
                    header: "",
                    render: (row) => (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          setSelectedYearId(row.id);
                          setTab("periods");
                        }}
                      >
                        دوره‌ها
                        <ArrowLeft className="size-4" strokeWidth={2} aria-hidden />
                      </Button>
                    ),
                  },
                ]}
                rows={years}
                rowKey={(row) => row.id}
                onRowClick={(row) => {
                  setSelectedYearId(row.id);
                  setTab("periods");
                }}
                emptyText="سال مالی‌ای برای این شرکت ثبت نشده است."
              />
              <p className="text-sm text-muted-foreground">
                برای دیدن دوره‌های یک سال، روی آن کلیک کنید.
              </p>
            </>
          )}
        </TabsContent>

        <TabsContent value="periods" className="space-y-4">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-end">
            <div className="sm:w-72">
              <Label htmlFor="fiscal-year-select">سال مالی</Label>
              <Select
                value={selectedYearId === null ? undefined : String(selectedYearId)}
                onValueChange={(value) => {
                  if (value !== null) setSelectedYearId(Number(value));
                }}
                disabled={years === null || years.length === 0}
              >
                <SelectTrigger id="fiscal-year-select" aria-label="سال مالی" className="mt-2 w-full">
                  {/*
                    Base UI shows the raw value while the popup is closed —
                    pass the selected year's display label explicitly.
                  */}
                  <SelectValue placeholder="انتخاب سال مالی">
                    {selectedYear ? (
                      <>
                        <span className="ltr-isolate">{selectedYear.name}</span>
                        {selectedYear.is_open ? " (باز)" : ""}
                      </>
                    ) : undefined}
                  </SelectValue>
                </SelectTrigger>
                <SelectContent>
                  {(years ?? []).map((year) => (
                    <SelectItem key={year.id} value={String(year.id)}>
                      <span className="ltr-isolate">{year.name}</span>
                      {year.is_open ? " (باز)" : ""}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {selectedYear ? (
              <p className="text-sm text-muted-foreground">
                {formatIsoDate(selectedYear.start_date)} تا {formatIsoDate(selectedYear.end_date)}
              </p>
            ) : null}
          </div>

          {periodsState.kind === "error" && !periodsStale ? (
            <div className="space-y-4" role="alert">
              <p className="flex items-start gap-2 text-sm text-destructive">
                <TriangleAlert className="mt-1 size-4 shrink-0" aria-hidden />
                {periodsState.message}
              </p>
              <Button variant="outline" onClick={retry}>
                تلاش دوباره
              </Button>
            </div>
          ) : periods === null ? (
            <div className="space-y-2" aria-label="در حال بارگذاری دوره‌های مالی">
              {Array.from({ length: 5 }).map((_, i) => (
                <Skeleton key={i} className="h-11 w-full" />
              ))}
            </div>
          ) : (
            <DataTable<FiscalPeriodResponse>
              columns={[
                { key: "name", header: "نام", render: (row) => <span className="ltr-isolate font-medium">{row.name}</span> },
                { key: "start", header: "شروع", render: (row) => formatIsoDate(row.start_date) },
                { key: "end", header: "پایان", render: (row) => formatIsoDate(row.end_date) },
                { key: "type", header: "نوع", render: (row) => periodTypeLabel(row.type) },
                {
                  key: "state",
                  header: "وضعیت",
                  render: (row) => (
                    <StatusBadge status={row.is_open ? "open" : "closed"} label={row.is_open ? "باز" : "بسته"} />
                  ),
                },
              ]}
              rows={periods}
              rowKey={(row) => row.id}
              emptyText="دوره‌ای برای این سال مالی ثبت نشده است."
            />
          )}
        </TabsContent>

        <TabsContent value="setup" className="space-y-4">
          {setupState.kind === "error" && !setupStale ? (
            <div className="space-y-4" role="alert">
              <p className="flex items-start gap-2 text-sm text-destructive">
                <TriangleAlert className="mt-1 size-4 shrink-0" aria-hidden />
                {setupState.message}
              </p>
              <Button variant="outline" onClick={retry}>
                تلاش دوباره
              </Button>
            </div>
          ) : setup === null ? (
            <div className="grid gap-4 md:grid-cols-2" aria-label="در حال بارگذاری وضعیت راه‌اندازی">
              {Array.from({ length: 4 }).map((_, i) => (
                <Skeleton key={i} className="h-28 w-full" />
              ))}
            </div>
          ) : (
            <>
              <div className="grid gap-4 md:grid-cols-2">
                <SetupCard
                  title="نمودار حساب‌ها"
                  ok={setup.chart_installed}
                  okText={`نصب‌شده · ${faNumber(setup.account_count)} حساب`}
                  notOkText="نصب نشده است"
                />
                <SetupCard
                  title="سال مالی"
                  ok={setup.fiscal_year_present}
                  okText={
                    setup.open_fiscal_year
                      ? `${faNumber(setup.fiscal_year_count)} سال · باز: ${setup.open_fiscal_year.name}`
                      : `${faNumber(setup.fiscal_year_count)} سال ثبت‌شده، بدون سال باز`
                  }
                  notOkText="سال مالی‌ای ثبت نشده است"
                />
                <SetupCard
                  title="دوره‌های مالی"
                  ok={setup.open_period_present}
                  okText={`${faNumber(setup.open_period_count)} دورهٔ باز`}
                  notOkText="دورهٔ بازی وجود ندارد"
                />
                <SetupCard
                  title="آمادگی کلی"
                  ok={setup.ready}
                  okText="آمادهٔ ثبت اسناد"
                  notOkText="هنوز آماده نیست"
                />
              </div>
              {setup.chart_template ? (
                <p className="text-sm text-muted-foreground">
                  قالب نمودار: <span className="ltr-isolate">{setup.chart_template}</span>
                </p>
              ) : null}
              <p>
                <Link
                  href="/accounting/accounts"
                  className="inline-flex min-h-11 items-center gap-1 text-sm font-medium text-brand hover:underline"
                >
                  مشاهدهٔ سر فصل حساب‌ها
                  <ArrowLeft className="size-4" strokeWidth={2} aria-hidden />
                </Link>
              </p>
            </>
          )}
        </TabsContent>
      </Tabs>
    </div>
  );
}

function SetupCard({
  title,
  ok,
  okText,
  notOkText,
}: {
  title: string;
  ok: boolean;
  okText: string;
  notOkText: string;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent>
        <p className="flex items-start gap-2 text-sm">
          {ok ? (
            <CheckCircle2 className="mt-1 size-5 shrink-0 text-success" strokeWidth={1.5} aria-hidden />
          ) : (
            <XCircle className="mt-1 size-5 shrink-0 text-destructive" strokeWidth={1.5} aria-hidden />
          )}
          <span>
            <StatusBadge status={ok ? "ready" : "not_ready"} />
            <span className="mt-1 block text-muted-foreground">{ok ? okText : notOkText}</span>
          </span>
        </p>
      </CardContent>
    </Card>
  );
}
