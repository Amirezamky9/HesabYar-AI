/**
 * Manual cheque registration (US-535; §5 §1.2 — the 24-field payload).
 *
 * A manually registered cheque lands in state `registered` with NO
 * journal — it is a claim until the leaf arrives and Sayyad confirms
 * it (confirm-receipt on the detail page). Amounts are entered in
 * Toman and converted ×10 to Rial at the backend boundary; dates
 * are picked Jalali and sent as Jalali YYYY-MM-DD.
 *
 * Issued cheques need a checkbook (engine id); collateral cheques
 * need the secured obligation (report 6's dimension). The company
 * comes from the shared company hook (row id or engine id — the
 * backend accepts both).
 */

"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { TriangleAlert } from "lucide-react";
import { ApiError, api, type CheckbookResponse } from "@/lib/api-client";
import { en } from "@/lib/utils";
import {
  Button,
  Card,
  CardContent,
  Checkbox,
  Input,
  JalaliDatePicker,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Textarea,
} from "@/components/ui";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import { isPermissionDenied, persianApiErrorMessage } from "@/features/shared/api-error";
import { useCompany } from "@/features/shared/use-company";
import { isoToJalaliString } from "./cheque-data";

function parseToman(raw: string): number | null {
  const cleaned = en(raw).replace(/[٬,،\s]/g, "").trim();
  if (!/^\d+$/.test(cleaned)) return null;
  const value = Number(cleaned);
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}

function digitsOnly(raw: string): string {
  return en(raw).replace(/\D/g, "");
}

