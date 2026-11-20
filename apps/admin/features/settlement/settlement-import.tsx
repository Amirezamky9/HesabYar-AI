/**
 * Settlement file import wizard (US-525).
 *
 * Steps: pick a channel + file + settings → upload (parse + dry-run
 * preview) → review the validation/dedupe summary and the
 * accounting-effect preview → approve (confirms tranches, files a
 * settlement event) or reject. The upload files a
 * `manual_import_review` item in the unified review queue; nothing is
 * confirmed until a human approves. Re-importing the same file is a
 * no-op (file-hash dedupe).
 *
 * No gateway panel password is ever asked for or stored: the operator
 * uploads the gateway's exported file (Excel/CSV/PDF).
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowRight, FileUp } from "lucide-react";
import {
  ApiError,
  api,
  type ApproveImportResponse,
  type ImportSettlementFileResponse,
  type PaymentChannelResponse,
  type SettlementFileRowResponse,
} from "@/lib/api-client";
import { formatIsoDate } from "@/lib/format-date";
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
  Textarea,
  type DataTableColumn,
} from "@/components/ui";
import {
  isPermissionDenied,
  persianApiErrorMessage,
} from "@/features/shared/api-error";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import {
  fileTypeLabel,
  isValidJalaliPeriod,
  jalaliPeriodOf,
  matchStatusLabel,
  matchStatusTone,
  normalizeJalaliPeriod,
} from "./settlement-data";
import { MAX_SETTLEMENT_FILE_BYTES, fileToBase64 } from "./settlement-file";

type Step = "setup" | "preview" | "done";

/** Row columns shared with the import detail page. */
export const SETTLEMENT_IMPORT_ROW_COLUMNS: DataTableColumn<SettlementFileRowResponse>[] = [
  { key: "row", header: "ردیف", render: (row) => faNumber(row.row_no) },
  {
    key: "txn",
    header: "شماره تراکنش",
    render: (row) => (row.gateway_transaction_id ? <span dir="ltr">{row.gateway_transaction_id}</span> : "—"),
  },
  {
    key: "date",
    header: "تاریخ",
    render: (row) => (row.transaction_date ? formatIsoDate(row.transaction_date) : "—"),
  },
  {
    key: "amount",
    header: "مبلغ ناخالص",
    render: (row) =>
      row.amount_irr === null ? "—" : <span className="tabular-nums">{formatMoney(row.amount_irr)}</span>,
    className: "text-end",
    headerClassName: "text-end",
  },
  {
    key: "fee",
    header: "کارمزد",
    render: (row) =>
      row.fee_irr === null ? "—" : <span className="tabular-nums">{formatMoney(row.fee_irr)}</span>,
    className: "text-end",
    headerClassName: "text-end",
  },
  {
    key: "net",
    header: "خالص",
    render: (row) =>
      row.net_irr === null ? "—" : <span className="tabular-nums">{formatMoney(row.net_irr)}</span>,
    className: "text-end",
    headerClassName: "text-end",
  },
  {
    key: "status",
    header: "نتیجهٔ تطبیق",
    render: (row) => (
      <StatusBadge status={row.match_status} label={matchStatusLabel(row.match_status)} tone={matchStatusTone(row.match_status)} />
    ),
  },
  { key: "note", header: "یادداشت", render: (row) => row.note ?? "—" },
];

