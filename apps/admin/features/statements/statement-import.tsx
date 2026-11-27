/**
 * Bank statement import wizard (US-514, B1 — Melli-first; migrated to
 * the unified batch cycle in US-554).
 *
 * Steps: pick a file + settings → detect columns and confirm the column
 * mapping (savable per bank) → import and review the accounting-effect
 * preview → confirm (writes a draft statement to the engine) or cancel
 * (the batch is archived with its reason and stays re-runnable).
 *
 * No internet-banking password is ever asked for or stored: the operator
 * uploads a bank-export file (Excel/CSV/PDF). From Baam: صورت‌حساب ←
 * بازهٔ زمانی ← خروجی Excel ← آپلود اینجا.
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowRight, FileUp, TriangleAlert } from "lucide-react";
import {
  ApiError,
  api,
  type ApproveImportBatchResponse,
  type BankImportBatchDetailResponse,
  type ColumnMappingResponse,
  type FileDetectionResponse,
  type StatementJournalResponse,
  type UploadImportBatchResponse,
} from "@/lib/api-client";
import { formatIsoDate } from "@/lib/format-date";
import { formatMoney } from "@/lib/format-money";
import { fa } from "@/lib/utils";
import {
  Button,
  buttonVariants,
  Card,
  CardContent,
  CardDescription,
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
  Textarea,
  type DataTableColumn,
} from "@/components/ui";
import {
  isPermissionDenied,
  persianApiErrorMessage,
} from "@/features/shared/api-error";
import { CompanyPicker } from "@/features/shared/company-picker";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import { useCompany } from "@/features/shared/use-company";
import {
  MAX_IMPORT_FILE_BYTES,
  MAX_IMPORT_ROWS,
  fileToBase64,
  mappingIsComplete,
} from "./statement-file";

/** Canonical mapping fields, in the order the editor shows them. */
const MAPPING_FIELDS: { key: string; label: string; required: boolean }[] = [
  { key: "date", label: "تاریخ", required: true },
  { key: "description", label: "شرح", required: true },
  { key: "amount", label: "مبلغ (با علامت)", required: false },
  { key: "debit", label: "برداشت", required: false },
  { key: "credit", label: "واریز", required: false },
  { key: "balance", label: "مانده", required: false },
  { key: "bank_ref", label: "شماره سند بانکی", required: false },
];

const NONE_VALUE = "__none__";

type Step = "setup" | "preview" | "done";

const PREVIEW_COLUMNS: DataTableColumn<
  BankImportBatchDetailResponse["rows"][number]
>[] = [
  { key: "row", header: "ردیف", render: (row) => fa(row.row_number) },
  {
    key: "date",
    header: "تاریخ",
    render: (row) => (row.row_date ? formatIsoDate(row.row_date) : "—"),
  },
  { key: "description", header: "شرح", render: (row) => row.description || "—" },
  {
    key: "amount",
    header: "مبلغ",
    render: (row) =>
      row.amount_irr === null ? (
        "—"
      ) : (
        <span className="tabular-nums">
          {row.amount_irr < 0 ? "−" : ""}
          {formatMoney(Math.abs(row.amount_irr))}
        </span>
      ),
    className: "text-end",
    headerClassName: "text-end",
  },
  {
    key: "status",
    header: "وضعیت",
    render: (row) => {
      if (row.parse_errors.length > 0)
        return <StatusBadge status="invalid" label="نامعتبر" tone="destructive" />;
      if (row.is_duplicate)
        return (
          <StatusBadge
            status="duplicate"
            label={row.duplicate_reason === "bank_ref" ? "تکراری (سند)" : "تکراری"}
            tone="warning"
          />
        );
      return <StatusBadge status="new" label="جدید" tone="success" />;
    },
  },
  {
    key: "account",
    header: "حساب پیشنهادی",
    render: (row) =>
      row.suggested_account_id === null ? "—" : fa(row.suggested_account_id),
  },
];

