/**
 * Validations tab (US-405).
 *
 * Runs a report validation over the company's LIVE trial balance
 * (POST /api/v1/report-validation-runs): no hand-made financial data,
 * no fabricated figures. The run result — per-rule outcomes, coverage,
 * provenance and the backend's Persian note — renders below the form.
 * Statement-level rules on trial-balance data yield ERROR; the note
 * explains that, and the table shows it.
 */

"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { ApiError, api, type ReportValidationRunResponse } from "@/lib/api-client";
import {
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  JalaliDatePicker,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui";
import { CompanyPicker } from "@/features/shared/company-picker";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import { isPermissionDenied, persianApiErrorMessage } from "@/features/shared/api-error";
import { useCompany } from "@/features/shared/use-company";
import { saveLastReportRunId } from "./compliance-data";
import { ValidationRunResult } from "./validation-run-result";

/** Default period: 1404-01-01..1404-12-29 (Gregorian 2025-03-21..2026-03-20) — inside Standard 15's allowed range. */
const DEFAULT_PERIOD_START = "2025-03-21";
const DEFAULT_PERIOD_END = "2026-03-20";

export function ValidationsPage() {
  const router = useRouter();
  const { companies, companyId, setCompanyId, loading: companiesLoading, error: companiesError } = useCompany();
  const [periodStart, setPeriodStart] = React.useState<string | null>(DEFAULT_PERIOD_START);
  const [periodEnd, setPeriodEnd] = React.useState<string | null>(DEFAULT_PERIOD_END);
  const [standardScope, setStandardScope] = React.useState("1");
  const [running, setRunning] = React.useState(false);
  const [run, setRun] = React.useState<ReportValidationRunResponse | null>(null);
  const [runError, setRunError] = React.useState<string | null>(null);
  const [reloadKey, setReloadKey] = React.useState(0);

  // `reloadKey` remounts the page (and the company hook) on retry.
  if (companiesError && isPermissionDenied(companiesError)) {
    return (
      <div key={reloadKey}>
        <PermissionDeniedState message={persianApiErrorMessage(companiesError, "دریافت شرکت‌ها ناموفق بود.")} />
      </div>
    );
  }

  if (companiesError) {
    return (
      <div key={reloadKey}>
        <ErrorState
          message={persianApiErrorMessage(companiesError, "دریافت شرکت‌ها ناموفق بود.")}
          onRetry={() => setReloadKey((k) => k + 1)}
        />
      </div>
    );
  }

  const runValidation = async () => {
    if (!companyId || !periodStart || !periodEnd) {
      setRunError("شرکت و بازهٔ دوره را انتخاب کنید.");
      return;
    }
    if (periodEnd < periodStart) {
      setRunError("پایان دوره باید بعد از شروع دوره باشد.");
      return;
    }
    setRunning(true);
    setRun(null);
    setRunError(null);
    try {
      const result = await api.createReportValidationRun({
        company_id: companyId,
        period_start: periodStart,
        period_end: periodEnd,
        standard_codes: standardScope === "all" ? null : [standardScope],
      });
      setRun(result);
      saveLastReportRunId(result.run.run_id);
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setRunError(persianApiErrorMessage(err, "اجرای اعتبارسنجی ناموفق بود."));
    } finally {
      setRunning(false);
    }
  };

  return (
    <div key={reloadKey} className="space-y-6" data-testid="validations-page">
      <div>
        <h1 className="text-2xl font-bold">اعتبارسنجی‌ها</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          اعتبارسنجی تراز آزمایشی زندهٔ شرکت بر اساس استانداردها؛ نتیجهٔ هر قاعده، پوشش و منشأ ارقام نمایش
          داده می‌شود.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>اجرای اعتبارسنجی گزارش</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {companiesLoading ? (
            <p className="text-sm text-muted-foreground">در حال دریافت شرکت‌ها…</p>
          ) : companies.length === 0 ? (
            <p className="text-sm text-muted-foreground">هیچ شرکتی در دسترس نیست.</p>
          ) : (
            <CompanyPicker companies={companies} companyId={companyId} onChange={setCompanyId} />
          )}
          <div className="grid gap-4 md:grid-cols-3">
            <div className="space-y-2">
              <Label htmlFor="period-start">شروع دوره</Label>
              <JalaliDatePicker id="period-start" value={periodStart} onChange={setPeriodStart} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="period-end">پایان دوره</Label>
              <JalaliDatePicker id="period-end" value={periodEnd} onChange={setPeriodEnd} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="standard-scope">استانداردها</Label>
              <Select value={standardScope} onValueChange={(v) => v && setStandardScope(v)}>
                <SelectTrigger id="standard-scope">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="1">استاندارد ۱ (دارای قاعده)</SelectItem>
                  <SelectItem value="all">همهٔ استانداردها</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <p className="text-sm text-muted-foreground">
            تراز آزمایشی فقط از سندهای ثبت‌شده خوانده می‌شود؛ اگر سندی ثبت نشده باشد، قواعد به‌دلیل نبود داده
            با وضعیت «خطا» ثبت می‌شوند، نه «قبول».
          </p>
          <Button onClick={runValidation} disabled={running || !companyId} data-testid="run-validation">
            {running ? "در حال اجرا…" : "اجرای اعتبارسنجی"}
          </Button>
          {runError ? (
            <p className="text-sm text-destructive" role="alert">
              {runError}
            </p>
          ) : null}
        </CardContent>
      </Card>

      {run ? <ValidationRunResult run={run} /> : null}
    </div>
  );
}