export function SettlementImport() {
  const router = useRouter();
  const [channels, setChannels] = React.useState<PaymentChannelResponse[]>([]);
  const [channelsLoading, setChannelsLoading] = React.useState(true);
  const [step, setStep] = React.useState<Step>("setup");
  const [channelKey, setChannelKey] = React.useState("");
  const [fileType, setFileType] = React.useState("detail");
  const [periodInput, setPeriodInput] = React.useState(jalaliPeriodOf());
  const [periodError, setPeriodError] = React.useState<string | null>(null);
  const [currencyUnit, setCurrencyUnit] = React.useState<"rial" | "toman">("rial");
  const [dateKind, setDateKind] = React.useState<"jalali" | "gregorian">("jalali");
  const [file, setFile] = React.useState<File | null>(null);
  const [fileBase64, setFileBase64] = React.useState<string | null>(null);
  const [result, setResult] = React.useState<ImportSettlementFileResponse | null>(null);
  const [approved, setApproved] = React.useState<ApproveImportResponse | null>(null);
  const [rejectReason, setRejectReason] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<unknown>(null);

  React.useEffect(() => {
    let cancelled = false;
    api
      .listPaymentChannels({ limit: 100 })
      .then((page) => {
        if (cancelled) return;
        setChannels(page.items);
        if (page.items.length > 0) setChannelKey(page.items[0].channel_key);
        setChannelsLoading(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.replace("/login");
          return;
        }
        setError(err);
        setChannelsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

  const handleFileChange = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const picked = event.target.files?.[0] ?? null;
    setFile(picked);
    setFileBase64(null);
    setError(null);
    if (!picked) return;
    if (picked.size > MAX_SETTLEMENT_FILE_BYTES) {
      setError(new Error("حجم فایل بیشتر از ۱۰ مگابایت است."));
      return;
    }
    setBusy(true);
    try {
      setFileBase64(await fileToBase64(picked));
    } catch (err: unknown) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const handleUpload = async () => {
    if (!file || !fileBase64 || channelKey === "") return;
    if (!isValidJalaliPeriod(periodInput.trim())) {
      setPeriodError("دوره را به قالب «۱۴۰۵/۰۸» وارد کنید.");
      return;
    }
    setPeriodError(null);
    setBusy(true);
    setError(null);
    try {
      const uploaded = await api.importSettlementFile({
        filename: file.name,
        content_base64: fileBase64,
        channel_key: channelKey,
        file_type: fileType,
        period_jalali: normalizeJalaliPeriod(periodInput.trim()),
        currency_unit: currencyUnit,
        date_kind: dateKind,
        header_row: 0,
      });
      setResult(uploaded);
      setStep("preview");
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const handleApprove = async () => {
    if (!result) return;
    setBusy(true);
    setError(null);
    try {
      const response = await api.approveSettlementImport(result.file_import.id, {});
      setApproved(response);
      setStep("done");
    } catch (err: unknown) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const handleReject = async () => {
    if (!result) return;
    setBusy(true);
    setError(null);
    try {
      await api.rejectSettlementImport(result.file_import.id, {
        reason: rejectReason.trim() === "" ? null : rejectReason.trim(),
      });
      router.push("/settlements");
    } catch (err: unknown) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  if (channelsLoading) {
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
        message={persianApiErrorMessage(error, "ایمپورت فایل تسویه ناموفق بود.")}
      />
    );
  }

  const canUpload =
    file !== null && fileBase64 !== null && channelKey !== "" && periodInput.trim() !== "";

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
            فایل خروجی درگاه را آپلود کنید؛ پیش‌نمایش اثر حسابداری را ببینید و پس
            از بازبینی تأیید کنید.
          </p>
        </div>
      </div>

      {error ? (
        <ErrorState
          message={persianApiErrorMessage(error, "ایمپورت فایل تسویه ناموفق بود.")}
          onRetry={() => setError(null)}
        />
      ) : null}

      {step === "setup" ? (
        <Card>
          <CardHeader>
            <CardTitle>فایل تسویهٔ درگاه</CardTitle>
            <CardDescription>
              خروجی Excel یا CSV یا PDF درگاه. هر فایل حداکثر ۲٬۰۰۰ ردیف و ۱۰
              مگابایت.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-6">
            <p className="text-sm text-muted-foreground">
              فایل از پنل درگاه دانلود می‌شود؛ رمز پنل درگاه هرگز پرسیده یا ذخیره
              نمی‌شود. ستون‌ها به‌صورت خودکار شناسایی می‌شوند (شماره تراکنش،
              تاریخ، مبلغ، کارمزد، خالص).
            </p>

            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor="settlement-channel">کانال پرداخت</Label>
                <Select value={channelKey} onValueChange={(v) => setChannelKey(v ?? "")}>
                  <SelectTrigger id="settlement-channel">
                    {/* Explicit label: Base UI resolves the trigger label
                    from popup items (mounted on first open), so a
                    controlled value would otherwise display its raw code. */}
                    <SelectValue>
                      {channelKey === ""
                        ? "یک کانال انتخاب کنید"
                        : (channels.find((c) => c.channel_key === channelKey)?.display_name_fa ?? channelKey)}
                    </SelectValue>
                  </SelectTrigger>
                  <SelectContent>
                    {channels.map((c) => (
                      <SelectItem key={c.id} value={c.channel_key}>
                        {c.display_name_fa}
                        {c.enabled ? "" : " (غیرفعال)"}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="settlement-file-type">نوع فایل</Label>
                <Select value={fileType} onValueChange={(v) => setFileType(v ?? "detail")}>
                  <SelectTrigger id="settlement-file-type">
                    <SelectValue>{fileTypeLabel(fileType)}</SelectValue>
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="detail">{fileTypeLabel("detail")}</SelectItem>
                    <SelectItem value="payouts">{fileTypeLabel("payouts")}</SelectItem>
                    <SelectItem value="statement">{fileTypeLabel("statement")}</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="settlement-period">دورهٔ فایل (جلالی)</Label>
                <Input
                  id="settlement-period"
                  value={periodInput}
                  onChange={(e) => setPeriodInput(e.target.value)}
                  placeholder="۱۴۰۵/۰۸"
                  inputMode="numeric"
                  dir="ltr"
                  aria-describedby={periodError ? "settlement-period-error" : undefined}
                />
                {periodError ? (
                  <p id="settlement-period-error" className="text-sm text-destructive">
                    {periodError}
                  </p>
                ) : null}
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="settlement-unit">واحد مبالغ فایل</Label>
                <Select
                  value={currencyUnit}
                  onValueChange={(v) => setCurrencyUnit(v as "rial" | "toman")}
                >
                  <SelectTrigger id="settlement-unit">
                    <SelectValue>{currencyUnit === "toman" ? "تومان" : "ریال"}</SelectValue>
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="rial">ریال</SelectItem>
                    <SelectItem value="toman">تومان</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="settlement-date-kind">تقویم تاریخ‌ها</Label>
                <Select
                  value={dateKind}
                  onValueChange={(v) => setDateKind(v as "jalali" | "gregorian")}
                >
                  <SelectTrigger id="settlement-date-kind">
                    <SelectValue>{dateKind === "gregorian" ? "میلادی" : "جلالی"}</SelectValue>
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="jalali">جلالی</SelectItem>
                    <SelectItem value="gregorian">میلادی</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="settlement-import-file">فایل تسویه</Label>
              <Input
                id="settlement-import-file"
                type="file"
                accept=".xlsx,.csv,.pdf"
                onChange={handleFileChange}
              />
              {file ? (
                <p className="text-sm text-muted-foreground">
                  {file.name} — {fa(Math.round(file.size / 1024))} کیلوبایت
                </p>
              ) : null}
            </div>

            <Button onClick={handleUpload} disabled={!canUpload || busy}>
              <FileUp className="size-4" aria-hidden />
              آپلود و پیش‌نمایش
            </Button>
          </CardContent>
        </Card>
      ) : null}

      {step === "preview" && result ? (
        <div className="space-y-6">
          {result.is_duplicate ? (
            <Card>
              <CardHeader>
                <CardTitle>این فایل قبلاً ایمپورت شده است</CardTitle>
                <CardDescription>
                  فایلی با همین اثر انگشت قبلاً ثبت شده؛ ایمپورت دوباره هیچ
                  تغییری نمی‌دهد.
                </CardDescription>
              </CardHeader>
              <CardContent>
                <Link
                  href={`/settlements/imports/${result.file_import.id}`}
                  className={buttonVariants({ variant: "default" })}
                >
                  مشاهدهٔ ایمپورت قبلی
                </Link>
              </CardContent>
            </Card>
          ) : (
            <>
              <Card>
                <CardHeader>
                  <CardTitle>پیش‌نمایش ایمپورت</CardTitle>
                  <CardDescription>
                    {faNumber(result.preview.matched_rows)} ردیف منطبق ·{" "}
                    {faNumber(result.preview.new_payment_rows)} پرداخت جدید ·{" "}
                    {faNumber(result.preview.unknown_rows)} مرجع ناشناخته ·{" "}
                    {faNumber(result.preview.duplicate_rows)} تکراری ·{" "}
                    {faNumber(result.preview.invalid_rows)} نامعتبر ·{" "}
                    {faNumber(result.preview.dropped_rows)} ردیف جمع حذف‌شده
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-6">
                  <dl className="grid gap-4 sm:grid-cols-3">
                    <div>
                      <dt className="text-sm text-muted-foreground">مجموع ناخالص</dt>
                      <dd className="mt-1 font-medium tabular-nums">{formatMoney(result.preview.gross_irr)}</dd>
                    </div>
                    <div>
                      <dt className="text-sm text-muted-foreground">کارمزد</dt>
                      <dd className="mt-1 font-medium tabular-nums">{formatMoney(result.preview.fee_irr)}</dd>
                    </div>
                    <div>
                      <dt className="text-sm text-muted-foreground">مالیات کارمزد</dt>
                      <dd className="mt-1 font-medium tabular-nums">{formatMoney(result.preview.vat_irr)}</dd>
                    </div>
                    <div>
                      <dt className="text-sm text-muted-foreground">خالص</dt>
                      <dd className="mt-1 font-medium tabular-nums">{formatMoney(result.preview.net_irr)}</dd>
                    </div>
                    <div>
                      <dt className="text-sm text-muted-foreground">برگشت‌ها</dt>
                      <dd className="mt-1 font-medium tabular-nums">{formatMoney(result.preview.refunds_irr)}</dd>
                    </div>
                    <div>
                      <dt className="text-sm text-muted-foreground">کسورات مستند</dt>
                      <dd className="mt-1 font-medium tabular-nums">{formatMoney(result.preview.deductions_irr)}</dd>
                    </div>
                    <div>
                      <dt className="text-sm text-muted-foreground">واریز بانکی موردانتظار</dt>
                      <dd className="mt-1 font-medium tabular-nums">{formatMoney(result.preview.expected_deposit_irr)}</dd>
                    </div>
                    <div>
                      <dt className="text-sm text-muted-foreground">طلب باز کانال — پیش از ایمپورت</dt>
                      <dd className="mt-1 font-medium tabular-nums">{formatMoney(result.preview.receivable_before_irr)}</dd>
                    </div>
                    <div>
                      <dt className="text-sm text-muted-foreground">طلب باز کانال — پس از ایمپورت</dt>
                      <dd className="mt-1 font-medium tabular-nums">{formatMoney(result.preview.receivable_after_irr)}</dd>
                    </div>
                  </dl>

                  <p className="text-sm text-muted-foreground">
                    این ایمپورت در{" "}
                    <Link href="/review-queue?type=manual_import_review" className="text-primary underline-offset-4 hover:underline">
                      صف بازبینی
                    </Link>{" "}
                    ثبت شد و تا تأیید انسانی هیچ قسطی تأیید نمی‌شود.
                  </p>

                  <DataTable
                    columns={SETTLEMENT_IMPORT_ROW_COLUMNS}
                    rows={result.rows}
                    rowKey={(row) => row.id}
                    emptyText="این فایل ردیفی ندارد."
                  />

                  {result.dropped_rows.length > 0 ? (
                    <p className="text-sm text-muted-foreground">
                      ردیف‌های جمع/سرجمع حذف شدند و تطبیق داده نشدند:{" "}
                      {result.dropped_rows.map((d) => `ردیف ${faNumber(d.row_number)}`).join("، ")}.
                    </p>
                  ) : null}

                  <div className="space-y-1.5">
                    <Label htmlFor="settlement-reject-reason">دلیل رد (اختیاری — فقط برای رد)</Label>
                    <Textarea
                      id="settlement-reject-reason"
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
            </>
          )}
        </div>
      ) : null}

      {step === "done" && approved && result ? (
        <Card>
          <CardHeader>
            <CardTitle>تسویه ثبت شد</CardTitle>
            <CardDescription>
              {faNumber(approved.confirmed_tranche_ids.length)} قسط تأیید شد؛ خالص
              رویداد تسویه {formatMoney(approved.net_irr)} است.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {approved.created_payment_ids.length > 0 ? (
              <p className="text-sm text-muted-foreground">
                {faNumber(approved.created_payment_ids.length)} پرداخت جدید از ردیف‌های
                ناشناخته ساخته شد.
              </p>
            ) : null}
            {approved.exception_ids.length > 0 ? (
              <p className="text-sm text-muted-foreground">
                {faNumber(approved.exception_ids.length)} استثنا باز شد؛ در{" "}
                <Link href="/review-queue?type=settlement_variance" className="text-primary underline-offset-4 hover:underline">
                  صف بازبینی
                </Link>{" "}
                رسیدگی کنید.
              </p>
            ) : null}
            <p className="text-sm text-muted-foreground">
              گام بعد: واریز بانکی را ثبت کنید و tie-out را اجرا کنید تا قسط‌ها
              بسته شوند.
            </p>
            <div className="flex flex-wrap gap-4">
              <Link
                href={`/settlements/imports/${result.file_import.id}`}
                className={buttonVariants({ variant: "default" })}
              >
                مشاهدهٔ ایمپورت و tie-out
              </Link>
              <Link href="/settlements/schedule" className={buttonVariants({ variant: "outline" })}>
                برنامهٔ قسط‌ها
              </Link>
            </div>
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
