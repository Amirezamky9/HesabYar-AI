/**
 * Shared manual-import batch detail (US-554).
 *
 * One cycle view for statement (B1) + settlement (P2) batches: the
 * stage timeline, the accounting-effect preview, the variance report
 * (expected vs imported vs posted), and the cycle actions — approve
 * (draft by default; auto-post is an explicit opt-in for settlements),
 * reject with a reason (archives the batch), re-run (archived batches
 * only), and retry-posting (approved settlement batches).
 *
 * Used by the banking batch page (`banking/statements/imports/[id]`)
 * and the settlement batch section of the import detail page.
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowRight } from "lucide-react";
import {
  ApiError,
  api,
  type BatchVarianceReportResponse,
  type ManualImportBatchResponse,
} from "@/lib/api-client";
import { formatIsoDateTime } from "@/lib/format-date";
import { formatMoney } from "@/lib/format-money";
import { fa, faNumber } from "@/lib/utils";
import {
  Button,
  buttonVariants,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Checkbox,
  Label,
  Skeleton,
  StatusBadge,
  Textarea,
} from "@/components/ui";
import {
  isNotFoundError,
  isPermissionDenied,
  persianApiErrorMessage,
} from "@/features/shared/api-error";
import { ErrorState, NotFoundState, PermissionDeniedState } from "@/features/shared/states";
import {
  batchKindLabel,
  batchStageLabel,
  batchStatusLabel,
  batchStatusTone,
  postingStatusLabel,
  postingStatusTone,
} from "./import-batch-data";

export function ImportBatchDetail({
  batchId,
  backHref,
  backLabel,
  importHref,
  importLabel,
}: {
  batchId: string;
  backHref: string;
  backLabel: string;
  importHref?: string | null;
  importLabel?: string;
}) {
  const router = useRouter();
  const [batch, setBatch] = React.useState<ManualImportBatchResponse | null>(null);
  const [variance, setVariance] = React.useState<BatchVarianceReportResponse | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<unknown>(null);
  const [busy, setBusy] = React.useState(false);
  const [actionError, setActionError] = React.useState<string | null>(null);
  const [rejectReason, setRejectReason] = React.useState("");
  const [autoPost, setAutoPost] = React.useState(false);
  const [reloadKey, setReloadKey] = React.useState(0);

  const load = React.useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [loaded, report] = await Promise.all([
        api.getImportBatch(batchId),
        api.getImportBatchVariance(batchId),
      ]);
      setBatch(loaded);
      setVariance(report);
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [batchId, router]);

  // Loading state is set by load() from a timer callback, never
  // synchronously inside this effect (react-hooks/set-state-in-effect).
  React.useEffect(() => {
    const timer = setTimeout(() => {
      void load();
    }, 0);
    return () => clearTimeout(timer);
  }, [load, reloadKey]);

  const handleApprove = async () => {
    if (!batch) return;
    setBusy(true);
    setActionError(null);
    try {
      await api.approveImportBatch(batch.id, {
        auto_post: batch.kind === "settlement" ? autoPost : false,
      });
      setReloadKey((k) => k + 1);
    } catch (err: unknown) {
      setActionError(persianApiErrorMessage(err, "تأیید بچ ناموفق بود."));
    } finally {
      setBusy(false);
    }
  };

  const handleReject = async () => {
    if (!batch) return;
    if (rejectReason.trim() === "") {
      setActionError("برای رد بچ، دلیل را بنویسید.");
      return;
    }
    setBusy(true);
    setActionError(null);
    try {
      await api.rejectImportBatch(batch.id, { reason: rejectReason.trim() });
      setReloadKey((k) => k + 1);
    } catch (err: unknown) {
      setActionError(persianApiErrorMessage(err, "رد بچ ناموفق بود."));
    } finally {
      setBusy(false);
    }
  };

  const handleRerun = async () => {
    if (!batch) return;
    setBusy(true);
    setActionError(null);
    try {
      await api.rerunImportBatch(batch.id);
      setReloadKey((k) => k + 1);
    } catch (err: unknown) {
      setActionError(persianApiErrorMessage(err, "اجرای دوبارهٔ بچ ناموفق بود."));
    } finally {
      setBusy(false);
    }
  };

  const handleRetryPosting = async () => {
    if (!batch) return;
    setBusy(true);
    setActionError(null);
    try {
      await api.retryBatchPosting(batch.id);
      setReloadKey((k) => k + 1);
    } catch (err: unknown) {
      setActionError(persianApiErrorMessage(err, "تلاش دوبارهٔ ثبت ناموفق بود."));
    } finally {
      setBusy(false);
    }
  };

  if (loading) {
    return (
      <div className="space-y-6" aria-label="در حال بارگذاری">
        <Skeleton className="h-8 w-56" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (error && isNotFoundError(error)) {
    return <NotFoundState message="این بچ ایمپورت پیدا نشد." />;
  }

  if (error && isPermissionDenied(error)) {
    return (
      <PermissionDeniedState
        message={persianApiErrorMessage(error, "مشاهدهٔ بچ ایمپورت ناموفق بود.")}
      />
    );
  }

  if (error || !batch) {
    return (
      <ErrorState
        message={persianApiErrorMessage(error, "مشاهدهٔ بچ ایمپورت ناموفق بود.")}
        onRetry={() => setReloadKey((k) => k + 1)}
      />
    );
  }

  const preview = batch.preview;
  const canApprove = batch.status === "awaiting_review";
  const canReject = batch.status === "awaiting_review";
  const canRerun = batch.status === "archived" || batch.status === "rejected";
  const canRetryPosting =
    batch.kind === "settlement" && batch.status === "approved";

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <Link
          href={backHref}
          className={buttonVariants({ variant: "ghost", size: "icon" })}
          aria-label={backLabel}
        >
          <ArrowRight className="size-4" aria-hidden />
        </Link>
        <div>
          <h1 className="text-2xl font-bold">بچ ایمپورت {batchKindLabel(batch.kind)}</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            <span dir="ltr">{batch.filename}</span> · تلاش {fa(batch.attempt)} ·{" "}
            {batch.file_type ? `نوع فایل: ${batch.file_type} · ` : ""}
            {batch.period_jalali ? `دورهٔ ${fa(batch.period_jalali)}` : ""}
          </p>
        </div>
        <StatusBadge
          status={batch.status}
          label={batchStatusLabel(batch.status)}
          tone={batchStatusTone(batch.status)}
        />
      </div>

      {actionError ? (
        <ErrorState message={actionError} onRetry={() => setActionError(null)} />
      ) : null}

      {batch.rejection_reason ? (
        <Card>
          <CardHeader>
            <CardTitle>دلیل رد</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm">{batch.rejection_reason}</p>
            <p className="mt-2 text-sm text-muted-foreground">
              ردیف‌ها نگه داشته شده‌اند؛ بچ را می‌توان دوباره اجرا کرد.
            </p>
          </CardContent>
        </Card>
      ) : null}

      {preview ? (
        <Card>
          <CardHeader>
            <CardTitle>پیش‌نمایش اثر حسابداری</CardTitle>
            <CardDescription>
              {faNumber(preview.matched_rows)} ردیف منطبق · {faNumber(preview.new_rows)}{" "}
              ردیف جدید · {faNumber(preview.duplicate_rows)} تکراری ·{" "}
              {faNumber(preview.invalid_rows)} نامعتبر · {faNumber(preview.dropped_rows)}{" "}
              ردیف جمع حذف‌شده
            </CardDescription>
          </CardHeader>
          <CardContent>
            <dl className="grid gap-4 sm:grid-cols-3">
              <div>
                <dt className="text-sm text-muted-foreground">مجموع ناخالص</dt>
                <dd className="mt-1 font-medium tabular-nums">{formatMoney(preview.gross_irr)}</dd>
              </div>
              {batch.kind === "settlement" ? (
                <>
                  <div>
                    <dt className="text-sm text-muted-foreground">کارمزد</dt>
                    <dd className="mt-1 font-medium tabular-nums">{formatMoney(preview.fee_irr)}</dd>
                  </div>
                  <div>
                    <dt className="text-sm text-muted-foreground">مالیات کارمزد</dt>
                    <dd className="mt-1 font-medium tabular-nums">{formatMoney(preview.tax_irr)}</dd>
                  </div>
                  <div>
                    <dt className="text-sm text-muted-foreground">برگشت‌ها</dt>
                    <dd className="mt-1 font-medium tabular-nums">{formatMoney(preview.refunds_irr)}</dd>
                  </div>
                  <div>
                    <dt className="text-sm text-muted-foreground">کسورات مستند</dt>
                    <dd className="mt-1 font-medium tabular-nums">{formatMoney(preview.deductions_irr)}</dd>
                  </div>
                </>
              ) : null}
              <div>
                <dt className="text-sm text-muted-foreground">خالص</dt>
                <dd className="mt-1 font-medium tabular-nums">{formatMoney(preview.net_irr)}</dd>
              </div>
              <div>
                <dt className="text-sm text-muted-foreground">واریز موردانتظار</dt>
                <dd className="mt-1 font-medium tabular-nums">{formatMoney(preview.expected_deposit_irr)}</dd>
              </div>
              {preview.receivable_before_irr !== null ? (
                <div>
                  <dt className="text-sm text-muted-foreground">طلب باز کانال — پیش از بچ</dt>
                  <dd className="mt-1 font-medium tabular-nums">{formatMoney(preview.receivable_before_irr)}</dd>
                </div>
              ) : null}
              {preview.receivable_after_irr !== null ? (
                <div>
                  <dt className="text-sm text-muted-foreground">طلب باز کانال — پس از بچ</dt>
                  <dd className="mt-1 font-medium tabular-nums">{formatMoney(preview.receivable_after_irr)}</dd>
                </div>
              ) : null}
            </dl>
          </CardContent>
        </Card>
      ) : null}

      {variance ? (
        <Card>
          <CardHeader>
            <CardTitle>گزارش مغایرت بچ</CardTitle>
            <CardDescription>
              موردانتظار در برابر ایمپورت‌شده در برابر ثبت‌شده — پولی که ایمپورت شده
              ولی هنوز ثبت نشده است، همین‌جا دیده می‌شود.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <dl className="grid gap-4 sm:grid-cols-3">
              <div>
                <dt className="text-sm text-muted-foreground">موردانتظار</dt>
                <dd className="mt-1 font-medium tabular-nums">{formatMoney(variance.expected_net_irr)}</dd>
              </div>
              <div>
                <dt className="text-sm text-muted-foreground">ایمپورت‌شده</dt>
                <dd className="mt-1 font-medium tabular-nums">{formatMoney(variance.imported_net_irr)}</dd>
              </div>
              <div>
                <dt className="text-sm text-muted-foreground">ثبت‌شده</dt>
                <dd className="mt-1 font-medium tabular-nums">{formatMoney(variance.posted_net_irr)}</dd>
              </div>
              <div>
                <dt className="text-sm text-muted-foreground">اختلاف (ایمپورت‌شده − ثبت‌شده)</dt>
                <dd className="mt-1 font-medium tabular-nums">{formatMoney(variance.difference_irr)}</dd>
              </div>
              <div>
                <dt className="text-sm text-muted-foreground">تطبیق سه‌طرفه</dt>
                <dd className="mt-1 font-medium">
                  {variance.tieout_holds === null
                    ? "هنوز اجرا نشده"
                    : variance.tieout_holds
                      ? "برقرار است"
                      : `برقرار نیست (اختلاف ${formatMoney(variance.tieout_difference_irr ?? 0)})`}
                </dd>
              </div>
              <div>
                <dt className="text-sm text-muted-foreground">وضعیت ثبت</dt>
                <dd className="mt-1 font-medium">
                  <StatusBadge
                    status={variance.posting_status ?? "unknown"}
                    label={postingStatusLabel(variance.posting_status)}
                    tone={postingStatusTone(variance.posting_status)}
                  />
                </dd>
              </div>
            </dl>
            {variance.gate_reason ? (
              <p className="text-sm text-muted-foreground">{variance.gate_reason}</p>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>مراحل بچ</CardTitle>
          <CardDescription>
            هر مرحله با زمان ورودش ثبت می‌شود؛ رد و اجرای دوباره هم در این تاریخچه
            می‌مانند.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <ol className="space-y-4">
            {batch.stages.map((stage, index) => (
              <li key={`${stage.stage}-${index}`} className="flex items-start gap-3">
                <span
                  className="mt-2 size-2 shrink-0 rounded-full bg-primary"
                  aria-hidden
                />
                <div>
                  <p className="font-medium">{batchStageLabel(stage.stage)}</p>
                  <p className="text-sm text-muted-foreground">
                    {formatIsoDateTime(stage.at)}
                    {stage.note ? ` — ${stage.note}` : ""}
                  </p>
                </div>
              </li>
            ))}
          </ol>
        </CardContent>
      </Card>

      {canApprove || canReject || canRerun || canRetryPosting ? (
        <Card>
          <CardHeader>
            <CardTitle>اقدام روی بچ</CardTitle>
          </CardHeader>
          <CardContent className="space-y-6">
            {batch.kind === "settlement" && canApprove ? (
              <div className="flex items-center gap-2">
                <Checkbox
                  id="batch-auto-post"
                  checked={autoPost}
                  onCheckedChange={(v) => setAutoPost(v === true)}
                />
                <Label htmlFor="batch-auto-post">
                  پس از تأیید، تطبیق سه‌طرفه اجرا و سند بانکی به‌صورت خودکار ثبت شود
                  (پیش‌فرض: پیش‌نویس)
                </Label>
              </div>
            ) : null}

            {canReject ? (
              <div className="space-y-1.5">
                <Label htmlFor="batch-reject-reason">دلیل رد (برای رد بچ لازم است)</Label>
                <Textarea
                  id="batch-reject-reason"
                  value={rejectReason}
                  onChange={(e) => setRejectReason(e.target.value)}
                  placeholder="چرا این بچ رد می‌شود؟"
                />
              </div>
            ) : null}

            <div className="flex flex-wrap gap-4">
              {canApprove ? (
                <Button onClick={handleApprove} disabled={busy}>
                  تأیید بچ
                </Button>
              ) : null}
              {canReject ? (
                <Button variant="outline" onClick={handleReject} disabled={busy}>
                  رد بچ
                </Button>
              ) : null}
              {canRerun ? (
                <Button onClick={handleRerun} disabled={busy}>
                  اجرای دوبارهٔ بچ
                </Button>
              ) : null}
              {canRetryPosting ? (
                <Button onClick={handleRetryPosting} disabled={busy}>
                  تلاش دوبارهٔ ثبت
                </Button>
              ) : null}
            </div>
          </CardContent>
        </Card>
      ) : null}

      <div className="flex flex-wrap gap-4">
        {batch.review_item_id ? (
          <Link
            href={`/review-queue?item=${batch.review_item_id}`}
            className={buttonVariants({ variant: "outline" })}
          >
            مشاهده در صف بازبینی
          </Link>
        ) : null}
        {importHref ? (
          <Link href={importHref} className={buttonVariants({ variant: "outline" })}>
            {importLabel ?? "مشاهدهٔ ایمپورت"}
          </Link>
        ) : null}
      </div>
    </div>
  );
}
