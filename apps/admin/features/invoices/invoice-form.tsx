/**
 * Invoice draft creation form (US-304).
 *
 * Creates a draft with POST /invoices/drafts (201) and navigates to the
 * new invoice's detail page. Money is integer IRR in the payload; the
 * operator types Toman in the AmountInput and the form converts (×10).
 * Line accounts and the invoice account come from the chart
 * (GET /accounting/accounts, paginated — the API caps pages at 100);
 * the invoice account is picked explicitly because the backend's
 * default only resolves when the company has exactly one receivable /
 * payable account (the test company has three). There is no parties
 * endpoint yet, so the party is entered by its engine id. No finalize
 * or payment action exists here — the API does not expose them yet.
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowRight, Plus, Trash2 } from "lucide-react";
import {
  ApiError,
  api,
  type AccountResponse,
  type CreateInvoiceDraftRequest,
} from "@/lib/api-client";
import { normalizeDigits } from "@/lib/format-money";
import { formatMoney } from "@/lib/format-money";
import { todayIso } from "@/lib/jalali";
import {
  AmountInput,
  Button,
  buttonVariants,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Input,
  JalaliDatePicker,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Textarea,
  toast,
} from "@/components/ui";
import { CompanyPicker } from "@/features/shared/company-picker";
import { persianApiErrorMessage } from "@/features/shared/api-error";
import { ErrorState } from "@/features/shared/states";
import { useCompany } from "@/features/shared/use-company";
import { INVOICE_TYPE_LABELS } from "./labels";

interface FormLine {
  key: number;
  accountId: string;
  /** Toman, as typed (converted to IRR ×10 on submit). */
  unitPriceToman: number | null;
  quantity: string;
  description: string;
}

const EMPTY_LINE = (key: number): FormLine => ({
  key,
  accountId: "",
  unitPriceToman: null,
  quantity: "۱",
  description: "",
});

/** Parse a user-typed quantity (Persian/Arabic digits, «٫» or "." decimal). */
export function parseQuantity(raw: string): number | null {
  const ascii = normalizeDigits(raw).replace(/[٬,،\s]/g, "").replace("٫", ".").trim();
  if (!/^\d+(\.\d+)?$/.test(ascii)) return null;
  const value = Number(ascii);
  return Number.isFinite(value) && value > 0 ? value : null;
}

let lineKey = 0;

