/**
 * Bank statement detail panel (US-304, reconcile in US-514).
 *
 * One statement from GET /api/v1/bank/statements/{id}: header, balances
 * and lines (signed amounts: positive = money in). A draft statement can
 * be reconciled (POST /bank/statements/{id}/reconcile): 3-priority
 * matching — exact reference, amount + date, smart suggestion. Unmatched
 * lines get a manual-match form; the statement is never auto-validated
 * or auto-posted by reconciliation.
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowRight } from "lucide-react";
import { ApiError, api, type BankStatementResponse, type ReconcileResponse } from "@/lib/api-client";
import { formatIsoDate } from "@/lib/format-date";
import { formatMoney } from "@/lib/format-money";
import { fa } from "@/lib/utils";
import {
  Button,
  buttonVariants,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Checkbox,
  DataTable,
  Input,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Skeleton,
  StatusBadge,
  type DataTableColumn,
} from "@/components/ui";
import {
  isNotFoundError,
  isPermissionDenied,
  persianApiErrorMessage,
} from "@/features/shared/api-error";
import { ErrorState, NotFoundState, PermissionDeniedState } from "@/features/shared/states";
import { statementValidationLabel } from "./labels";

const LINE_COLUMNS: DataTableColumn<BankStatementResponse["lines"][number]>[] = [
  { key: "date", header: "تاریخ", render: (line) => formatIsoDate(line.date) },
  { key: "description", header: "شرح", render: (line) => line.description || "—" },
  { key: "party", header: "طرف حساب", render: (line) => line.party_name ?? "—" },
  {
    key: "amount",
    header: "مبلغ",
    render: (line) => (
      <span className="tabular-nums">
        {line.amount < 0 ? "−" : ""}
        {formatMoney(Math.abs(line.amount))}
      </span>
    ),
    className: "text-end",
    headerClassName: "text-end",
  },
  {
    key: "matched",
    header: "تطبیق",
    render: (line) =>
      line.related_invoice_id !== null ? (
        <Link href={`/invoices/${line.related_invoice_id}`} className="text-primary underline underline-offset-4">
          فاکتور {fa(line.related_invoice_id)}
        </Link>
      ) : (
        "تطبیق‌نیافته"
      ),
  },
];

const OUTCOME_LABELS: Record<string, string> = {
  already_matched: "قبلاً تطبیق‌یافته",
  matched: "تطبیق‌یافته",
  suggested: "پیشنهاد شده",
  ambiguous: "مبهم",
  unmatched: "تطبیق‌نیافته",
  skipped_zero: "رد شد (مبلغ صفر)",
};

const RECONCILE_RESULT_COLUMNS: DataTableColumn<ReconcileResponse["lines"][number]>[] = [
  { key: "date", header: "تاریخ", render: (line) => formatIsoDate(line.line_date) },
  { key: "description", header: "شرح", render: (line) => line.description || "—" },
  {
    key: "amount",
    header: "مبلغ",
    render: (line) => (
      <span className="tabular-nums">
        {line.amount_irr < 0 ? "−" : ""}
        {formatMoney(Math.abs(line.amount_irr))}
      </span>
    ),
    className: "text-end",
    headerClassName: "text-end",
  },
  {
    key: "outcome",
    header: "نتیجه",
    render: (line) => OUTCOME_LABELS[line.outcome] ?? line.outcome,
  },
  {
    key: "target",
    header: "هدف",
    render: (line) => {
      if (line.target_label) {
        return (
          <>
            {line.target_label}
            {line.confidence !== null ? ` (${fa(line.confidence)}٪)` : ""}
          </>
        );
      }
      if (line.suggestions.length > 0) {
        return line.suggestions
          .map((s) => `${s.target_label} (${fa(s.confidence)}٪)`)
          .join("، ");
      }
      return "—";
    },
  },
];

export function StatementDetail({ statementId }: { statementId: number }) {
  const router = useRouter();
  const [statement, setStatement] = React.useState<BankStatementResponse | null>(null);
  const [error, setError] = React.useState<unknown>(null);
  const [reloadKey, setReloadKey] = React.useState(0);
  const [result, setResult] = React.useState<ReconcileResponse | null>(null);
  const [reconciling, setReconciling] = React.useState(false);
  const [reconcileError, setReconcileError] = React.useState<unknown>(null);
  const [matchLineId, setMatchLineId] = React.useState<number | null>(null);
  const [matchTargetType, setMatchTargetType] = React.useState<string>("invoice");
  const [matchTargetId, setMatchTargetId] = React.useState("");
  const [matchAccountId, setMatchAccountId] = React.useState("");
  const [matchSaveAsRule, setMatchSaveAsRule] = React.useState(false);
  const [matching, setMatching] = React.useState(false);

  const handleReconcile = async () => {
    setReconciling(true);
    setReconcileError(null);
    try {
      const data = await api.reconcileStatement(statementId);
      setResult(data);
      setReloadKey((k) => k + 1);
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setReconcileError(err);
    } finally {
      setReconciling(false);
    }
  };

  const handleManualMatch = async () => {
    if (matchLineId === null) return;
    setMatching(true);
    setReconcileError(null);
    try {
      await api.applyManualMatch(statementId, matchLineId, {
        target_type: matchTargetType,
        target_id: matchTargetType === "manual" ? "manual" : matchTargetId.trim(),
        ...(matchAccountId.trim() ? { account_id: Number(matchAccountId.trim()) } : {}),
        save_as_rule: matchSaveAsRule,
      });
      setMatchLineId(null);
      setMatchTargetId("");
      setMatchAccountId("");
      setMatchSaveAsRule(false);
      setResult(null);
      setReloadKey((k) => k + 1);
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setReconcileError(err);
    } finally {
      setMatching(false);
    }
  };

  React.useEffect(() => {
    let cancelled = false;
    api
      .getStatement(statementId)
      .then((data) => {
        if (!cancelled) setStatement(data);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.replace("/login");
          return;
        }
        setError(err);
      })
      .finally(() => {
        /* loading is derived: null record + no error = loading */
      });
    return () => {
      cancelled = true;
    };
  }, [statementId, reloadKey, router]);

  const loading = statement === null && error === null;

  if (loading) {
    return (
      <div className="space-y-6" aria-label="در حال بارگذاری">
        <Skeleton className="h-8 w-56" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (error && isPermissionDenied(error)) {
    return <PermissionDeniedState message={persianApiErrorMessage(error, "دریافت صورت‌حساب ناموفق بود.")} />;
  }

  if (error && isNotFoundError(error)) {
    return <NotFoundState message="صورت‌حساب موردنظر پیدا نشد." />;
  }

  if (error || !statement) {
    return (
      <ErrorState
        message={persianApiErrorMessage(error, "دریافت صورت‌حساب ناموفق بود.")}
        onRetry={() => {
          setError(null);
          setStatement(null);
          setReloadKey((k) => k + 1);
        }}
      />
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          {/* Plain Link styled as a button: Base UI's Button with
              render={<Link>} logs a nativeButton console error (US-305). */}
          <Link href="/banking/statements" className={buttonVariants({ variant: "ghost", size: "icon" })} aria-label="بازگشت به صورت‌حساب‌های بانکی">
            <ArrowRight className="size-4" aria-hidden />
          </Link>
          <div>
            <h1 className="text-2xl font-bold">{statement.name}</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              {statement.journal_name}
              {" · "}
              {formatIsoDate(statement.date)}
            </p>
          </div>
        </div>
        <StatusBadge status={statement.state} />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>مشخصات صورت‌حساب</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid gap-4 sm:grid-cols-2">
            <div>
              <dt className="text-sm text-muted-foreground">نوع اعتبارسنجی</dt>
              <dd className="mt-1 font-medium">{statementValidationLabel(statement.validation)}</dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">تعداد ردیف‌ها</dt>
              <dd className="mt-1 font-medium tabular-nums">{fa(statement.lines.length)}</dd>
            </div>
            {statement.start_balance !== null ? (
              <div>
                <dt className="text-sm text-muted-foreground">ماندهٔ اول</dt>
                <dd className="mt-1 font-medium tabular-nums">{formatMoney(statement.start_balance)}</dd>
              </div>
            ) : null}
            {statement.end_balance !== null ? (
              <div>
                <dt className="text-sm text-muted-foreground">ماندهٔ آخر</dt>
                <dd className="mt-1 font-medium tabular-nums">{formatMoney(statement.end_balance)}</dd>
              </div>
            ) : null}
            {statement.balance !== null ? (
              <div>
                <dt className="text-sm text-muted-foreground">مانده</dt>
                <dd className="mt-1 font-medium tabular-nums">{formatMoney(statement.balance)}</dd>
              </div>
            ) : null}
            {statement.total_amount !== null ? (
              <div>
                <dt className="text-sm text-muted-foreground">مبلغ کل</dt>
                <dd className="mt-1 font-medium tabular-nums">{formatMoney(statement.total_amount)}</dd>
              </div>
            ) : null}
          </dl>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>ردیف‌های صورت‌حساب</CardTitle>
        </CardHeader>
        <CardContent>
          <DataTable
            columns={LINE_COLUMNS}
            rows={statement.lines}
            rowKey={(line) => line.id}
            emptyText="این صورت‌حساب ردیفی ندارد."
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>تطبیق صورت‌حساب</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {statement.state !== "draft" ? (
            <p className="text-sm text-muted-foreground">
              فقط صورت‌حساب‌های پیش‌نویس تطبیق داده می‌شوند؛ این صورت‌حساب در وضعیت
              «{statement.state}» است.
            </p>
          ) : (
            <>
              <p className="text-sm text-muted-foreground">
                تطبیق سه‌اولویتی: شماره سند دقیق، مبلغ و تاریخ (±۲ روز)، و پیشنهاد
                هوشمند با اطمینان که نیاز به تأیید شما دارد. واریزهای تطبیق‌نیافته
                هرگز به‌صورت خودکار به درآمد ثبت نمی‌شوند؛ به صف بازبینی می‌روند.
                تطبیق، صورت‌حساب را تأیید یا ثبت نمی‌کند.
              </p>
              <Button onClick={handleReconcile} disabled={reconciling}>
                {reconciling ? "در حال تطبیق…" : "تطبیق صورت‌حساب"}
              </Button>
            </>
          )}
          {reconcileError ? (
            <ErrorState
              message={persianApiErrorMessage(reconcileError, "تطبیق ناموفق بود.")}
              onRetry={() => setReconcileError(null)}
            />
          ) : null}
          {result ? (
            <div className="space-y-4">
              <dl className="grid gap-4 sm:grid-cols-4">
                <div>
                  <dt className="text-sm text-muted-foreground">تطبیق‌یافته</dt>
                  <dd className="mt-1 font-medium tabular-nums">
                    {fa(result.matched_count + result.already_matched_count)}
                  </dd>
                </div>
                <div>
                  <dt className="text-sm text-muted-foreground">پیشنهاد شده</dt>
                  <dd className="mt-1 font-medium tabular-nums">
                    {fa(result.suggested_count)}
                  </dd>
                </div>
                <div>
                  <dt className="text-sm text-muted-foreground">مبهم</dt>
                  <dd className="mt-1 font-medium tabular-nums">
                    {fa(result.ambiguous_count)}
                  </dd>
                </div>
                <div>
                  <dt className="text-sm text-muted-foreground">تطبیق‌نیافته</dt>
                  <dd className="mt-1 font-medium tabular-nums">
                    {fa(result.unmatched_count)}
                  </dd>
                </div>
              </dl>
              <DataTable
                columns={RECONCILE_RESULT_COLUMNS}
                rows={result.lines}
                rowKey={(line) => line.line_id}
                emptyText="این صورت‌حساب ردیفی ندارد."
              />
              {result.lines.some((l) =>
                ["suggested", "ambiguous", "unmatched"].includes(l.outcome),
              ) ? (
                <div className="space-y-4 border-t border-border pt-4">
                  <h3 className="text-base font-semibold">تطبیق دستی یک ردیف</h3>
                  <p className="text-sm text-muted-foreground">
                    ردیف را انتخاب کنید و هدف تطبیق را مشخص کنید. با فعال‌کردن «ذخیره
                    به‌عنوان قاعده»، این نگاشت برای ردیف‌های مشابه آینده یاد گرفته
                    می‌شود.
                  </p>
                  <div className="grid gap-4 sm:grid-cols-2">
                    <div className="space-y-1.5">
                      <Label htmlFor="match-line">ردیف</Label>
                      <Select
                        value={matchLineId === null ? "" : String(matchLineId)}
                        onValueChange={(v) => setMatchLineId(v ? Number(v) : null)}
                      >
                        <SelectTrigger id="match-line">
                          <SelectValue placeholder="یک ردیف انتخاب کنید" />
                        </SelectTrigger>
                        <SelectContent>
                          {result.lines
                            .filter((l) =>
                              ["suggested", "ambiguous", "unmatched"].includes(
                                l.outcome,
                              ),
                            )
                            .map((l) => (
                              <SelectItem key={l.line_id} value={String(l.line_id)}>
                                {formatIsoDate(l.line_date)} — {l.description} —{" "}
                                {formatMoney(Math.abs(l.amount_irr))}
                              </SelectItem>
                            ))}
                        </SelectContent>
                      </Select>
                    </div>
                    <div className="space-y-1.5">
                      <Label htmlFor="match-target-type">نوع هدف</Label>
                      <Select
                        value={matchTargetType}
                        onValueChange={(v) => setMatchTargetType(v ?? "invoice")}
                      >
                        <SelectTrigger id="match-target-type">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="invoice">فاکتور</SelectItem>
                          <SelectItem value="payment">پرداخت</SelectItem>
                          <SelectItem value="manual">کدینگ دستی</SelectItem>
                        </SelectContent>
                      </Select>
                    </div>
                    {matchTargetType !== "manual" ? (
                      <div className="space-y-1.5">
                        <Label htmlFor="match-target-id">
                          {matchTargetType === "invoice"
                            ? "شمارهٔ فاکتور"
                            : "شمارهٔ پرداخت"}
                        </Label>
                        <Input
                          id="match-target-id"
                          inputMode="numeric"
                          dir="ltr"
                          value={matchTargetId}
                          onChange={(e) => setMatchTargetId(e.target.value)}
                          placeholder="مثلاً ۱۲۳"
                        />
                      </div>
                    ) : null}
                    {matchTargetType !== "invoice" ? (
                      <div className="space-y-1.5">
                        <Label htmlFor="match-account-id">حساب کدینگ</Label>
                        <Input
                          id="match-account-id"
                          inputMode="numeric"
                          dir="ltr"
                          value={matchAccountId}
                          onChange={(e) => setMatchAccountId(e.target.value)}
                          placeholder="شمارهٔ حساب"
                        />
                      </div>
                    ) : null}
                  </div>
                  <label className="flex min-h-11 cursor-pointer items-center gap-2 text-sm">
                    <Checkbox
                      checked={matchSaveAsRule}
                      onCheckedChange={(v) => setMatchSaveAsRule(v === true)}
                    />
                    ذخیره به‌عنوان قاعده برای ردیف‌های مشابه آینده
                  </label>
                  <Button
                    onClick={handleManualMatch}
                    disabled={
                      matching ||
                      matchLineId === null ||
                      (matchTargetType !== "manual" && matchTargetId.trim() === "") ||
                      (matchTargetType !== "invoice" && matchAccountId.trim() === "")
                    }
                  >
                    {matching ? "در حال ثبت…" : "ثبت تطبیق دستی"}
                  </Button>
                </div>
              ) : null}
            </div>
          ) : null}
        </CardContent>
      </Card>
    </div>
  );
}