export function StatementImport() {
  const router = useRouter();
  const { companies, companyId, setCompanyId, loading: companiesLoading } =
    useCompany();

  const [step, setStep] = React.useState<Step>("setup");
  const [file, setFile] = React.useState<File | null>(null);
  const [fileBase64, setFileBase64] = React.useState<string | null>(null);
  const [detection, setDetection] = React.useState<FileDetectionResponse | null>(null);
  const [columns, setColumns] = React.useState<Record<string, string>>({});
  const [journals, setJournals] = React.useState<StatementJournalResponse[]>([]);
  const [journalId, setJournalId] = React.useState<string>("");
  const [mappings, setMappings] = React.useState<ColumnMappingResponse[]>([]);
  const [mappingId, setMappingId] = React.useState<string>(NONE_VALUE);
  const [saveMapping, setSaveMapping] = React.useState(false);
  const [mappingName, setMappingName] = React.useState("");
  const [accounts, setAccounts] = React.useState<
    { id: number; code: string; name: string }[]
  >([]);
  const [defaultAccountId, setDefaultAccountId] = React.useState<string>("");
  const [currencyUnit, setCurrencyUnit] = React.useState<"rial" | "toman">("rial");
  const [dateKind, setDateKind] = React.useState<"jalali" | "gregorian">("jalali");
  const [batch, setBatch] = React.useState<BankImportBatchDetailResponse | null>(null);
  const [upload, setUpload] = React.useState<UploadImportBatchResponse | null>(null);
  const [approved, setApproved] = React.useState<ApproveImportBatchResponse | null>(null);
  const [cancelReason, setCancelReason] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<unknown>(null);
  const [notice, setNotice] = React.useState<string | null>(null);

  // Journals + saved mappings + accounts follow the selected company.
  // Loaded independently (allSettled): the accounts list is slow
  // (~33s against remote Tryton) and must not wipe the journals when
  // it fails.
  React.useEffect(() => {
    if (!companyId) return;
    let cancelled = false;
    Promise.allSettled([
      api.listStatementJournals(companyId),
      api.listColumnMappings({ bank_key: "melli" }),
      api.listAccounts({ company: companyId, limit: 100 }),
    ])
      .then(([journalResult, mappingResult, accountResult]) => {
        if (cancelled) return;
        if (journalResult.status === "fulfilled") {
          setJournals(journalResult.value);
          if (journalResult.value.length > 0)
            setJournalId(String(journalResult.value[0].id));
        } else {
          throw journalResult.reason;
        }
        if (mappingResult.status === "fulfilled") {
          setMappings(mappingResult.value.filter((m) => m.status === "active"));
        }
        // Mappings are optional: a failure just means no saved mapping.
        if (accountResult.status === "fulfilled") {
          setAccounts(
            accountResult.value.items
              .filter((a) => a.postable && !a.closed)
              .map((a) => ({ id: a.id, code: a.code, name: a.name })),
          );
        } else {
          setNotice("دریافت فهرست حساب‌ها ناموفق بود؛ صفحه را تازه‌سازی کنید.");
        }
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
  }, [companyId, router]);

  const handleFileChange = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const picked = event.target.files?.[0] ?? null;
    setFile(picked);
    setDetection(null);
    setColumns({});
    setError(null);
    setNotice(null);
    if (!picked) return;
    if (picked.size > MAX_IMPORT_FILE_BYTES) {
      setError(new Error("حجم فایل بیشتر از ۱۰ مگابایت است."));
      return;
    }
    setBusy(true);
    try {
      const base64 = await fileToBase64(picked);
      setFileBase64(base64);
      const detected = await api.detectStatementFile({
        filename: picked.name,
        content_base64: base64,
      });
      setDetection(detected);
      setColumns(detected.suggested_mapping);
      if (detected.missing_fields.length > 0) {
        setNotice(
          `برخی ستون‌ها شناسایی نشدند (${detected.missing_fields.join("، ")}). نگاشت را دستی کامل کنید.`,
        );
      }
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

  const handleImport = async () => {
    if (!companyId || !file || !fileBase64) return;
    setBusy(true);
    setError(null);
    try {
      const created = await api.uploadImportBatch({
        kind: "statement",
        source_key: "melli",
        company: companyId,
        statement_journal_id: Number(journalId),
        filename: file.name,
        content_base64: fileBase64,
        currency_unit: currencyUnit,
        default_account_id: Number(defaultAccountId),
        ...(mappingId !== NONE_VALUE
          ? { mapping_id: mappingId }
          : {
              columns,
              date_kind: dateKind,
              header_row: 0,
              save_mapping: saveMapping,
              mapping_name: mappingName.trim() === "" ? null : mappingName.trim(),
            }),
      });
      setUpload(created);
      // The row table comes from the underlying import detail.
      if (created.batch.source_import_id) {
        const detail = await api.getStatementImport(created.batch.source_import_id);
        setBatch(detail);
      }
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

  const handleConfirm = async () => {
    if (!upload) return;
    setBusy(true);
    setError(null);
    try {
      const response = await api.approveImportBatch(upload.batch.id, {});
      setApproved(response);
      setStep("done");
    } catch (err: unknown) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const handleCancel = async () => {
    if (!upload) return;
    setBusy(true);
    setError(null);
    try {
      await api.rejectImportBatch(upload.batch.id, {
        reason:
          cancelReason.trim() === "" ? "لغو ایمپورت توسط کاربر" : cancelReason.trim(),
      });
      router.push("/banking/statements");
    } catch (err: unknown) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  if (companiesLoading) {
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
        message={persianApiErrorMessage(error, "ایمپورت صورت‌حساب ناموفق بود.")}
      />
    );
  }

  const canImport =
    file !== null &&
    fileBase64 !== null &&
    journalId !== "" &&
    defaultAccountId !== "" &&
    (mappingId !== NONE_VALUE || (detection !== null && mappingIsComplete(columns)));

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <Link
          href="/banking/statements"
          className={buttonVariants({ variant: "ghost", size: "icon" })}
          aria-label="بازگشت به صورت‌حساب‌های بانکی"
        >
          <ArrowRight className="size-4" aria-hidden />
        </Link>
        <div>
          <h1 className="text-2xl font-bold">ایمپورت صورت‌حساب بانکی</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            فایل خروجی بانک را آپلود کنید؛ صورت‌حساب به‌صورت پیش‌نویس ثبت می‌شود.
          </p>
        </div>
      </div>

      {error ? (
        <ErrorState
          message={persianApiErrorMessage(error, "ایمپورت صورت‌حساب ناموفق بود.")}
          onRetry={() => setError(null)}
        />
      ) : null}

      {step === "done" && approved && upload ? (
        <Card>
          <CardHeader>
            <CardTitle>صورت‌حساب ثبت شد</CardTitle>
            <CardDescription>
              {fa(upload.preview.new_rows)} ردیف جدید به‌صورت پیش‌نویس در دفتر ثبت شد.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <p className="text-sm text-muted-foreground">
              صورت‌حساب هنوز تأیید نشده است؛ پس از تطبیق ردیف‌ها می‌توانید آن را در
              موتور حسابداری تأیید کنید.
            </p>
            <div className="flex flex-wrap gap-4">
              {approved.tryton_journal_id !== null ? (
                <Link
                  href={`/banking/statements/${approved.tryton_journal_id}`}
                  className={buttonVariants({ variant: "default" })}
                >
                  مشاهدهٔ صورت‌حساب
                </Link>
              ) : null}
              <Link
                href="/banking/statements"
                className={buttonVariants({ variant: "outline" })}
              >
                بازگشت به فهرست
              </Link>
            </div>
          </CardContent>
        </Card>
      ) : null}

      {step === "setup" ? (
        <Card>
          <CardHeader>
            <CardTitle>فایل صورت‌حساب</CardTitle>
            <CardDescription>
              خروجی Excel یا CSV یا PDF بانک ملی. هر فایل حداکثر {fa(MAX_IMPORT_ROWS)}{" "}
              ردیف و ۱۰ مگابایت.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-6">
            <p className="text-sm text-muted-foreground">
              از بام: صورت‌حساب ← بازهٔ زمانی ← خروجی Excel ← آپلود اینجا. رمز
              اینترنت‌بانک هرگز پرسیده یا ذخیره نمی‌شود.
            </p>

            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label>شرکت</Label>
                <CompanyPicker
                  companies={companies}
                  companyId={companyId}
                  onChange={setCompanyId}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="import-bank">بانک</Label>
                <Select value="melli" onValueChange={() => {}}>
                  <SelectTrigger id="import-bank">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="melli">بانک ملی ایران</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="import-journal">دفتر صورت‌حساب</Label>
                <Select value={journalId} onValueChange={(v) => setJournalId(v ?? "")}>
                  <SelectTrigger id="import-journal">
                    <SelectValue placeholder="یک دفتر انتخاب کنید" />
                  </SelectTrigger>
                  <SelectContent>
                    {journals.map((j) => (
                      <SelectItem key={j.id} value={String(j.id)}>
                        {j.name}
                        {j.bank_account_number ? ` — ${j.bank_account_number}` : ""}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="import-account">حساب پیش‌فرض ردیف‌های بدون کد</Label>
                <Select value={defaultAccountId} onValueChange={(v) => setDefaultAccountId(v ?? "")}>
                  <SelectTrigger id="import-account">
                    <SelectValue placeholder="یک حساب انتخاب کنید" />
                  </SelectTrigger>
                  <SelectContent>
                    {accounts.map((a) => (
                      <SelectItem key={a.id} value={String(a.id)}>
                        {fa(a.code)} — {a.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="import-unit">واحد مبالغ فایل</Label>
                <Select
                  value={currencyUnit}
                  onValueChange={(v) => setCurrencyUnit(v as "rial" | "toman")}
                >
                  <SelectTrigger id="import-unit">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="rial">ریال</SelectItem>
                    <SelectItem value="toman">تومان</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="import-date-kind">تقویم تاریخ‌ها</Label>
                <Select
                  value={dateKind}
                  onValueChange={(v) => setDateKind(v as "jalali" | "gregorian")}
                >
                  <SelectTrigger id="import-date-kind">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="jalali">جلالی</SelectItem>
                    <SelectItem value="gregorian">میلادی</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="import-file">فایل صورت‌حساب</Label>
              <Input
                id="import-file"
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

            {busy && !detection ? (
              <div className="space-y-2" aria-label="در حال بررسی فایل">
                <Skeleton className="h-6 w-48" />
                <Skeleton className="h-32 w-full" />
              </div>
            ) : null}

            {notice ? (
              <p className="flex items-start gap-2 text-sm text-muted-foreground">
                <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
                {notice}
              </p>
            ) : null}

            {detection ? (
              <div className="space-y-4">
                <div>
                  <h2 className="text-base font-semibold">ستون‌های شناسایی‌شده</h2>
                  <p className="mt-1 text-sm text-muted-foreground">
                    قالب: {detection.file_format === "xlsx" ? "اکسل" : detection.file_format === "csv" ? "CSV" : "PDF"}
                    {detection.encoding ? ` · رمزگذاری: ${detection.encoding}` : ""}
                    {detection.detection_notes.length > 0
                      ? ` · ${detection.detection_notes.join("، ")}`
                      : ""}
                  </p>
                </div>

                {mappings.length > 0 ? (
                  <div className="space-y-1.5">
                    <Label htmlFor="import-mapping">نگاشت ستون ذخیره‌شده</Label>
                    <Select value={mappingId} onValueChange={(v) => setMappingId(v ?? NONE_VALUE)}>
                      <SelectTrigger id="import-mapping">
                        <SelectValue placeholder="نگاشت دستی" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value={NONE_VALUE}>نگاشت دستی</SelectItem>
                        {mappings.map((m) => (
                          <SelectItem key={m.id} value={m.id}>
                            {m.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                ) : null}

                {mappingId === NONE_VALUE ? (
                  <div className="grid gap-4 sm:grid-cols-2">
                    {MAPPING_FIELDS.map((field) => (
                      <div key={field.key} className="space-y-1.5">
                        <Label htmlFor={`mapping-${field.key}`}>
                          {field.label}
                          {field.required ? " (الزامی)" : ""}
                        </Label>
                        <Select
                          value={columns[field.key] ?? NONE_VALUE}
                          onValueChange={(v) =>
                            setColumns((prev) => {
                              const next = { ...prev };
                              if (v === NONE_VALUE || v === null) delete next[field.key];
                              else next[field.key] = v;
                              return next;
                            })
                          }
                        >
                          <SelectTrigger id={`mapping-${field.key}`}>
                            <SelectValue placeholder="—" />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value={NONE_VALUE}>—</SelectItem>
                            {detection.columns.map((col) => (
                              <SelectItem key={col} value={col}>
                                {col}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </div>
                    ))}
                  </div>
                ) : null}

                <p className="text-sm text-muted-foreground">
                  تاریخ و شرح الزامی‌اند؛ مبلغ یا جفت برداشت/واریز لازم است.
                </p>

                {mappingId === NONE_VALUE ? (
                  <div className="space-y-4">
                    <div className="flex items-center gap-2">
                      <Checkbox
                        id="import-save-mapping"
                        checked={saveMapping}
                        onCheckedChange={(v) => setSaveMapping(v === true)}
                      />
                      <Label htmlFor="import-save-mapping">
                        ذخیرهٔ این نگاشت برای بانک ملی (ایمپورت بعدی همین نگاشت را
                        پیشنهاد می‌دهد)
                      </Label>
                    </div>
                    {saveMapping ? (
                      <div className="space-y-1.5">
                        <Label htmlFor="import-mapping-name">نام نگاشت (اختیاری)</Label>
                        <Input
                          id="import-mapping-name"
                          value={mappingName}
                          onChange={(e) => setMappingName(e.target.value)}
                          placeholder="مثلاً: خروجی بام"
                        />
                      </div>
                    ) : null}
                  </div>
                ) : null}

                <Button onClick={handleImport} disabled={!canImport || busy}>
                  <FileUp className="size-4" aria-hidden />
                  ایمپورت و پیش‌نمایش
                </Button>
              </div>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      {step === "preview" && batch ? (
        <Card>
          <CardHeader>
            <CardTitle>پیش‌نمایش ایمپورت</CardTitle>
            <CardDescription>
              {batch.filename} · {fa(batch.total_rows)} ردیف · {fa(batch.new_rows)}{" "}
              جدید · {fa(batch.duplicate_rows)} تکراری · {fa(batch.invalid_rows)}{" "}
              نامعتبر
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <dl className="grid gap-4 sm:grid-cols-3">
              <div>
                <dt className="text-sm text-muted-foreground">مجموع واریزها</dt>
                <dd className="mt-1 font-medium tabular-nums">
                  {formatMoney(batch.total_deposits_irr)}
                </dd>
              </div>
              <div>
                <dt className="text-sm text-muted-foreground">مجموع برداشت‌ها</dt>
                <dd className="mt-1 font-medium tabular-nums">
                  {formatMoney(batch.total_withdrawals_irr)}
                </dd>
              </div>
              <div>
                <dt className="text-sm text-muted-foreground">وضعیت بررسی</dt>
                <dd className="mt-1 font-medium">
                  در صف بازبینی (بررسی دستی ایمپورت)
                </dd>
              </div>
            </dl>
            <DataTable
              columns={PREVIEW_COLUMNS}
              rows={batch.rows}
              rowKey={(row) => row.id}
              emptyText="این فایل ردیفی ندارد."
            />
            {batch.rows.some((row) => row.parse_errors.length > 0) ? (
              <p className="text-sm text-muted-foreground">
                ردیف‌های نامعتبر ثبت نمی‌شوند؛ پیش از تأیید، فایل را اصلاح و دوباره
                آپلود کنید.
              </p>
            ) : null}
            <div className="space-y-1.5">
              <Label htmlFor="import-cancel-reason">دلیل لغو (اختیاری — فقط برای لغو)</Label>
              <Textarea
                id="import-cancel-reason"
                value={cancelReason}
                onChange={(e) => setCancelReason(e.target.value)}
                placeholder="چرا این ایمپورت لغو می‌شود؟"
              />
            </div>
            <div className="flex flex-wrap gap-4">
              <Button onClick={handleConfirm} disabled={busy || batch.new_rows === 0}>
                تأیید و ثبت صورت‌حساب
              </Button>
              <Button variant="outline" onClick={handleCancel} disabled={busy}>
                لغو ایمپورت
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