export function InvoiceForm() {
  const router = useRouter();
  const { companies, companyId, setCompanyId, loading: companiesLoading, error: companiesError } = useCompany();
  const [accounts, setAccounts] = React.useState<AccountResponse[]>([]);
  const [accountsError, setAccountsError] = React.useState<unknown>(null);
  const [invoiceType, setInvoiceType] = React.useState<"out" | "in">("out");
  const [invoiceAccountId, setInvoiceAccountId] = React.useState("");
  const [partyId, setPartyId] = React.useState("");
  const [date, setDate] = React.useState<string | null>(() => todayIso());
  const [description, setDescription] = React.useState("");
  const [reference, setReference] = React.useState("");
  const [lines, setLines] = React.useState<FormLine[]>([EMPTY_LINE(lineKey++)]);
  const [submitting, setSubmitting] = React.useState(false);
  const [submitError, setSubmitError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!companyId) return;
    let cancelled = false;
    // The chart has more than one page (API caps pages at 100), so walk
    // the cursors. Only active, postable accounts can take invoice lines.
    (async () => {
      const all: AccountResponse[] = [];
      let cursor: string | undefined;
      try {
        do {
          const page = await api.listAccounts({ company: companyId, limit: 100, cursor });
          all.push(...page.items);
          cursor = page.next_cursor ?? undefined;
        } while (cursor && !cancelled);
        if (!cancelled) setAccounts(all.filter((a) => a.active && a.postable));
      } catch (err: unknown) {
        if (!cancelled) setAccountsError(err);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [companyId]);

  // The invoice account must be receivable (sale) or payable (purchase).
  // The API exposes the account-type name, which is the only signal
  // available; when no account carries it, fall back to every postable
  // account and let the backend validate the pick.
  const invoiceAccounts = React.useMemo(() => {
    const wantedType = invoiceType === "out" ? "Accounts receivable" : "Accounts payable";
    const typed = accounts.filter((a) => a.type === wantedType);
    return typed.length > 0 ? typed : accounts;
  }, [accounts, invoiceType]);

  const updateLine = (key: number, patch: Partial<FormLine>) => {
    setLines((prev) => prev.map((line) => (line.key === key ? { ...line, ...patch } : line)));
  };

  const lineTotals = lines.map((line) => {
    const qty = parseQuantity(line.quantity);
    if (qty === null || line.unitPriceToman === null) return null;
    const total = qty * line.unitPriceToman * 10;
    return Number.isInteger(total) ? total : null;
  });
  const grandTotal = lineTotals.every((t) => t !== null)
    ? lineTotals.reduce<number>((sum, t) => sum + (t ?? 0), 0)
    : null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitError(null);

    const party = Number(normalizeDigits(partyId).trim());
    if (!companyId) {
      setSubmitError("شرکتی انتخاب نشده است.");
      return;
    }
    if (!Number.isInteger(party) || party <= 0) {
      setSubmitError("شناسهٔ طرف حساب باید یک عدد مثبت باشد.");
      return;
    }
    if (!date) {
      setSubmitError("تاریخ فاکتور را انتخاب کنید.");
      return;
    }
    const invoiceAccount = Number(invoiceAccountId);
    if (!Number.isInteger(invoiceAccount) || invoiceAccount <= 0) {
      setSubmitError(invoiceType === "out" ? "حساب دریافتنی را انتخاب کنید." : "حساب پرداختنی را انتخاب کنید.");
      return;
    }
    if (grandTotal === null || grandTotal <= 0) {
      setSubmitError("مبلغ کل فاکتور باید بیشتر از صفر باشد؛ ردیف‌ها را بررسی کنید.");
      return;
    }
    const requestLines: CreateInvoiceDraftRequest["lines"] = [];
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const accountId = Number(line.accountId);
      const qty = parseQuantity(line.quantity);
      if (!Number.isInteger(accountId) || accountId <= 0) {
        setSubmitError(`حساب ردیف ${i + 1} را انتخاب کنید.`);
        return;
      }
      if (line.unitPriceToman === null || qty === null) {
        setSubmitError(`قیمت واحد و تعداد ردیف ${i + 1} را کامل کنید.`);
        return;
      }
      requestLines.push({
        account_id: accountId,
        unit_price: line.unitPriceToman * 10,
        quantity: qty,
        description: line.description,
      });
    }

    setSubmitting(true);
    try {
      const created = await api.createInvoiceDraft({
        company_id: companyId,
        invoice_type: invoiceType,
        party_id: party,
        date,
        description,
        reference,
        account_id: invoiceAccount,
        lines: requestLines,
      });
      toast.success("پیش‌نویس فاکتور ساخته شد.");
      router.push(`/invoices/${created.id}`);
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setSubmitError(persianApiErrorMessage(err, "ساخت پیش‌نویس فاکتور ناموفق بود."));
    } finally {
      setSubmitting(false);
    }
  };

  if (companiesLoading) {
    return (
      <div className="space-y-6" aria-label="در حال بارگذاری">
        <div className="h-8 w-40 animate-pulse rounded-lg bg-muted" />
        <div className="h-64 animate-pulse rounded-lg bg-muted" />
      </div>
    );
  }

  if (companiesError) {
    return (
      <ErrorState
        message={persianApiErrorMessage(companiesError, "دریافت شرکت‌ها ناموفق بود.")}
        onRetry={() => window.location.reload()}
      />
    );
  }

  if (companies.length === 0) {
    return <p className="text-sm text-muted-foreground">هیچ شرکتی در دسترس نیست.</p>;
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-6">
      <div className="flex items-center gap-3">
        {/* Plain Link styled as a button: Base UI's Button with
            render={<Link>} logs a nativeButton console error (US-305). */}
        <Link href="/invoices" className={buttonVariants({ variant: "ghost", size: "icon" })} aria-label="بازگشت به فاکتورها">
          <ArrowRight className="size-4" aria-hidden />
        </Link>
        <div>
          <h1 className="text-2xl font-bold">فاکتور جدید</h1>
          <p className="mt-1 text-sm text-muted-foreground">یک پیش‌نویس فاکتور در موتور حسابداری بسازید.</p>
        </div>
      </div>

      <CompanyPicker companies={companies} companyId={companyId} onChange={setCompanyId} />

      <Card>
        <CardHeader>
          <CardTitle>مشخصات فاکتور</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="invoice-type">نوع فاکتور</Label>
            <Select
              value={invoiceType}
              onValueChange={(v) => {
                setInvoiceType(v as "out" | "in");
                setInvoiceAccountId("");
              }}
            >
              <SelectTrigger id="invoice-type">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="out">{INVOICE_TYPE_LABELS.out} (مشتری)</SelectItem>
                <SelectItem value="in">{INVOICE_TYPE_LABELS.in} (تأمین‌کننده)</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="invoice-account">{invoiceType === "out" ? "حساب دریافتنی" : "حساب پرداختنی"}</Label>
            <Select value={invoiceAccountId} onValueChange={(v) => setInvoiceAccountId(v ?? "")}>
              <SelectTrigger id="invoice-account">
                <SelectValue placeholder="انتخاب حساب" />
              </SelectTrigger>
              <SelectContent>
                {invoiceAccounts.map((a) => (
                  <SelectItem key={a.id} value={String(a.id)}>
                    <span dir="ltr" className="ltr-isolate tabular-nums">
                      {a.code}
                    </span>
                    {" — "}
                    {a.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="invoice-party">شناسهٔ طرف حساب</Label>
            <Input
              id="invoice-party"
              inputMode="numeric"
              dir="ltr"
              value={partyId}
              onChange={(e) => setPartyId(e.target.value)}
              placeholder="123"
              className="text-start"
              aria-describedby="invoice-party-hint"
            />
            <p id="invoice-party-hint" className="text-xs text-muted-foreground">
              شناسهٔ عددی مشتری یا تأمین‌کننده در موتور حسابداری. جستجوی طرف حساب هنوز در دسترس نیست.
            </p>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="invoice-date">تاریخ فاکتور</Label>
            <JalaliDatePicker id="invoice-date" value={date} onChange={setDate} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="invoice-reference">مرجع</Label>
            <Input
              id="invoice-reference"
              dir="ltr"
              value={reference}
              onChange={(e) => setReference(e.target.value)}
              placeholder="شمارهٔ سفارش یا قرارداد"
              className="text-start"
            />
          </div>
          <div className="space-y-1.5 sm:col-span-2">
            <Label htmlFor="invoice-description">شرح</Label>
            <Textarea
              id="invoice-description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="شرح فاکتور…"
            />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>ردیف‌های فاکتور</CardTitle>
        </CardHeader>
        <CardContent className="space-y-6">
          {accountsError ? (
            <p role="alert" className="text-sm text-destructive">
              دریافت حساب‌ها ناموفق بود؛ بدون حساب نمی‌توان ردیف ساخت.
            </p>
          ) : null}
          {lines.map((line, i) => (
            <div key={line.key} className="space-y-4 rounded-lg border p-4">
              <div className="flex items-center justify-between gap-4">
                <h3 className="font-medium">ردیف {i + 1}</h3>
                {lines.length > 1 ? (
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    aria-label={`حذف ردیف ${i + 1}`}
                    onClick={() => setLines((prev) => prev.filter((l) => l.key !== line.key))}
                  >
                    <Trash2 className="size-4" />
                  </Button>
                ) : null}
              </div>
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-1.5">
                  <Label htmlFor={`line-account-${line.key}`}>حساب</Label>
                  <Select value={line.accountId} onValueChange={(v) => updateLine(line.key, { accountId: v ?? "" })}>
                    <SelectTrigger id={`line-account-${line.key}`}>
                      <SelectValue placeholder="انتخاب حساب" />
                    </SelectTrigger>
                    <SelectContent>
                      {accounts.map((a) => (
                        <SelectItem key={a.id} value={String(a.id)}>
                          <span dir="ltr" className="ltr-isolate tabular-nums">
                            {a.code}
                          </span>
                          {" — "}
                          {a.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor={`line-price-${line.key}`}>قیمت واحد</Label>
                  <AmountInput
                    id={`line-price-${line.key}`}
                    value={line.unitPriceToman}
                    onChange={(v) => updateLine(line.key, { unitPriceToman: v })}
                    unit="تومان"
                    min={0}
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor={`line-quantity-${line.key}`}>تعداد</Label>
                  <Input
                    id={`line-quantity-${line.key}`}
                    inputMode="decimal"
                    dir="ltr"
                    value={line.quantity}
                    onChange={(e) => updateLine(line.key, { quantity: e.target.value })}
                    className="text-start"
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor={`line-description-${line.key}`}>شرح ردیف</Label>
                  <Input
                    id={`line-description-${line.key}`}
                    value={line.description}
                    onChange={(e) => updateLine(line.key, { description: e.target.value })}
                    placeholder="شرح ردیف…"
                  />
                </div>
              </div>
              <p className="text-sm text-muted-foreground">
                مبلغ ردیف:{" "}
                {lineTotals[i] !== null ? (
                  <span className="font-medium text-foreground tabular-nums">{formatMoney(lineTotals[i]!)}</span>
                ) : (
                  "—"
                )}
              </p>
            </div>
          ))}
          <Button type="button" variant="outline" onClick={() => setLines((prev) => [...prev, EMPTY_LINE(lineKey++)])}>
            <Plus className="size-4" />
            افزودن ردیف
          </Button>
          <p className="border-t pt-4 text-sm">
            مبلغ کل:{" "}
            {grandTotal !== null ? (
              <span className="font-bold tabular-nums">{formatMoney(grandTotal)}</span>
            ) : (
              "—"
            )}
          </p>
        </CardContent>
      </Card>

      {submitError ? (
        <p role="alert" className="text-sm text-destructive">
          {submitError}
        </p>
      ) : null}

      <div className="flex gap-4">
        <Button type="submit" disabled={submitting}>
          {submitting ? "در حال ساخت…" : "ساخت پیش‌نویس"}
        </Button>
        <Link href="/invoices" className={buttonVariants({ variant: "outline" })}>
          انصراف
        </Link>
      </div>
    </form>
  );
}
