/**
 * Settlement file import detail (US-525) — one import batch: its rows
 * with dry-run match outcomes, approve/reject, bank-deposit recording
 * and the mandatory three-way tie-out.
 *
 * The tie-out needs a company to post (without one it computes but
 * never posts); the policy gate decides auto-post vs draft-hold, and
 * the run's figures + gate reason render here honestly either way.
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowRight } from "lucide-react";
import {
  ApiError,
  api,
  type BankDepositResponse,
  type ManualImportBatchResponse,
  type RecordDepositResponse,
  type SettlementEventResponse,
  type SettlementFileImportDetailResponse,
  type TieOutRunResponse,
} from "@/lib/api-client";
import { formatIsoDate } from "@/lib/format-date";
import { formatMoney, tomanToIrr } from "@/lib/format-money";
import { faNumber } from "@/lib/utils";
import {
  AmountInput,
  Button,
  buttonVariants,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  DataTable,
  Input,
  JalaliDatePicker,
  Label,
  Skeleton,
  StatusBadge,
  Textarea,
  type DataTableColumn,
} from "@/components/ui";
import {
  isNotFoundError,
  isPermissionDenied,
  persianApiErrorMessage,
} from "@/features/shared/api-error";
import { CompanyPicker } from "@/features/shared/company-picker";
import { ErrorState, NotFoundState, PermissionDeniedState } from "@/features/shared/states";
import { useCompany } from "@/features/shared/use-company";
import {
  batchStageLabel,
  batchStatusLabel,
  batchStatusTone,
} from "@/features/imports/import-batch-data";
import {
  channelNameOf,
  channelsById,
  type ChannelsById,
} from "./settlement-report";
import {
  depositDecisionLabel,
  fileTypeLabel,
  importStatusLabel,
  importStatusTone,
  jalaliPeriodLabel,
  todayIsoTehran,
} from "./settlement-data";
import { SETTLEMENT_IMPORT_ROW_COLUMNS } from "./settlement-import";

export function SettlementImportDetail({ importId }: { importId: string }) {
  const router = useRouter();
  const { companies, companyId, setCompanyId, loading: companiesLoading } = useCompany();
  const [detail, setDetail] = React.useState<SettlementFileImportDetailResponse | null>(null);
  const [batch, setBatch] = React.useState<ManualImportBatchResponse | null>(null);
  const [channelsMap, setChannelsMap] = React.useState<ChannelsById>(new Map());
  const [events, setEvents] = React.useState<SettlementEventResponse[]>([]);
  const [deposits, setDeposits] = React.useState<BankDepositResponse[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<unknown>(null);
  const [busy, setBusy] = React.useState(false);
  const [actionError, setActionError] = React.useState<string | null>(null);
  const [rejectReason, setRejectReason] = React.useState("");
  const [approved, setApproved] = React.useState<{
    confirmed_tranche_ids: string[];
    net_irr: number;
  } | null>(null);
  const [tieOut, setTieOut] = React.useState<TieOutRunResponse | null>(null);
  const [reloadKey, setReloadKey] = React.useState(0);

  // Deposit form (amounts the operator types are Toman).
  const [depositDate, setDepositDate] = React.useState<string | null>(todayIsoTehran());
  const [depositAmountToman, setDepositAmountToman] = React.useState<number | null>(null);
  const [depositBankRef, setDepositBankRef] = React.useState("");
  const [depositDescription, setDepositDescription] = React.useState("");
  const [depositTrackingRef, setDepositTrackingRef] = React.useState("");
  const [depositResult, setDepositResult] = React.useState<RecordDepositResponse | null>(null);
  const [depositError, setDepositError] = React.useState<string | null>(null);
  const [depositBusy, setDepositBusy] = React.useState(false);

  const load = React.useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [importDetail, channelPage] = await Promise.all([
        api.getSettlementImport(importId),
        api.listPaymentChannels({ limit: 100 }),
      ]);
      setDetail(importDetail);
      setChannelsMap(channelsById(channelPage.items));
      // The unified batch wrapping this import (US-554) — imports
      // created before US-554 have none. A lookup failure must not
      // break the page.
      try {
        const batches = await api.listImportBatches({ source_import_id: importId });
        setBatch(batches[0] ?? null);
      } catch {
        setBatch(null);
      }
      const eventPage = await api.listSettlementEvents({
        import_batch_id: importDetail.file_import.import_batch_id,
        limit: 100,
      });
      setEvents(eventPage.items);
      // Deposits matched to this batch's events (via their match links).
      const depositIds = new Set<string>();
      for (const event of eventPage.items) {
        const matches = await api.listEventMatches(event.id);
        for (const match of matches) {
          if (match.bank_deposit_id) depositIds.add(match.bank_deposit_id);
        }
      }
      const matched: BankDepositResponse[] = [];
      for (const id of depositIds) {
        matched.push(await api.getBankDeposit(id));
      }
      setDeposits(matched);
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [importId, router]);

  // Loading state is set by load() from a timer callback, never
  // synchronously inside this effect (react-hooks/set-state-in-effect).
  React.useEffect(() => {
    const timer = setTimeout(() => {
      void load();
    }, 0);
    return () => clearTimeout(timer);
  }, [load, reloadKey]);

  const handleApprove = async () => {
    if (!detail) return;
    setBusy(true);
    setActionError(null);
    try {
      // A batch-wrapped import is approved through the batch (the
      // batch drives the import); a pre-US-554 import has no batch.
      if (batch) {
        const response = await api.approveImportBatch(batch.id, {});
        setApproved({
          confirmed_tranche_ids: response.confirmed_tranche_ids,
          net_irr: batch.preview?.net_irr ?? 0,
        });
      } else {
        const response = await api.approveSettlementImport(detail.file_import.id, {});
        setApproved({
          confirmed_tranche_ids: response.confirmed_tranche_ids,
          net_irr: response.net_irr,
        });
      }
      setReloadKey((k) => k + 1);
    } catch (err: unknown) {
      setActionError(persianApiErrorMessage(err, "تأیید ایمپورت ناموفق بود."));
    } finally {
      setBusy(false);
    }
  };

  const handleReject = async () => {
    if (!detail) return;
    setBusy(true);
    setActionError(null);
    try {
      if (batch) {
        await api.rejectImportBatch(batch.id, {
          reason:
            rejectReason.trim() === "" ? "رد ایمپورت توسط کاربر" : rejectReason.trim(),
        });
      } else {
        await api.rejectSettlementImport(detail.file_import.id, {
          reason: rejectReason.trim() === "" ? null : rejectReason.trim(),
        });
      }
      router.push("/settlements");
    } catch (err: unknown) {
      setActionError(persianApiErrorMessage(err, "رد ایمپورت ناموفق بود."));
    } finally {
      setBusy(false);
    }
  };

  const handleRecordDeposit = async () => {
    if (!detail || depositDate === null || depositAmountToman === null || depositAmountToman <= 0) {
      setDepositError("تاریخ و مبلغ واریز الزامی‌اند.");
      return;
    }
    setDepositBusy(true);
    setDepositError(null);
    setDepositResult(null);
    try {
      const result = await api.recordBankDeposit({
        amount_irr: tomanToIrr(depositAmountToman),
        deposit_date: depositDate,
        bank_account_ref: depositBankRef.trim() === "" ? null : depositBankRef.trim(),
        description: depositDescription.trim() === "" ? null : depositDescription.trim(),
        tracking_ref: depositTrackingRef.trim() === "" ? null : depositTrackingRef.trim(),
        source: "manual",
        company_id: companyId,
      });
      setDepositResult(result);
      setReloadKey((k) => k + 1);
    } catch (err: unknown) {
      setDepositError(persianApiErrorMessage(err, "ثبت واریز ناموفق بود."));
    } finally {
      setDepositBusy(false);
    }
  };

  const handleTieOut = async () => {
    if (!detail) return;
    setBusy(true);
    setActionError(null);
    setTieOut(null);
    try {
      const result = await api.runImportTieOut(detail.file_import.id, {
        company_id: companyId,
      });
      setTieOut(result);
      setReloadKey((k) => k + 1);
    } catch (err: unknown) {
      setActionError(persianApiErrorMessage(err, "اجرای tie-out ناموفق بود."));
    } finally {
      setBusy(false);
    }
  };

  if (loading || companiesLoading) {
    return (
      <div className="space-y-6" aria-label="در حال بارگذاری">
        <Skeleton className="h-8 w-56" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (error && isPermissionDenied(error)) {
    return (
      <PermissionDeniedState
        message={persianApiErrorMessage(error, "دسترسی به این ایمپورت ممکن نیست.")}
      />
    );
  }

  if (error && isNotFoundError(error)) {
    return <NotFoundState message="این ایمپورت پیدا نشد." />;
  }

  if (error || !detail) {
    return (
      <ErrorState
        message={persianApiErrorMessage(error, "بارگذاری ایمپورت ناموفق بود.")}
        onRetry={() => setReloadKey((k) => k + 1)}
      />
    );
  }

  const fileImport = detail.file_import;
  const awaitingReview = fileImport.status === "awaiting_review";
  const canTieOut =
    fileImport.status === "approved" || fileImport.status === "settled" || fileImport.status === "exception";

  const eventColumns: DataTableColumn<SettlementEventResponse>[] = [
    { key: "payout", header: "تاریخ واریز", render: (row) => formatIsoDate(row.payout_date) },
    {
      key: "net",
      header: "خالص",
      render: (row) => <span className="tabular-nums">{formatMoney(row.net_irr)}</span>,
      className: "text-end",
      headerClassName: "text-end",
    },
    {
      key: "deductions",
      header: "کسورات",
      render: (row) => <span className="tabular-nums">{formatMoney(row.deductions_irr)}</span>,
      className: "text-end",
      headerClassName: "text-end",
    },
    { key: "tracking", header: "شماره پیگیری", render: (row) => (row.tracking_ref ? <span dir="ltr">{row.tracking_ref}</span> : "—") },
    { key: "source", header: "منبع", render: (row) => (row.source === "file" ? "فایل" : row.source === "api" ? "API" : "دستی") },
  ];

  const depositColumns: DataTableColumn<BankDepositResponse>[] = [
    { key: "date", header: "تاریخ", render: (row) => formatIsoDate(row.deposit_date) },
    {
      key: "amount",
      header: "مبلغ",
      render: (row) => <span className="tabular-nums">{formatMoney(row.amount_irr)}</span>,
      className: "text-end",
      headerClassName: "text-end",
    },
    { key: "tracking", header: "شماره پیگیری", render: (row) => (row.tracking_ref ? <span dir="ltr">{row.tracking_ref}</span> : "—") },
    { key: "description", header: "شرح", render: (row) => row.description ?? "—" },
  ];

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <Link
          href="/settlements"
          className={buttonVariants({ variant: "ghost", size: "icon" })}
          aria-label="بازگشت به مرکز تسویه"
        >
          <ArrowRight className="size-4" aria-hidden />
        </Link>
        <div>
          <h1 className="text-2xl font-bold">ایمپورت فایل تسویه</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {channelNameOf(fileImport.payment_channel_id, channelsMap)} · دورهٔ{" "}
            {jalaliPeriodLabel(fileImport.period_jalali)} · {fileTypeLabel(fileImport.file_type)}
          </p>
        </div>
        <StatusBadge status={fileImport.status} label={importStatusLabel(fileImport.status)} tone={importStatusTone(fileImport.status)} />
      </div>

      {actionError ? <p className="text-sm text-destructive">{actionError}</p> : null}

      {approved ? (
        <p className="text-sm text-muted-foreground">
          ایمپورت تأیید شد: {faNumber(approved.confirmed_tranche_ids.length)} قسط تأیید شد؛
          خالص رویداد {formatMoney(approved.net_irr)} است.
        </p>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>مشخصات بچ</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid gap-4 sm:grid-cols-3">
            <div>
              <dt className="text-sm text-muted-foreground">ردیف‌ها</dt>
              <dd className="mt-1 font-medium tabular-nums">{faNumber(fileImport.row_count)}</dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">منطبق</dt>
              <dd className="mt-1 font-medium tabular-nums">{faNumber(fileImport.matched_count)}</dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">پرداخت جدید</dt>
              <dd className="mt-1 font-medium tabular-nums">{faNumber(fileImport.new_count)}</dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">خطا</dt>
              <dd className="mt-1 font-medium tabular-nums">{faNumber(fileImport.error_count)}</dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">آپلودکننده</dt>
              <dd className="mt-1 font-medium">{fileImport.uploaded_by ?? "—"}</dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">تاریخ آپلود</dt>
              <dd className="mt-1 font-medium">
                {fileImport.uploaded_at ? formatIsoDate(fileImport.uploaded_at.slice(0, 10)) : "—"}
              </dd>
            </div>
          </dl>
        </CardContent>
      </Card>

      {batch ? (
        <Card>
          <CardHeader>
            <CardTitle>چرخهٔ بچ ایمپورت</CardTitle>
            <CardDescription>
              این ایمپورت در چرخهٔ یکپارچهٔ بچ‌ها ثبت شده است؛ تأیید و رد از همین
              چرخه می‌گذرد و مغایرت بچ جداگانه گزارش می‌شود.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex flex-wrap items-center gap-3">
              <StatusBadge
                status={batch.status}
                label={batchStatusLabel(batch.status)}
                tone={batchStatusTone(batch.status)}
              />
              <p className="text-sm text-muted-foreground">
                تلاش {faNumber(batch.attempt)} ·{" "}
                {batch.stages.map((stage) => batchStageLabel(stage.stage)).join(" ← ")}
              </p>
            </div>
            {batch.preview ? (
              <dl className="grid gap-4 sm:grid-cols-3">
                <div>
                  <dt className="text-sm text-muted-foreground">خالص بچ</dt>
                  <dd className="mt-1 font-medium tabular-nums">
                    {formatMoney(batch.preview.net_irr)}
                  </dd>
                </div>
                <div>
                  <dt className="text-sm text-muted-foreground">واریز موردانتظار</dt>
                  <dd className="mt-1 font-medium tabular-nums">
                    {formatMoney(batch.preview.expected_deposit_irr)}
                  </dd>
                </div>
                <div>
                  <dt className="text-sm text-muted-foreground">وضعیت ثبت</dt>
                  <dd className="mt-1 font-medium">
                    {batch.posting_status ?? "—"}
                  </dd>
                </div>
              </dl>
            ) : null}
            {batch.rejection_reason ? (
              <p className="text-sm text-muted-foreground">
                دلیل رد: {batch.rejection_reason}
              </p>
            ) : null}
            <Link
              href={`/settlements/imports/batch/${batch.id}`}
              className={buttonVariants({ variant: "outline" })}
            >
              مشاهدهٔ کامل بچ
            </Link>
          </CardContent>
        </Card>
      ) : null}

      {awaitingReview ? (
        <Card>
          <CardHeader>
            <CardTitle>بازبینی و تأیید</CardTitle>
            <CardDescription>
              تا تأیید انسانی هیچ قسطی تأیید نمی‌شود. ایمپورت در{" "}
              <Link href="/review-queue?type=manual_import_review" className="text-primary underline-offset-4 hover:underline">
                صف بازبینی
              </Link>{" "}
              ثبت شده است.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="import-reject-reason">دلیل رد (اختیاری — فقط برای رد)</Label>
              <Textarea
                id="import-reject-reason"
                value={rejectReason}
                onChange={(e) => setRejectReason(e.target.value)}
                placeholder="چرا این ایمپورت رد می‌شود؟"
              />
            </div>
            <div className="flex flex-wrap gap-4">
              <Button onClick={handleApprove} disabled={busy}>
                تأیید و ثبت تسویه
              </Button>
              <Button variant="outline" onClick={handleReject} disabled={busy}>
                رد ایمپورت
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>ردیف‌های فایل</CardTitle>
          <CardDescription>نتیجهٔ تطبیق آزمایشی هر ردیف.</CardDescription>
        </CardHeader>
        <CardContent>
          <DataTable
            columns={SETTLEMENT_IMPORT_ROW_COLUMNS}
            rows={detail.rows}
            rowKey={(row) => row.id}
            emptyText="این فایل ردیفی ندارد."
          />
        </CardContent>
      </Card>

      {events.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle>رویدادهای تسویهٔ بچ</CardTitle>
            <CardDescription>واریزهای تأییدشدهٔ درگاه برای این بچ.</CardDescription>
          </CardHeader>
          <CardContent>
            <DataTable
              columns={eventColumns}
              rows={events}
              rowKey={(row) => row.id}
              emptyText="رویدادی نیست."
            />
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>واریزهای بانکی بچ</CardTitle>
          <CardDescription>
            واریزهای تطبیق‌شده با این بچ. واریز جدید را اینجا ثبت کنید؛ تطبیق
            خودکار بر اساس مرجع دقیق، سپس مبلغ و تاریخ انجام می‌شود.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <DataTable
            columns={depositColumns}
            rows={deposits}
            rowKey={(row) => row.id}
            emptyText="هنوز واریزی به این بچ تطبیق نشده است."
          />

          <div className="grid gap-4 border-t pt-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="deposit-date">تاریخ واریز</Label>
              <JalaliDatePicker
                id="deposit-date"
                value={depositDate}
                onChange={(iso) => setDepositDate(iso)}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="deposit-amount">مبلغ واریز</Label>
              <AmountInput
                id="deposit-amount"
                value={depositAmountToman}
                onChange={setDepositAmountToman}
                unit="تومان"
                min={1}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="deposit-tracking">شماره پیگیری بانکی</Label>
              <Input
                id="deposit-tracking"
                value={depositTrackingRef}
                onChange={(e) => setDepositTrackingRef(e.target.value)}
                dir="ltr"
                placeholder="اختیاری"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="deposit-bank-ref">مرجع حساب بانکی</Label>
              <Input
                id="deposit-bank-ref"
                value={depositBankRef}
                onChange={(e) => setDepositBankRef(e.target.value)}
                dir="ltr"
                placeholder="اختیاری"
              />
            </div>
            <div className="space-y-1.5 sm:col-span-2">
              <Label htmlFor="deposit-description">شرح بانکی</Label>
              <Input
                id="deposit-description"
                value={depositDescription}
                onChange={(e) => setDepositDescription(e.target.value)}
                placeholder="اختیاری — متن گردش حساب"
              />
            </div>
            <div className="space-y-1.5">
              <Label>شرکت (برای tie-out هم‌زمان)</Label>
              <CompanyPicker companies={companies} companyId={companyId} onChange={setCompanyId} />
            </div>
          </div>

          {depositError ? <p className="text-sm text-destructive">{depositError}</p> : null}
          {depositResult ? (
            <p className="text-sm text-muted-foreground">
              واریز ثبت شد — تطبیق: {depositDecisionLabel(depositResult.decision_method)}
              {depositResult.matched_tranche_ids.length > 0
                ? ` · ${faNumber(depositResult.matched_tranche_ids.length)} قسط دریافت شد`
                : " · بی‌همتا"}
              {depositResult.posted_journal_id !== null
                ? ` · سند بانکی ثبت شد (دفتر ${faNumber(depositResult.posted_journal_id)})`
                : ""}
            </p>
          ) : null}

          <Button onClick={handleRecordDeposit} disabled={depositBusy}>
            {depositBusy ? "در حال ثبت…" : "ثبت واریز بانکی"}
          </Button>
        </CardContent>
      </Card>

      {canTieOut ? (
        <Card>
          <CardHeader>
            <CardTitle>سه‌طرفهٔ tie-out</CardTitle>
            <CardDescription>
              Σ خالص تأییدشده منهای کسورات مستند در برابر Σ واریزهای بانکی. بدون
              شرکت، tie-out فقط محاسبه می‌شود و چیزی ثبت نمی‌شود.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1.5">
              <Label>شرکت</Label>
              <CompanyPicker companies={companies} companyId={companyId} onChange={setCompanyId} />
            </div>
            <Button onClick={handleTieOut} disabled={busy}>
              {busy ? "در حال اجرا…" : "اجرای tie-out"}
            </Button>

            {tieOut ? (
              <div className="space-y-4 border-t pt-4">
                <dl className="grid gap-4 sm:grid-cols-3">
                  <div>
                    <dt className="text-sm text-muted-foreground">خالص تأییدشده</dt>
                    <dd className="mt-1 font-medium tabular-nums">{formatMoney(tieOut.tieout.confirmed_total_irr)}</dd>
                  </div>
                  <div>
                    <dt className="text-sm text-muted-foreground">کسورات مستند</dt>
                    <dd className="mt-1 font-medium tabular-nums">{formatMoney(tieOut.tieout.deductions_irr)}</dd>
                  </div>
                  <div>
                    <dt className="text-sm text-muted-foreground">واریز موردانتظار</dt>
                    <dd className="mt-1 font-medium tabular-nums">{formatMoney(tieOut.tieout.expected_deposits_irr)}</dd>
                  </div>
                  <div>
                    <dt className="text-sm text-muted-foreground">واریزهای تطبیق‌شده</dt>
                    <dd className="mt-1 font-medium tabular-nums">{formatMoney(tieOut.tieout.matched_deposits_irr)}</dd>
                  </div>
                  <div>
                    <dt className="text-sm text-muted-foreground">اختلاف</dt>
                    <dd className="mt-1 font-medium tabular-nums">{formatMoney(tieOut.tieout.difference_irr)}</dd>
                  </div>
                  <div>
                    <dt className="text-sm text-muted-foreground">نتیجه</dt>
                    <dd className="mt-1 font-medium">
                      {tieOut.tieout.holds ? "تطبیق برقرار است" : "مغایرت دارد"}
                    </dd>
                  </div>
                </dl>
                <p className="text-sm text-muted-foreground">
                  وضعیت بچ: {importStatusLabel(tieOut.status)}
                  {tieOut.gate_reason ? ` · دروازهٔ سیاست: ${tieOut.gate_reason}` : ""}
                </p>
                {tieOut.posted ? (
                  <p className="text-sm text-muted-foreground">
                    سند بانکی ثبت شد
                    {tieOut.tryton_journal_id !== null ? ` (دفتر ${faNumber(tieOut.tryton_journal_id)})` : ""}؛{" "}
                    {faNumber(tieOut.settled_tranche_ids.length)} قسط بسته شد.
                  </p>
                ) : null}
                {tieOut.exception_ids.length > 0 ? (
                  <p className="text-sm text-muted-foreground">
                    {faNumber(tieOut.exception_ids.length)} استثنا باز شد؛ در{" "}
                    <Link href="/review-queue?type=settlement_variance" className="text-primary underline-offset-4 hover:underline">
                      صف بازبینی
                    </Link>{" "}
                    رسیدگی کنید.
                  </p>
                ) : null}
              </div>
            ) : null}
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
