/**
 * One standard with its revisions and rules (US-405).
 *
 * Live detail from GET /api/v1/standards/{code}: full §12.1 provenance
 * per revision (authority, source URL, snapshot, SHA-256, verified
 * by/at) and the machine-readable rules with their DSL formulas.
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowRight } from "lucide-react";
import { ApiError, api, type StandardDetailResponse } from "@/lib/api-client";
import { fa } from "@/lib/utils";
import { formatIsoDate, formatIsoDateTime } from "@/lib/format-date";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  DataTable,
  Skeleton,
  StatusBadge,
  type DataTableColumn,
} from "@/components/ui";
import { ErrorState, NotFoundState, PermissionDeniedState } from "@/features/shared/states";
import { isNotFoundError, isPermissionDenied, persianApiErrorMessage } from "@/features/shared/api-error";
import { verificationLabel, verificationReason, verificationTone } from "./labels";

const REVISION_COLUMNS: DataTableColumn<NonNullable<StandardDetailResponse["revisions"]>[number]>[] = [
  { key: "revision", header: "بازبینی", render: (row) => <span dir="ltr">{row.revision_id}</span> },
  { key: "title", header: "عنوان", render: (row) => row.title },
  {
    key: "status",
    header: "وضعیت تأیید",
    render: (row) => (
      <StatusBadge
        status={row.verification_status}
        label={verificationLabel(row.verification_status)}
        tone={verificationTone(row.verification_status)}
      />
    ),
  },
  {
    key: "effective",
    header: "اعتبار",
    render: (row) =>
      row.effective_from || row.effective_to
        ? `${formatIsoDate(row.effective_from) || "—"} تا ${formatIsoDate(row.effective_to) || "—"}`
        : "—",
  },
  { key: "state", header: "وضعیت بازبینی", render: (row) => (row.status === "active" ? "فعال" : "جایگزین‌شده") },
];

const RULE_COLUMNS: DataTableColumn<NonNullable<StandardDetailResponse["rules"]>[number]>[] = [
  { key: "rule", header: "قاعده", render: (row) => <span dir="ltr" className="tabular-nums">{row.rule_id}</span> },
  { key: "category", header: "دسته", render: (row) => row.category },
  { key: "description", header: "شرح", render: (row) => <span className="text-sm">{row.description}</span> },
  { key: "type", header: "نوع", render: (row) => row.rule_type },
  { key: "severity", header: "شدت", render: (row) => (row.severity === "error" ? "خطا" : "هشدار") },
  {
    key: "executable",
    header: "قابل اجرا",
    render: (row) => (row.is_executable ? "بله" : "خیر — فرمول ندارد"),
  },
];

export function StandardDetailPage({ standardCode }: { standardCode: string }) {
  const router = useRouter();
  const [detail, setDetail] = React.useState<StandardDetailResponse | null>(null);
  const [error, setError] = React.useState<unknown>(null);
  const [reloadKey, setReloadKey] = React.useState(0);

  React.useEffect(() => {
    let cancelled = false;
    api
      .getStandard(standardCode)
      .then((data) => {
        if (cancelled) return;
        setDetail(data);
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
  }, [standardCode, reloadKey, router]);

  if (error && isNotFoundError(error)) {
    return <NotFoundState message={`استاندارد ${fa(standardCode)} در رجیستری پیدا نشد.`} />;
  }

  if (error && isPermissionDenied(error)) {
    return <PermissionDeniedState message={persianApiErrorMessage(error, "دریافت استاندارد ناموفق بود.")} />;
  }

  if (error) {
    return (
      <ErrorState
        message={persianApiErrorMessage(error, "دریافت استاندارد ناموفق بود.")}
        onRetry={() => setReloadKey((k) => k + 1)}
      />
    );
  }

  if (detail === null) {
    return (
      <div className="space-y-6" aria-label="در حال بارگذاری">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-64" />
      </div>
    );
  }

  const latest = detail.revisions[0];
  const blockedReason = latest ? verificationReason(latest.verification_status) : null;

  return (
    <div className="space-y-6" data-testid="standard-detail-page">
      <div>
        <Link
          href="/compliance/standards"
          className="inline-flex min-h-11 items-center gap-1 text-sm font-medium text-primary hover:underline"
        >
          <ArrowRight className="size-4" strokeWidth={2} aria-hidden />
          بازگشت به فهرست استانداردها
        </Link>
        <h1 className="mt-2 text-2xl font-bold">
          استاندارد {fa(detail.standard_code)} — {detail.title}
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {detail.ifrs_equivalent ? (
            <>
              معادل بین‌المللی: <span dir="ltr">{detail.ifrs_equivalent}</span> ·{" "}
            </>
          ) : null}
          {detail.source_authority ?? "مرجع صدور ثبت نشده است."}
        </p>
      </div>

      {latest ? (
        <div className="flex flex-wrap items-center gap-2" data-testid="standard-verification">
          <StatusBadge
            status={latest.verification_status}
            label={verificationLabel(latest.verification_status)}
            tone={verificationTone(latest.verification_status)}
          />
          {blockedReason ? <p className="text-sm text-muted-foreground">{blockedReason}</p> : null}
        </div>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>بازبینی‌ها و منشأ</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <DataTable
            columns={REVISION_COLUMNS}
            rows={detail.revisions}
            rowKey={(row) => row.revision_id}
            emptyText="برای این استاندارد بازبینی‌ای ثبت نشده است."
            caption="هر بازبینی، متن ثبت‌شدهٔ آن استاندارد با منشأ کامل است."
          />
          {detail.revisions.map((revision) => (
            <dl key={revision.revision_id} className="grid gap-2 rounded-lg border p-4 text-sm md:grid-cols-2">
              <div>
                <dt className="text-muted-foreground">بازبینی</dt>
                <dd dir="ltr">{revision.revision_id}</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">مرجع صدور</dt>
                <dd>{revision.source_authority ?? "—"}</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">نشانی منبع</dt>
                <dd dir="ltr" className="break-all">
                  {revision.source_url ? (
                    <a href={revision.source_url} target="_blank" rel="noreferrer" className="text-primary hover:underline">
                      {revision.source_url}
                    </a>
                  ) : (
                    "—"
                  )}
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">تصویر متن</dt>
                <dd dir="ltr" className="break-all">{revision.snapshot_uri ?? "—"}</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">اثرانگشت متن (SHA-256)</dt>
                <dd dir="ltr" className="break-all text-xs tabular-nums">{revision.sha256 ?? "—"}</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">تأییدکننده / زمان تأیید</dt>
                <dd>
                  {revision.verified_by ?? "—"}
                  {revision.verified_at ? ` · ${formatIsoDateTime(revision.verified_at)}` : ""}
                </dd>
              </div>
              {revision.notes ? (
                <div className="md:col-span-2">
                  <dt className="text-muted-foreground">یادداشت</dt>
                  <dd>{revision.notes}</dd>
                </div>
              ) : null}
            </dl>
          ))}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>قواعد ماشین‌خوان ({fa(detail.rules.length)})</CardTitle>
        </CardHeader>
        <CardContent>
          <DataTable
            columns={RULE_COLUMNS}
            rows={detail.rules}
            rowKey={(row) => row.rule_id}
            emptyText="این استاندارد هنوز قاعدهٔ ماشین‌خوان ندارد؛ فقط متن مرجع است."
            caption="قاعده‌های بدون فرمول (مانند الزام افشا) قابل اجرا نیستند و در اعتبارسنجی «اجرا‌نشده» گزارش می‌شوند."
          />
        </CardContent>
      </Card>
    </div>
  );
}
