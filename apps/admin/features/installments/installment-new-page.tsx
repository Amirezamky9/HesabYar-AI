/**
 * New installment plan (US-545, Phase 5-5 ۵ج).
 *
 * One invoice, one schedule — terms-based (equal split, the
 * remainder on the last leg) or explicit legs, never both. A BNPL
 * plan pins exactly one gateway channel and never carries a sale
 * contract; an internal plan may name a contract code (the version
 * in force is selected at creation and governs the plan for its
 * whole life). Money the operator types is Toman.
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Plus, Trash2 } from "lucide-react";
import { ApiError, api, type PaymentChannelResponse } from "@/lib/api-client";
import {
  AmountInput,
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
  Skeleton,
  Tabs,
  TabsList,
  TabsTrigger,
} from "@/components/ui";
import { faNumber } from "@/lib/utils";
import { ErrorState } from "@/features/shared/states";
import { persianApiErrorMessage } from "@/features/shared/api-error";
import { CompanyPicker } from "@/features/shared/company-picker";
import { useCompany } from "@/features/shared/use-company";
import { isoToJalaliString } from "../cheques/cheque-data";

type ScheduleMode = "terms" | "explicit";

interface ExplicitLegDraft {
  key: number;
  dueIso: string | null;
  principalToman: number | null;
}

let legKey = 0;

function newLegDraft(): ExplicitLegDraft {
  legKey += 1;
  return { key: legKey, dueIso: null, principalToman: null };
}

export function InstallmentNewPage() {
  const router = useRouter();
  const { companies, companyId, setCompanyId, loading: companiesLoading, error: companiesError } =
    useCompany();
  const [channels, setChannels] = React.useState<PaymentChannelResponse[]>([]);
  const [channelsError, setChannelsError] = React.useState(false);

  const [invoiceId, setInvoiceId] = React.useState("");
  const [customerRef, setCustomerRef] = React.useState("");
  const [kind, setKind] = React.useState<"internal" | "bnpl">("internal");
  const [gatewayChannelKey, setGatewayChannelKey] = React.useState("");
  const [contractCode, setContractCode] = React.useState("");
  const [mode, setMode] = React.useState<ScheduleMode>("terms");
  const [totalToman, setTotalToman] = React.useState<number | null>(null);
  const [legCount, setLegCount] = React.useState("3");
  const [intervalDays, setIntervalDays] = React.useState("30");
  const [firstDueIso, setFirstDueIso] = React.useState<string | null>(null);
  const [legs, setLegs] = React.useState<ExplicitLegDraft[]>([newLegDraft(), newLegDraft()]);
  const [autoPost, setAutoPost] = React.useState(false);
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<unknown>(null);

  React.useEffect(() => {
    let cancelled = false;
    api
      .listPaymentChannels({ enabled: true, limit: 50 })
      .then((page) => {
        if (cancelled) return;
        setChannels(page.items.filter((c) => c.enabled));
      })
      .catch(() => {
        if (!cancelled) setChannelsError(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const validationError = (): string | null => {
    if (!companyId) return "شرکتی انتخاب نشده است؛ ابتدا یک شرکت انتخاب کنید.";
    if (!invoiceId.trim()) return "شناسهٔ فاکتور الزامی است؛ هر برنامه به دقیقاً یک فاکتور فروش تعلق دارد.";
    if (kind === "bnpl" && !gatewayChannelKey.trim())
      return "برنامهٔ BNPL دقیقاً یک درگاه لازم دارد؛ درگاه فروش را انتخاب کنید.";
    if (mode === "terms") {
      if (!totalToman || totalToman <= 0) return "اصل کل باید بزرگ‌تر از صفر باشد.";
      const count = Number(legCount);
      if (!Number.isInteger(count) || count < 1) return "تعداد اقساط باید عدد صحیح بزرگ‌تر از صفر باشد.";
      const interval = Number(intervalDays);
      if (!Number.isInteger(interval) || interval < 1) return "فاصلهٔ اقساط باید عدد صحیح روز بزرگ‌تر از صفر باشد.";
      if (!firstDueIso) return "تاریخ نخستین سررسید را انتخاب کنید.";
    } else {
      if (legs.length < 1) return "دست‌کم یک قسط لازم است.";
      for (const [index, draft] of legs.entries()) {
        if (!draft.dueIso) return `سررسید قسط ${faNumber(index + 1)} را انتخاب کنید.`;
        if (!draft.principalToman || draft.principalToman <= 0)
          return `اصل قسط ${faNumber(index + 1)} باید بزرگ‌تر از صفر باشد.`;
      }
    }
    return null;
  };

  const submit = async () => {
    const problem = validationError();
    if (problem) {
      setError(new Error(problem));
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const created = await api.createInstallmentPlan({
        company_id: companyId as string,
        invoice_id: invoiceId.trim(),
        customer_ref: customerRef.trim() || undefined,
        kind,
        gateway_channel_key: kind === "bnpl" ? gatewayChannelKey.trim() : undefined,
        contract_code: kind === "internal" && contractCode.trim() ? contractCode.trim() : undefined,
        auto_post: autoPost,
        ...(mode === "terms"
          ? {
              total_principal_toman: totalToman as number,
              leg_count: Number(legCount),
              interval_days: Number(intervalDays),
              first_due_date: isoToJalaliString(firstDueIso as string),
            }
          : {
              legs: legs.map((draft) => ({
                due_date: isoToJalaliString(draft.dueIso as string),
                principal_toman: draft.principalToman as number,
              })),
            }),
      });
      router.push(`/installments/${created.id}`);
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.push("/login");
        return;
      }
      setError(err);
      setSubmitting(false);
    }
  };

  if (companiesError) {
    return (
      <ErrorState
        message={persianApiErrorMessage(companiesError, "دریافت فهرست شرکت‌ها ناموفق بود.")}
        onRetry={() => window.location.reload()}
      />
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">برنامهٔ قسط جدید</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          یک فاکتور، یک برنامه؛ اقساط یا از شرایط پرداخت ساخته می‌شوند (تقسیم مساوی، باقی‌مانده روی آخرین قسط) یا
          صریح وارد می‌شوند. هر فاکتور فقط یک برنامه دارد.
        </p>
      </div>

      {companiesLoading ? (
        <div className="space-y-2" aria-label="در حال بارگذاری">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      ) : (
        <Card>
          <CardContent className="space-y-6 pt-6">
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-2">
                <Label>شرکت</Label>
                <CompanyPicker companies={companies} companyId={companyId} onChange={setCompanyId} />
              </div>
              <div className="space-y-2">
                <Label htmlFor="installment-invoice-id">شناسهٔ فاکتور فروش</Label>
                <Input
                  id="installment-invoice-id"
                  value={invoiceId}
                  onChange={(event) => setInvoiceId(event.target.value)}
                  placeholder="مثلاً INV-1405-1234"
                  dir="ltr"
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="installment-customer-ref">مرجع مشتری</Label>
                <Input
                  id="installment-customer-ref"
                  value={customerRef}
                  onChange={(event) => setCustomerRef(event.target.value)}
                  placeholder="اختیاری — شناسهٔ مشتری در فروشگاه"
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="installment-kind">نوع برنامه</Label>
                <Select
                  value={kind}
                  onValueChange={(value) => setKind((value as "internal" | "bnpl") ?? "internal")}
                >
                  <SelectTrigger id="installment-kind">
                    <SelectValue>{kind === "bnpl" ? "BNPL (درگاه)" : "داخلی"}</SelectValue>
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="internal">داخلی — مشتری به فروشنده می‌پردازد</SelectItem>
                    <SelectItem value="bnpl">BNPL — درگاه اقساط را می‌پردازد</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              {kind === "bnpl" ? (
                <div className="space-y-2">
                  <Label htmlFor="installment-gateway">درگاه BNPL</Label>
                  {channelsError ? (
                    <>
                      <Input
                        id="installment-gateway"
                        value={gatewayChannelKey}
                        onChange={(event) => setGatewayChannelKey(event.target.value)}
                        placeholder="مثلاً snapppay"
                        dir="ltr"
                      />
                      <p className="text-xs text-muted-foreground">
                        فهرست درگاه‌ها دریافت نشد؛ کلید درگاه را دستی وارد کنید.
                      </p>
                    </>
                  ) : (
                    <Select value={gatewayChannelKey || undefined} onValueChange={(value) => setGatewayChannelKey(value ?? "")}>
                      <SelectTrigger id="installment-gateway">
                        <SelectValue placeholder="انتخاب درگاه" />
                      </SelectTrigger>
                      <SelectContent>
                        {channels.map((channel) => (
                          <SelectItem key={channel.channel_key} value={channel.channel_key}>
                            {channel.display_name_fa}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                </div>
              ) : (
                <div className="space-y-2">
                  <Label htmlFor="installment-contract-code">کد قرارداد فروش اقساطی</Label>
                  <Input
                    id="installment-contract-code"
                    value={contractCode}
                    onChange={(event) => setContractCode(event.target.value)}
                    placeholder="اختیاری — نسخهٔ جاری قرارداد اعمال می‌شود"
                    dir="ltr"
                  />
                  <p className="text-xs text-muted-foreground">
                    قرارداد نرخ سود، جریمهٔ تأخیر و مراحل پیگیری را تعیین می‌کند؛ برنامهٔ BNPL قرارداد ندارد.
                  </p>
                </div>
              )}
            </div>

            <div className="space-y-4">
              <Tabs value={mode} onValueChange={(value) => setMode(value as ScheduleMode)}>
                <TabsList aria-label="روش ساخت برنامه">
                  <TabsTrigger value="terms">از شرایط پرداخت</TabsTrigger>
                  <TabsTrigger value="explicit">اقساط صریح</TabsTrigger>
                </TabsList>
              </Tabs>

              {mode === "terms" ? (
                <div className="grid gap-4 sm:grid-cols-2">
                  <div className="space-y-2">
                    <Label htmlFor="installment-total">اصل کل (تومان)</Label>
                    <AmountInput id="installment-total" value={totalToman} onChange={setTotalToman} min={1} />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="installment-leg-count">تعداد اقساط</Label>
                    <Input
                      id="installment-leg-count"
                      inputMode="numeric"
                      value={legCount}
                      onChange={(event) => setLegCount(event.target.value.replace(/[^0-9]/g, ""))}
                      dir="ltr"
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="installment-interval-days">فاصلهٔ اقساط (روز)</Label>
                    <Input
                      id="installment-interval-days"
                      inputMode="numeric"
                      value={intervalDays}
                      onChange={(event) => setIntervalDays(event.target.value.replace(/[^0-9]/g, ""))}
                      dir="ltr"
                    />
                    <p className="text-xs text-muted-foreground">
                      فاصله به روز است، نه ماه؛ ۳۰ روز یعنی «ماهانه». محاسبهٔ ماه شمسی در انتهای ماه مبهم است.
                    </p>
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="installment-first-due-date">نخستین سررسید</Label>
                    <JalaliDatePicker
                      id="installment-first-due-date"
                      value={firstDueIso}
                      onChange={setFirstDueIso}
                      placeholder="انتخاب تاریخ"
                    />
                  </div>
                </div>
              ) : (
                <div className="space-y-4">
                  {legs.map((draft, index) => (
                    <div key={draft.key} className="grid items-end gap-4 rounded-lg border p-4 sm:grid-cols-3">
                      <div className="space-y-2">
                        <Label htmlFor={`installment-leg-${index}-due`}>سررسید قسط {faNumber(index + 1)}</Label>
                        <JalaliDatePicker
                          id={`installment-leg-${index}-due`}
                          value={draft.dueIso}
                          onChange={(iso) =>
                            setLegs((current) =>
                              current.map((item) => (item.key === draft.key ? { ...item, dueIso: iso } : item)),
                            )
                          }
                          placeholder="انتخاب تاریخ"
                        />
                      </div>
                      <div className="space-y-2">
                        <Label htmlFor={`installment-leg-${index}-principal`}>اصل قسط {faNumber(index + 1)} (تومان)</Label>
                        <AmountInput
                          id={`installment-leg-${index}-principal`}
                          value={draft.principalToman}
                          onChange={(value) =>
                            setLegs((current) =>
                              current.map((item) =>
                                item.key === draft.key ? { ...item, principalToman: value } : item,
                              ),
                            )
                          }
                          min={1}
                        />
                      </div>
                      <div>
                        <Button
                          type="button"
                          variant="outline"
                          disabled={legs.length <= 1}
                          onClick={() => setLegs((current) => current.filter((item) => item.key !== draft.key))}
                          aria-label={`حذف قسط ${faNumber(index + 1)}`}
                        >
                          <Trash2 className="size-4" aria-hidden />
                          حذف
                        </Button>
                      </div>
                    </div>
                  ))}
                  <Button type="button" variant="outline" onClick={() => setLegs((current) => [...current, newLegDraft()])}>
                    <Plus className="size-4" aria-hidden />
                    افزودن قسط
                  </Button>
                </div>
              )}
            </div>

            <div className="flex items-center gap-2">
              <Checkbox
                id="installment-auto-post"
                checked={autoPost}
                onCheckedChange={(checked) => setAutoPost(checked === true)}
              />
              <Label htmlFor="installment-auto-post">
                ثبت خودکار سندهای جریمه و سود (در غیر این صورت پیش‌نویس می‌مانند)
              </Label>
            </div>

            {error ? (
              <p role="alert" className="rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
                {error instanceof Error && !(error instanceof ApiError)
                  ? error.message
                  : persianApiErrorMessage(error, "ساخت برنامهٔ قسط ناموفق بود.")}
              </p>
            ) : null}

            <div className="flex flex-wrap gap-2">
              <Button onClick={submit} disabled={submitting || companiesLoading}>
                {submitting ? "در حال ساخت…" : "ساخت برنامه"}
              </Button>
              <Link
                href="/installments"
                className="inline-flex min-h-11 items-center rounded-md border px-4 text-sm font-medium"
              >
                انصراف
              </Link>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
