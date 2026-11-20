/**
 * Invoice detail panel (US-304).
 *
 * One invoice from GET /api/v1/invoices/{id}: party, dates, lines and
 * untaxed/tax/total. Draft invoices can be checked with
 * POST /invoices/{id}/validate — a read-only HesabYar check (the API
 * never finalizes here; finalizing is a later story, so no finalize or
 * payment action exists in this UI).
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowRight, CheckCircle2 } from "lucide-react";
import { ApiError, api, type InvoiceResponse, type ValidationResponse } from "@/lib/api-client";
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
  DataTable,
  MoneyDisplay,
  Skeleton,
  StatusBadge,
  toast,
  type DataTableColumn,
} from "@/components/ui";
import {
  isNotFoundError,
  isPermissionDenied,
  persianApiErrorMessage,
} from "@/features/shared/api-error";
import { ErrorState, NotFoundState, PermissionDeniedState } from "@/features/shared/states";
import { INVOICE_TYPE_LABELS, invoiceLineTypeLabel } from "./labels";

const LINE_COLUMNS: DataTableColumn<InvoiceResponse["lines"][number]>[] = [
  { key: "description", header: "شرح", render: (line) => line.description || "—" },
  {
    key: "type",
    header: "نوع ردیف",
    render: (line) => invoiceLineTypeLabel(line.line_type),
  },
  {
    key: "quantity",
    header: "تعداد",
    render: (line) => <span className="tabular-nums">{fa(line.quantity)}</span>,
  },
  {
    key: "unit_price",
    header: "قیمت واحد",
    render: (line) => <span className="tabular-nums">{formatMoney(line.unit_price)}</span>,
    className: "text-end",
    headerClassName: "text-end",
  },
  {
    key: "amount",
    header: "مبلغ",
    render: (line) => <span className="tabular-nums">{formatMoney(line.amount)}</span>,
    className: "text-end",
    headerClassName: "text-end",
  },
];

export function InvoiceDetail({ invoiceId }: { invoiceId: number }) {
  const router = useRouter();
  const [invoice, setInvoice] = React.useState<InvoiceResponse | null>(null);
  const [error, setError] = React.useState<unknown>(null);
  const [validating, setValidating] = React.useState(false);
  const [validation, setValidation] = React.useState<ValidationResponse | null>(null);
  const [reloadKey, setReloadKey] = React.useState(0);

  React.useEffect(() => {
    let cancelled = false;
    api
      .getInvoice(invoiceId)
      .then((data) => {
        if (!cancelled) setInvoice(data);
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
  }, [invoiceId, reloadKey, router]);

  const handleValidate = async () => {
    setValidating(true);
    setValidation(null);
    try {
      const result = await api.validateInvoice(invoiceId);
      setValidation(result);
      if (result.valid) {
        toast.success("پیش‌نویس معتبر است.");
      } else {
        toast.error("پیش‌نویس مشکل دارد؛ موارد زیر را بررسی کنید.");
      }
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      toast.error(persianApiErrorMessage(err, "بررسی پیش‌نویس ناموفق بود."));
    } finally {
      setValidating(false);
    }
  };

  const loading = invoice === null && error === null;

  if (loading) {
    return (
      <div className="space-y-6" aria-label="در حال بارگذاری">
        <Skeleton className="h-8 w-56" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (error && isPermissionDenied(error)) {
    return <PermissionDeniedState message={persianApiErrorMessage(error, "دریافت فاکتور ناموفق بود.")} />;
  }

  if (error && isNotFoundError(error)) {
    return <NotFoundState message="فاکتور موردنظر پیدا نشد." />;
  }

  if (error || !invoice) {
    return (
      <ErrorState
        message={persianApiErrorMessage(error, "دریافت فاکتور ناموفق بود.")}
        onRetry={() => {
          setError(null);
          setInvoice(null);
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
          <Link href="/invoices" className={buttonVariants({ variant: "ghost", size: "icon" })} aria-label="بازگشت به فاکتورها">
            <ArrowRight className="size-4" aria-hidden />
          </Link>
          <div>
            <h1 className="text-2xl font-bold">
              فاکتور {invoice.number ? <span dir="ltr" className="ltr-isolate">{invoice.number}</span> : `#${fa(invoice.id)}`}
            </h1>
            <p className="mt-1 text-sm text-muted-foreground">
              {INVOICE_TYPE_LABELS[invoice.invoice_type as keyof typeof INVOICE_TYPE_LABELS] ?? invoice.invoice_type}
              {" · "}
              {invoice.party_name}
            </p>
          </div>
        </div>
        <StatusBadge status={invoice.state} />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>مشخصات فاکتور</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid gap-4 sm:grid-cols-2">
            <div>
              <dt className="text-sm text-muted-foreground">طرف حساب</dt>
              <dd className="mt-1 font-medium">{invoice.party_name}</dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">تاریخ فاکتور</dt>
              <dd className="mt-1 font-medium">{formatIsoDate(invoice.invoice_date) || "—"}</dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">تاریخ حسابداری</dt>
              <dd className="mt-1 font-medium">{formatIsoDate(invoice.accounting_date) || "—"}</dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">مرجع</dt>
              <dd className="mt-1 font-medium" dir="ltr">
                {invoice.reference || "—"}
              </dd>
            </div>
            {invoice.description ? (
              <div className="sm:col-span-2">
                <dt className="text-sm text-muted-foreground">شرح</dt>
                <dd className="mt-1">{invoice.description}</dd>
              </div>
            ) : null}
          </dl>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>ردیف‌های فاکتور</CardTitle>
        </CardHeader>
        <CardContent className="space-y-6">
          <DataTable
            columns={LINE_COLUMNS}
            rows={invoice.lines}
            rowKey={(_, i) => i}
            emptyText="این فاکتور ردیفی ندارد."
          />
          <dl className="ms-auto grid max-w-sm gap-2 border-t pt-4">
            <div className="flex items-center justify-between gap-4">
              <dt className="text-sm text-muted-foreground">مبلغ بدون مالیات</dt>
              <dd className="tabular-nums">{formatMoney(invoice.untaxed_amount)}</dd>
            </div>
            <div className="flex items-center justify-between gap-4">
              <dt className="text-sm text-muted-foreground">مالیات</dt>
              <dd className="tabular-nums">{formatMoney(invoice.tax_amount)}</dd>
            </div>
            <div className="flex items-center justify-between gap-4 border-t pt-2">
              <dt className="font-medium">مبلغ کل</dt>
              <dd>
                <MoneyDisplay amountIrr={invoice.total_amount} size="sm" />
              </dd>
            </div>
          </dl>
        </CardContent>
      </Card>

      {invoice.state === "draft" ? (
        <Card>
          <CardHeader>
            <CardTitle>بررسی پیش‌نویس</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <p className="text-sm text-muted-foreground">
              بررسی، پیش‌نویس را تغییر نمی‌دهد و آن را نهایی نمی‌کند؛ فقط نشان می‌دهد آیا برای مرحلهٔ بعد آماده است.
            </p>
            <Button onClick={handleValidate} disabled={validating}>
              <CheckCircle2 className="size-4" />
              {validating ? "در حال بررسی…" : "بررسی پیش‌نویس"}
            </Button>
            {validation ? (
              validation.valid ? (
                <p role="status" className="text-sm text-success">
                  پیش‌نویس معتبر است و آمادهٔ مرحلهٔ بعد است.
                </p>
              ) : (
                <ul role="alert" className="space-y-1 text-sm text-destructive">
                  {validation.issues?.map((issue, i) => (
                    <li key={`${issue.code}-${i}`}>
                      {issue.message}
                      {issue.field ? ` (‏${issue.field}‏)` : ""}
                    </li>
                  )) ?? <li>پیش‌نویس معتبر نیست.</li>}
                </ul>
              )
            ) : null}
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
