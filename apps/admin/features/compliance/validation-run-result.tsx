/**
 * Shared validation-run result view (US-405) — used by the validations
 * tab after a report-validation run.
 *
 * Shows the run outcome, the four outcome counts, the full coverage
 * snapshot (corpus vs implemented vs verified vs executed), the
 * per-rule results, the rules that were implemented but not executed
 * (with reasons), the snapshot provenance and the backend's own Persian
 * note. Statement-level rules on trial-balance data yield ERROR — the
 * note says so and the table shows it; nothing is hidden.
 */

"use client";

import * as React from "react";
import { fa } from "@/lib/utils";
import { formatIsoDate } from "@/lib/format-date";
import { formatIsoDateTime } from "@/lib/format-date";
import type { ReportValidationRunResponse } from "@/lib/api-client";
import {
  Badge,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  DataTable,
  StatusBadge,
  type DataTableColumn,
} from "@/components/ui";
import { validationOutcomeLabel, validationOutcomeTone } from "./labels";

function CoverageStat({ label, value, hint }: { label: string; value: number; hint: string }) {
  return (
    <div className="rounded-lg border p-4">
      <p className="text-sm text-muted-foreground">{label}</p>
      <p className="mt-1 text-2xl font-bold tabular-nums">{fa(value)}</p>
      <p className="mt-1 text-xs text-muted-foreground">{hint}</p>
    </div>
  );
}

export function ValidationRunResult({ run }: { run: ReportValidationRunResponse }) {
  const inner = run.run;
  const resultColumns: DataTableColumn<(typeof inner.results)[number]>[] = [
    { key: "rule", header: "قاعده", render: (row) => <span dir="ltr" className="tabular-nums">{row.rule_id}</span> },
    {
      key: "standard",
      header: "استاندارد",
      render: (row) => fa(row.standard_code),
    },
    {
      key: "outcome",
      header: "نتیجه",
      render: (row) => (
        <StatusBadge
          status={row.outcome}
          label={validationOutcomeLabel(row.outcome)}
          tone={validationOutcomeTone(row.outcome)}
        />
      ),
    },
    { key: "message", header: "پیام", render: (row) => <span className="text-sm">{row.message}</span> },
  ];

  return (
    <div className="space-y-6" data-testid="validation-run-result">
      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-center gap-2">
            <CardTitle>نتیجهٔ اعتبارسنجی</CardTitle>
            <StatusBadge
              status={inner.overall_outcome}
              label={validationOutcomeLabel(inner.overall_outcome)}
              tone={validationOutcomeTone(inner.overall_outcome)}
            />
            {inner.is_valid ? null : (
              <Badge variant="outline">نامعتبر — حداقل یک قاعدهٔ خطادار یا ردشده وجود دارد</Badge>
            )}
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          <dl className="grid grid-cols-2 gap-4 text-sm md:grid-cols-4">
            <div>
              <dt className="text-muted-foreground">اجراشده</dt>
              <dd className="mt-1 text-lg font-bold tabular-nums">{fa(inner.rules_executed)}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">قبول</dt>
              <dd className="mt-1 text-lg font-bold tabular-nums">{fa(inner.rules_passed)}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">ردشده</dt>
              <dd className="mt-1 text-lg font-bold tabular-nums">{fa(inner.rules_failed)}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">خطادار</dt>
              <dd className="mt-1 text-lg font-bold tabular-nums">{fa(inner.rules_errored)}</dd>
            </div>
          </dl>
          <p className="text-sm text-muted-foreground">
            دوره: {formatIsoDate(inner.period_start)} تا {formatIsoDate(inner.period_end)} · نسخهٔ قواعد:{" "}
            <span dir="ltr">{inner.ruleset_version}</span> · اجرا: {formatIsoDateTime(inner.created_at)}
          </p>
          {run.note ? (
            <p className="rounded-lg border border-warning/40 bg-warning/10 px-4 py-3 text-sm text-warning-foreground" role="note">
              {run.note}
            </p>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>پوشش</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
            <CoverageStat label="استانداردهای ثبت‌شده" value={inner.coverage.corpus_standards} hint="منبع رجیستری" />
            <CoverageStat label="استانداردهای دارای قاعده" value={inner.coverage.standards_with_rules} hint="دارای قاعدهٔ ماشین‌خوان" />
            <CoverageStat label="قواعد پیاده‌شده" value={inner.coverage.rules_implemented} hint="در رجیستری ثبت شده" />
            <CoverageStat label="قواعد تأییدشده" value={inner.coverage.rules_verified} hint="بازبینی متن تأییدشده" />
            <CoverageStat label="قواعد اجراشده" value={inner.coverage.rules_executed} hint="در همین اجرا ارزیابی شد" />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>نتایج هر قاعده</CardTitle>
        </CardHeader>
        <CardContent>
          <DataTable
            columns={resultColumns}
            rows={inner.results}
            rowKey={(row) => `${row.standard_code}-${row.rule_id}`}
            emptyText="در این اجرا قاعده‌ای ارزیابی نشد."
            caption="قاعده‌هایی که دادهٔ لازم را ندارند با وضعیت «خطا» ثبت می‌شوند، نه «قبول»."
          />
        </CardContent>
      </Card>

      {inner.not_executed.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle>قواعد پیاده‌شده اما اجرا‌نشده</CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="space-y-2 text-sm">
              {inner.not_executed.map((item) => (
                <li key={`${item.standard_code}-${item.rule_id}`} className="flex flex-wrap gap-2">
                  <span dir="ltr" className="font-medium tabular-nums">{item.rule_id}</span>
                  <span className="text-muted-foreground">{item.reason}</span>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>منشأ ارقام</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid gap-2 text-sm md:grid-cols-2">
            <div>
              <dt className="text-muted-foreground">شناسهٔ تصویر تراز</dt>
              <dd dir="ltr" className="break-all tabular-nums">{run.provenance.snapshot_id}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">منبع داده</dt>
              <dd dir="ltr">{run.provenance.source_endpoint}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">تا تاریخ</dt>
              <dd>{formatIsoDate(run.provenance.as_of_date)}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">تراز آزمایشی</dt>
              <dd>
                بدهکار {fa(run.provenance.total_debit)} · بستانکار {fa(run.provenance.total_credit)} ·{" "}
                {run.provenance.balanced ? "متوازن" : "نامتوازن"}
              </dd>
            </div>
            <div className="md:col-span-2">
              <dt className="text-muted-foreground">اثرانگشت داده (SHA-256)</dt>
              <dd dir="ltr" className="break-all text-xs tabular-nums">{run.provenance.payload_hash}</dd>
            </div>
          </dl>
        </CardContent>
      </Card>
    </div>
  );
}
