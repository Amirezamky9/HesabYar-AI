/**
 * Checkbooks (US-535; report 8 / صورت وضعیت دسته‌چک).
 *
 * Every checkbook of the selected company with used / voided /
 * remaining leaves (from the engine registry) and the Fintech
 * chequebook-inquiry note. The inquiry itself is a SIMULATOR until
 * the ۵ت remainder (B3) wires live connectivity — the leaf counts
 * below come from the engine registry, not from Sayyad, and the
 * page says so.
 */

"use client";

import * as React from "react";
import { ApiError, api, type CheckbookResponse } from "@/lib/api-client";
import { Card, CardContent, CardHeader, CardTitle, Skeleton } from "@/components/ui";
import { faNumber } from "@/lib/utils";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import { isPermissionDenied, persianApiErrorMessage } from "@/features/shared/api-error";
import { CompanyPicker } from "@/features/shared/company-picker";
import { useCompany } from "@/features/shared/use-company";

function LeafBar({ checkbook }: { checkbook: CheckbookResponse }) {
  const total = Math.max(1, checkbook.total_leaves);
  const usedPct = (checkbook.used_leaves / total) * 100;
  const voidedPct = (checkbook.voided_leaves / total) * 100;
  const remainingPct = Math.max(0, 100 - usedPct - voidedPct);
  return (
    <div
      className="flex h-3 w-full overflow-hidden rounded-full bg-muted"
      role="img"
      aria-label={`مصرف‌شده ${faNumber(checkbook.used_leaves)}، باطل‌شده ${faNumber(checkbook.voided_leaves)}، باقی‌مانده ${faNumber(checkbook.remaining_leaves)} از ${faNumber(checkbook.total_leaves)} برگ`}
    >
      <span className="bg-primary" style={{ width: `${usedPct}%` }} />
      <span className="bg-destructive" style={{ width: `${voidedPct}%` }} />
      <span className="bg-success" style={{ width: `${remainingPct}%` }} />
    </div>
  );
}

export function CheckbooksPage() {
  const { companies, companyId, setCompanyId, loading: companyLoading, error: companyError } = useCompany();
  const company = companies.find((c) => c.id === companyId) ?? null;
  const [rows, setRows] = React.useState<CheckbookResponse[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<unknown>(null);

  // Load lives in a callback (not the effect body) so the effect only
  // subscribes; the retry button reuses the same callback.
  const load = React.useCallback(() => {
    if (!company) return undefined;
    let cancelled = false;
    setLoading(true);
    api
      .listCheckbooks(company.engine_id)
      .then((page) => {
        if (cancelled) return;
        setRows(page);
        setError(null);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          setError(err);
          setLoading(false);
          return;
        }
        setError(err);
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [company]);

  // Loading state is set by load() from a timer callback, never
  // synchronously in the effect body (react-hooks/set-state-in-effect).
  React.useEffect(() => {
    let cleanup: (() => void) | undefined;
    const timer = setTimeout(() => {
      cleanup = load();
    }, 0);
    return () => {
      clearTimeout(timer);
      cleanup?.();
    };
  }, [load]);

  if (companyError && isPermissionDenied(companyError)) {
    return <PermissionDeniedState message="کلید شما دسترسی خواندن شرکت‌ها را ندارد." />;
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">دسته‌چک‌ها</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          صورت وضعیت دسته‌چک (گزارش ۸): برگ‌های مصرف‌شده، باطل‌شده و باقی‌ماندهٔ هر دسته‌چک.
        </p>
      </div>

      <CompanyPicker
        companies={companies}
        companyId={companyId}
        onChange={setCompanyId}
      />

      {error ? (
        <ErrorState
          message={persianApiErrorMessage(error, "دریافت دسته‌چک‌ها ناموفق بود.")}
          onRetry={load}
        />
      ) : loading || companyLoading ? (
        <div className="grid gap-4 md:grid-cols-2" aria-label="در حال بارگذاری">
          <Skeleton className="h-44 w-full" />
          <Skeleton className="h-44 w-full" />
        </div>
      ) : rows.length === 0 ? (
        <p className="rounded-lg border px-4 py-6 text-center text-sm text-muted-foreground">
          برای این شرکت دسته‌چکی در موتور ثبت نشده است.
        </p>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {rows.map((checkbook) => (
            <Card key={checkbook.checkbook_engine_id} data-testid="checkbook-card">
              <CardHeader>
                <CardTitle>{checkbook.name}</CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                <LeafBar checkbook={checkbook} />
                <dl className="grid grid-cols-2 gap-2 text-sm">
                  <div className="flex justify-between gap-2">
                    <dt className="text-muted-foreground">کل برگ‌ها</dt>
                    <dd className="font-medium tabular-nums">{faNumber(checkbook.total_leaves)}</dd>
                  </div>
                  <div className="flex justify-between gap-2">
                    <dt className="text-muted-foreground">مصرف‌شده</dt>
                    <dd className="font-medium tabular-nums">{faNumber(checkbook.used_leaves)}</dd>
                  </div>
                  <div className="flex justify-between gap-2">
                    <dt className="text-muted-foreground">باطل‌شده</dt>
                    <dd className="font-medium tabular-nums">{faNumber(checkbook.voided_leaves)}</dd>
                  </div>
                  <div className="flex justify-between gap-2">
                    <dt className="text-muted-foreground">باقی‌مانده</dt>
                    <dd className="font-medium tabular-nums">{faNumber(checkbook.remaining_leaves)}</dd>
                  </div>
                  <div className="flex justify-between gap-2">
                    <dt className="text-muted-foreground">ثبت‌شده در صیاد</dt>
                    <dd className="font-medium tabular-nums">
                      {checkbook.leaves_registered_in_sayad === null ? "نامشخص" : faNumber(checkbook.leaves_registered_in_sayad)}
                    </dd>
                  </div>
                </dl>
                {checkbook.inquiry_notes.map((note) => (
                  <p key={note} className="text-xs text-muted-foreground">
                    {checkbook.inquiry_simulated ? "استعلام شبیه‌ساز: " : ""}
                    {note}
                  </p>
                ))}
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
