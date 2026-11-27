/**
 * Intake queue (US-535; US-533's queue, report 11's rows).
 *
 * Registered cheques awaiting the physical leaf, oldest first.
 * `age_days` is days since the site registered the cheque; the
 * day-3 and day-(N-1) reminders are filed by the daily job, and the
 * leaf expires at the installation deadline (settings, default 7
 * days). Confirming the leaf happens on the cheque detail page
 * (confirm-receipt); this page is the operator's watch list.
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ApiError, api, type ChequeResponse, type ChequeSettingsResponse } from "@/lib/api-client";
import { Card, CardContent, DataTable, MoneyDisplay, Skeleton, type DataTableColumn } from "@/components/ui";
import { faNumber } from "@/lib/utils";
import { formatIsoDateTime } from "@/lib/format-date";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import { isPermissionDenied, persianApiErrorMessage } from "@/features/shared/api-error";
import {
  INTAKE_REMINDER_LABELS,
  formatJalaliShort,
  intakeReminderState,
} from "./cheque-data";

export function ChequeIntakePage() {
  const router = useRouter();
  const [rows, setRows] = React.useState<ChequeResponse[]>([]);
  const [settings, setSettings] = React.useState<ChequeSettingsResponse | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<unknown>(null);

  const load = React.useCallback(() => {
    let cancelled = false;
    setLoading(true);
    Promise.all([api.listChequeIntake({ limit: 100 }), api.getChequeSettings()])
      .then(([queue, chequeSettings]) => {
        if (cancelled) return;
        setRows(queue);
        setSettings(chequeSettings);
        setError(null);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.push("/login");
          return;
        }
        setError(err);
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

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

  if (error && isPermissionDenied(error)) {
    return <PermissionDeniedState message="کلید شما دسترسی خواندن چک‌ها را ندارد." />;
  }

  const deadline = settings?.leaf_arrival_deadline_days ?? 7;
  const expiredCount = rows.filter((r) => intakeReminderState(r.age_days, deadline) === "expired").length;

  const columns: DataTableColumn<ChequeResponse>[] = [
    {
      key: "sayad",
      header: "شناسهٔ صیاد",
      render: (row) => (
        <Link href={`/cheques/${row.id}`} className="font-mono text-sm text-primary underline-offset-4 hover:underline" dir="ltr">
          {row.sayad_id}
        </Link>
      ),
    },
    {
      key: "party",
      header: "صادرکننده",
      render: (row) => (
        <span>
          {row.issuer_name}
          <span className="block text-xs text-muted-foreground">{row.issuer_bank}</span>
        </span>
      ),
    },
    {
      key: "amount",
      header: "مبلغ",
      render: (row) => <MoneyDisplay amountIrr={row.amount_irr} size="sm" />,
    },
    {
      key: "due",
      header: "سررسید",
      render: (row) => formatJalaliShort(row.due_date),
    },
    {
      key: "age",
      header: "قدمت (روز)",
      render: (row) => {
        const state = intakeReminderState(row.age_days, deadline);
        return (
          <span>
            {faNumber(row.age_days)}
            <span className="block text-xs text-muted-foreground">{INTAKE_REMINDER_LABELS[state]}</span>
          </span>
        );
      },
    },
    {
      key: "registered",
      header: "ثبت سایت",
      render: (row) => formatIsoDateTime(row.created_at),
    },
  ];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">در انتظار رسید برگ</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          چک‌های ثبت‌شده در سایت که برگ فیزیکی‌شان هنوز نرسیده است — قدیمی‌ترین اول. مهلت رسیدن برگ {faNumber(deadline)} روز است؛
          یادآوری روز ۳ و روز پایانی را job روزانه ثبت می‌کند و در مهلت، چک منقضی می‌شود.
        </p>
      </div>

      {error ? (
        <ErrorState
          message={persianApiErrorMessage(error, "دریافت صف intake ناموفق بود.")}
          onRetry={load}
        />
      ) : loading ? (
        <div className="space-y-2" aria-label="در حال بارگذاری">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      ) : (
        <>
          <div className="grid gap-4 sm:grid-cols-3">
            <Card>
              <CardContent className="pt-6">
                <p className="text-sm text-muted-foreground">در انتظار برگ</p>
                <p className="mt-1 text-2xl font-bold tabular-nums">{faNumber(rows.length)}</p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-6">
                <p className="text-sm text-muted-foreground">منقضی (برگ نرسید)</p>
                <p className="mt-1 text-2xl font-bold tabular-nums">{faNumber(expiredCount)}</p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-6">
                <p className="text-sm text-muted-foreground">مهلت رسیدن برگ</p>
                <p className="mt-1 text-2xl font-bold tabular-nums">{faNumber(deadline)} روز</p>
              </CardContent>
            </Card>
          </div>
          <DataTable
            columns={columns}
            rows={rows}
            rowKey={(row) => row.id}
            emptyText="چکی در انتظار برگ نیست؛ همهٔ برگ‌ها رسیده‌اند."
            onRowClick={(row) => router.push(`/cheques/${row.id}`)}
            caption="صف intake — ثبت سایت فقط یک ادعاست و تا رسیدن برگ هیچ سندی ندارد."
          />
        </>
      )}
    </div>
  );
}