export function ChequeNewPage() {
  const router = useRouter();
  const { companies, companyId, loading: companyLoading, error: companyError } = useCompany();
  const company = companies.find((c) => c.id === companyId) ?? null;

  const [kind, setKind] = React.useState("received");
  const [purpose, setPurpose] = React.useState("payment");
  const [sayadId, setSayadId] = React.useState("");
  const [amountToman, setAmountToman] = React.useState("");
  const [issueIso, setIssueIso] = React.useState<string | null>(null);
  const [dueIso, setDueIso] = React.useState<string | null>(null);
  const [issuerName, setIssuerName] = React.useState("");
  const [issuerNationalId, setIssuerNationalId] = React.useState("");
  const [issuerBank, setIssuerBank] = React.useState("");
  const [payeeName, setPayeeName] = React.useState("");
  const [payeeNationalId, setPayeeNationalId] = React.useState("");
  const [orderId, setOrderId] = React.useState("");
  const [customerRef, setCustomerRef] = React.useState("");
  const [sayadRegistered, setSayadRegistered] = React.useState(true);
  const [leafNumber, setLeafNumber] = React.useState("");
  const [issuerBranch, setIssuerBranch] = React.useState("");
  const [issuerAccountNumber, setIssuerAccountNumber] = React.useState("");
  const [reason, setReason] = React.useState("");
  const [invoiceId, setInvoiceId] = React.useState("");
  const [installmentLegId, setInstallmentLegId] = React.useState("");
  const [securedObligation, setSecuredObligation] = React.useState("");
  const [checkbookId, setCheckbookId] = React.useState("");
  const [notes, setNotes] = React.useState("");
  const [checkbooks, setCheckbooks] = React.useState<CheckbookResponse[]>([]);
  const [checkbooksError, setCheckbooksError] = React.useState(false);
  const [formError, setFormError] = React.useState<string | null>(null);
  const [submitting, setSubmitting] = React.useState(false);

  React.useEffect(() => {
    if (!company || kind !== "issued") return;
    let cancelled = false;
    api
      .listCheckbooks(company.engine_id)
      .then((rows) => {
        if (!cancelled) setCheckbooks(rows);
      })
      .catch(() => {
        if (!cancelled) setCheckbooksError(true);
      });
    return () => {
      cancelled = true;
    };
  }, [company, kind]);

  if (companyError && isPermissionDenied(companyError)) {
    return <PermissionDeniedState message="کلید شما دسترسی خواندن شرکت‌ها را ندارد." />;
  }
  if (companyError) {
    return <ErrorState message="دریافت فهرست شرکت‌ها ناموفق بود." onRetry={() => router.refresh()} />;
  }

  const validate = (): string | null => {
    if (!companyId) return "شرکتی انتخاب نشده است.";
    if (digitsOnly(sayadId).length !== 16) return "شناسهٔ صیاد باید ۱۶ رقم باشد.";
    if (parseToman(amountToman) === null) return "مبلغ باید عدد صحیح تومان بزرگ‌تر از صفر باشد.";
    if (!issueIso || !dueIso) return "تاریخ صدور و سررسید را انتخاب کنید.";
    if (isoToJalaliString(dueIso) < isoToJalaliString(issueIso)) return "سررسید نمی‌تواند پیش از تاریخ صدور باشد.";
    const issuerDigits = digitsOnly(issuerNationalId);
    if (issuerDigits.length !== 10 && issuerDigits.length !== 11) return "کد ملی/شناسهٔ صادرکننده باید ۱۰ رقم (حقیقی) یا ۱۱ رقم (حقوقی) باشد.";
    const payeeDigits = digitsOnly(payeeNationalId);
    if (payeeDigits.length !== 10 && payeeDigits.length !== 11) return "کد ملی/شناسهٔ ذی‌نفع باید ۱۰ رقم (حقیقی) یا ۱۱ رقم (حقوقی) باشد.";
    if (!issuerName.trim() || !issuerBank.trim() || !payeeName.trim()) return "نام صادرکننده، بانک صادرکننده و نام ذی‌نفع الزامی‌اند.";
    if (!orderId.trim() || !customerRef.trim()) return "شناسهٔ سفارش و مرجع مشتری الزامی‌اند.";
    if (kind === "issued" && !checkbookId) return "برای چک پرداختی، دسته‌چک را انتخاب کنید.";
    if (purpose === "collateral" && !securedObligation.trim()) return "برای چک پشتوانه، تعهد تحت پوشش را بنویسید.";
    return null;
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const problem = validate();
    if (problem) {
      setFormError(problem);
      return;
    }
    setFormError(null);
    setSubmitting(true);
    try {
      const created = await api.createCheque({
        company_id: companyId as string,
        sayad_id: digitsOnly(sayadId),
        amount_toman: parseToman(amountToman) as number,
        issue_date: isoToJalaliString(issueIso as string),
        due_date: isoToJalaliString(dueIso as string),
        issuer_name: issuerName.trim(),
        issuer_national_id: digitsOnly(issuerNationalId),
        issuer_bank: issuerBank.trim(),
        payee_name: payeeName.trim(),
        payee_national_id: digitsOnly(payeeNationalId),
        purpose,
        kind,
        checkbook_id: kind === "issued" ? Number(checkbookId) : null,
        secured_obligation: purpose === "collateral" ? securedObligation.trim() : null,
        order_id: orderId.trim(),
        customer_ref: customerRef.trim(),
        sayad_registered: sayadRegistered,
        leaf_number: leafNumber.trim() || null,
        issuer_branch: issuerBranch.trim() || null,
        issuer_account_number: issuerAccountNumber.trim() || null,
        reason: reason.trim() || null,
        invoice_id: invoiceId.trim() || null,
        installment_leg_id: installmentLegId.trim() || null,
        notes: notes.trim() || null,
      });
      router.push(`/cheques/${created.id}`);
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.push("/login");
        return;
      }
      setFormError(persianApiErrorMessage(err, "ثبت چک ناموفق بود."));
      setSubmitting(false);
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">ثبت چک جدید</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          ثبت دستی چک — شرکت: {companyLoading ? "…" : (company?.name ?? "—")} · چک ثبت‌شده بدون هیچ سندی در وضعیت «ثبت‌شده» می‌ماند تا برگ برسد و صیاد تأیید کند.
        </p>
      </div>

      {!sayadRegistered ? (
        <p className="flex items-start gap-2 rounded-lg border border-warning/40 bg-warning/10 px-4 py-3 text-sm text-warning-foreground" role="alert">
          <TriangleAlert className="mt-1 size-4 shrink-0" aria-hidden />
          صادرکننده برگ را در صیاد ثبت نکرده است — چک ثبت‌نشده را نپذیرید (رأی وحدت رویهٔ ۸۷۰).
        </p>
      ) : null}

      <form onSubmit={submit} className="space-y-6">
        <Card>
          <CardContent className="grid gap-4 pt-6 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="cheque-kind">نوع چک</Label>
              <Select value={kind} onValueChange={(v) => setKind(v ?? "received")}>
                <SelectTrigger id="cheque-kind">
                  <SelectValue>{kind === "received" ? "دریافتی (چک مشتری)" : "پرداختی (چک ما)"}</SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="received">دریافتی (چک مشتری)</SelectItem>
                  <SelectItem value="issued">پرداختی (چک ما)</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="cheque-purpose">هدف چک</Label>
              <Select value={purpose} onValueChange={(v) => setPurpose(v ?? "payment")}>
                <SelectTrigger id="cheque-purpose">
                  <SelectValue>{purpose === "payment" ? "تجاری" : "پشتوانه"}</SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="payment">تجاری</SelectItem>
                  <SelectItem value="collateral">پشتوانه (خارج از ترازنامه — حساب انتظامی)</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="cheque-sayad-id">شناسهٔ ۱۶رقمی صیاد</Label>
              <Input id="cheque-sayad-id" dir="ltr" inputMode="numeric" value={sayadId} onChange={(e) => setSayadId(e.target.value)} placeholder="1234567890123456" />
              <p className="text-xs text-muted-foreground">متفاوت از شمارهٔ سری برگ.</p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="cheque-amount">مبلغ (تومان)</Label>
              <Input id="cheque-amount" dir="ltr" inputMode="numeric" value={amountToman} onChange={(e) => setAmountToman(e.target.value)} placeholder="1250000" />
            </div>
            <div className="space-y-2">
              <Label htmlFor="cheque-issue-date">تاریخ صدور</Label>
              <JalaliDatePicker id="cheque-issue-date" value={issueIso} onChange={setIssueIso} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="cheque-due-date">سررسید</Label>
              <JalaliDatePicker id="cheque-due-date" value={dueIso} onChange={setDueIso} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="cheque-issuer-name">نام صادرکننده</Label>
              <Input id="cheque-issuer-name" value={issuerName} onChange={(e) => setIssuerName(e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="cheque-issuer-national">کد ملی / شناسهٔ صادرکننده</Label>
              <Input id="cheque-issuer-national" dir="ltr" inputMode="numeric" value={issuerNationalId} onChange={(e) => setIssuerNationalId(e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="cheque-issuer-bank">بانک صادرکننده</Label>
              <Input id="cheque-issuer-bank" value={issuerBank} onChange={(e) => setIssuerBank(e.target.value)} placeholder="056 سامان" />
            </div>
            <div className="space-y-2">
              <Label htmlFor="cheque-leaf">شمارهٔ سری برگ (اختیاری)</Label>
              <Input id="cheque-leaf" dir="ltr" value={leafNumber} onChange={(e) => setLeafNumber(e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="cheque-payee-name">نام ذی‌نفع (ثبت‌شده در صیاد)</Label>
              <Input id="cheque-payee-name" value={payeeName} onChange={(e) => setPayeeName(e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="cheque-payee-national">کد ملی / شناسهٔ ذی‌نفع</Label>
              <Input id="cheque-payee-national" dir="ltr" inputMode="numeric" value={payeeNationalId} onChange={(e) => setPayeeNationalId(e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="cheque-order">شناسهٔ سفارش</Label>
              <Input id="cheque-order" dir="ltr" value={orderId} onChange={(e) => setOrderId(e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="cheque-customer">مرجع مشتری</Label>
              <Input id="cheque-customer" dir="ltr" value={customerRef} onChange={(e) => setCustomerRef(e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="cheque-branch">شعبهٔ بانک صادرکننده (اختیاری)</Label>
              <Input id="cheque-branch" value={issuerBranch} onChange={(e) => setIssuerBranch(e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="cheque-account-number">شماره حساب جاری صادرکننده (اختیاری)</Label>
              <Input id="cheque-account-number" dir="ltr" value={issuerAccountNumber} onChange={(e) => setIssuerAccountNumber(e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="cheque-reason">بابت (اختیاری)</Label>
              <Input id="cheque-reason" value={reason} onChange={(e) => setReason(e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="cheque-invoice">فاکتور مرتبط (اختیاری)</Label>
              <Input id="cheque-invoice" dir="ltr" value={invoiceId} onChange={(e) => setInvoiceId(e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="cheque-leg">قسط مرتبط (اختیاری)</Label>
              <Input id="cheque-leg" dir="ltr" value={installmentLegId} onChange={(e) => setInstallmentLegId(e.target.value)} />
            </div>
            {purpose === "collateral" ? (
              <div className="space-y-2">
                <Label htmlFor="cheque-obligation">تعهد تحت پوشش</Label>
                <Input id="cheque-obligation" value={securedObligation} onChange={(e) => setSecuredObligation(e.target.value)} placeholder="مثلاً قرارداد فروش اقساطی ۱۴۰۵/۱۲۳" />
              </div>
            ) : null}
            {kind === "issued" ? (
              <div className="space-y-2">
                <Label htmlFor="cheque-checkbook">دسته‌چک</Label>
                {checkbooksError ? (
                  <p className="text-sm text-destructive">دریافت دسته‌چک‌ها ناموفق بود.</p>
                ) : (
                  <Select value={checkbookId} onValueChange={(v) => setCheckbookId(v ?? "")}>
                    <SelectTrigger id="cheque-checkbook">
                      <SelectValue placeholder="انتخاب دسته‌چک">
                        {checkbooks.find((c) => String(c.checkbook_engine_id) === checkbookId)?.name ?? "انتخاب دسته‌چک"}
                      </SelectValue>
                    </SelectTrigger>
                    <SelectContent>
                      {checkbooks.map((c) => (
                        <SelectItem key={c.checkbook_engine_id} value={String(c.checkbook_engine_id)}>
                          {c.name} — {c.remaining_leaves} برگ باقی‌مانده
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              </div>
            ) : null}
            <div className="space-y-2 sm:col-span-2">
              <Label htmlFor="cheque-notes">یادداشت (اختیاری)</Label>
              <Textarea id="cheque-notes" value={notes} onChange={(e) => setNotes(e.target.value)} />
            </div>
            <div className="flex items-center gap-2 sm:col-span-2">
              <Checkbox id="cheque-sayad-registered" checked={sayadRegistered} onCheckedChange={(checked) => setSayadRegistered(checked === true)} />
              <Label htmlFor="cheque-sayad-registered">صادرکننده برگ را در صیاد ثبت کرده است</Label>
            </div>
          </CardContent>
        </Card>

        {formError ? (
          <p className="rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive" role="alert">
            {formError}
          </p>
        ) : null}

        <div className="flex gap-2">
          <Button type="submit" disabled={submitting || companyLoading}>
            {submitting ? "در حال ثبت…" : "ثبت چک"}
          </Button>
          <Button type="button" variant="outline" onClick={() => router.push("/cheques")}>
            انصراف
          </Button>
        </div>
      </form>
    </div>
  );
}
