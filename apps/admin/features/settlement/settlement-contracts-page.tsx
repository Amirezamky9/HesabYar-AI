/**
 * Gateway contract version management (US-525).
 *
 * A rate change is always a NEW version with an effective date — the
 * previous version is superseded, never rewritten, and past
 * settlements keep the old rate. The fee % is entered MANUALLY by the
 * operator (never hard-coded, never defaulted from another channel);
 * the settlement model and tranche legs are editable per contract.
 *
 * After a new version, re-deriving open schedules is an EXPLICIT
 * action (actor + reason, audit-logged) — never automatic.
 */

"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import {
  ApiError,
  api,
  type BulkRederiveResponse,
  type GatewayContractResponse,
  type PaymentChannelResponse,
} from "@/lib/api-client";
import { formatIsoDate } from "@/lib/format-date";
import { formatMoney, normalizeDigits, tomanToIrr } from "@/lib/format-money";
import { faNumber } from "@/lib/utils";
import {
  AmountInput,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Checkbox,
  DataTable,
  Input,
  JalaliDatePicker,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Skeleton,
  StatusBadge,
  Switch,
  Textarea,
  type DataTableColumn,
} from "@/components/ui";
import {
  isPermissionDenied,
  persianApiErrorMessage,
} from "@/features/shared/api-error";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import {
  autoPostLabel,
  feeAccrualLabel,
  feeTimingLabel,
  formatPercentString,
  parseDecimalString,
  settlementModelLabel,
  todayIsoTehran,
} from "./settlement-data";

interface LegDraft {
  month_offset: string;
  payout_day: string;
  percent: string;
}

const EMPTY_LEG: LegDraft = { month_offset: "1", payout_day: "5", percent: "100" };

const SETTLEMENT_MODELS = [
  { value: "tranches_4", label: "۴ قسط ماهانه" },
  { value: "monthly_lump", label: "یکجا ماهانه" },
  { value: "daily", label: "روزانه" },
  { value: "__custom__", label: "مدل دیگر (متنی)" },
];

function legsSum(legs: LegDraft[]): number | null {
  let sum = 0;
  for (const leg of legs) {
    const percent = parseDecimalString(normalizeDigits(leg.percent.trim()));
    if (percent === null) return null;
    sum += percent;
  }
  return sum;
}

export function SettlementContractsPage() {
  const router = useRouter();
  const [channels, setChannels] = React.useState<PaymentChannelResponse[]>([]);
  const [channelKey, setChannelKey] = React.useState("");
  const [contracts, setContracts] = React.useState<GatewayContractResponse[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<unknown>(null);
  const [reloadKey, setReloadKey] = React.useState(0);

  // New-version form (fee % is manual — no default, no prefill).
  const [showForm, setShowForm] = React.useState(false);
  const [validFrom, setValidFrom] = React.useState<string | null>(todayIsoTehran());
  const [feePercent, setFeePercent] = React.useState("");
  const [feeVatRate, setFeeVatRate] = React.useState("");
  const [feeVatClaimable, setFeeVatClaimable] = React.useState(false);
  const [feeTiming, setFeeTiming] = React.useState("proportional");
  const [feeAccrual, setFeeAccrual] = React.useState("at_sale");
  const [settlementModel, setSettlementModel] = React.useState("tranches_4");
  const [customModel, setCustomModel] = React.useState("");
  const [legs, setLegs] = React.useState<LegDraft[]>([{ ...EMPTY_LEG }]);
  const [windowStart, setWindowStart] = React.useState("");
  const [windowEnd, setWindowEnd] = React.useState("");
  const [payoutCalendar, setPayoutCalendar] = React.useState("jalali");
  const [workingDayAdjustment, setWorkingDayAdjustment] = React.useState(false);
  const [dayLag, setDayLag] = React.useState("");
  const [graceDays, setGraceDays] = React.useState("0");
  const [matchToleranceToman, setMatchToleranceToman] = React.useState<number | null>(null);
  const [tieoutToleranceToman, setTieoutToleranceToman] = React.useState<number | null>(null);
  const [autoPostPolicy, setAutoPostPolicy] = React.useState("draft_only");
  const [autoPostCapToman, setAutoPostCapToman] = React.useState<number | null>(null);
  const [sourceNote, setSourceNote] = React.useState("");
  const [formError, setFormError] = React.useState<string | null>(null);
  const [formBusy, setFormBusy] = React.useState(false);
  const [createdVersion, setCreatedVersion] = React.useState<number | null>(null);

  // Bulk re-derive (explicit only).
  const [rederiveActor, setRederiveActor] = React.useState("");
  const [rederiveReason, setRederiveReason] = React.useState("");
  const [rederiveBusy, setRederiveBusy] = React.useState(false);
  const [rederiveResult, setRederiveResult] = React.useState<BulkRederiveResponse | null>(null);
  const [rederiveError, setRederiveError] = React.useState<string | null>(null);

  const [channelBusy, setChannelBusy] = React.useState(false);
  const [channelError, setChannelError] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const channelPage = await api.listPaymentChannels({ limit: 100 });
      setChannels(channelPage.items);
      const key = channelKey !== "" ? channelKey : (channelPage.items[0]?.channel_key ?? "");
      if (key !== "") {
        setChannelKey(key);
        const contractPage = await api.listGatewayContracts(key, { limit: 100, sort: "-version" });
        setContracts(contractPage.items);
      }
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [channelKey, router]);

  // Loading state is set by load() from a timer callback, never
  // synchronously inside this effect (react-hooks/set-state-in-effect).
  React.useEffect(() => {
    const timer = setTimeout(() => {
      void load();
    }, 0);
    return () => clearTimeout(timer);
  }, [load, reloadKey]);

  const activeContract = contracts.find((c) => c.status === "active") ?? null;
  const selectedChannel = channels.find((c) => c.channel_key === channelKey) ?? null;

  const handleChannelChange = (key: string) => {
    setChannelKey(key);
    setCreatedVersion(null);
    setRederiveResult(null);
  };

  const handleToggleChannel = async (enabled: boolean) => {
    if (!selectedChannel) return;
    setChannelBusy(true);
    setChannelError(null);
    try {
      const updated = await api.updatePaymentChannel(selectedChannel.channel_key, { enabled });
      setChannels((prev) => prev.map((c) => (c.id === updated.id ? updated : c)));
    } catch (err: unknown) {
      setChannelError(persianApiErrorMessage(err, "تغییر وضعیت کانال ناموفق بود."));
    } finally {
      setChannelBusy(false);
    }
  };

  const fillFourEqualLegs = () => {
    setLegs([
      { month_offset: "1", payout_day: "5", percent: "25" },
      { month_offset: "2", payout_day: "5", percent: "25" },
      { month_offset: "3", payout_day: "5", percent: "25" },
      { month_offset: "4", payout_day: "5", percent: "25" },
    ]);
  };

  const validateForm = (): string | null => {
    if (validFrom === null) return "تاریخ اثر نسخه جدید الزامی است.";
    const fee = parseDecimalString(normalizeDigits(feePercent.trim()));
    if (fee === null || fee <= 0 || fee > 100) return "درصد کارمزد را به‌صورت عددی بین ۰ تا ۱۰۰ وارد کنید.";
    if (feeVatRate.trim() !== "") {
      const vat = parseDecimalString(normalizeDigits(feeVatRate.trim()));
      if (vat === null || vat < 0 || vat > 100) return "نرخ مالیات کارمزد باید عددی بین ۰ تا ۱۰۰ باشد.";
    }
    if (settlementModel === "__custom__" && customModel.trim() === "") {
      return "نام مدل تسویه را وارد کنید.";
    }
    if (legs.length === 0) return "دست‌کم یک قسط (leg) لازم است.";
    for (const leg of legs) {
      const offset = Number(normalizeDigits(leg.month_offset.trim()));
      const day = Number(normalizeDigits(leg.payout_day.trim()));
      const percent = parseDecimalString(normalizeDigits(leg.percent.trim()));
      if (!Number.isInteger(offset) || offset < 0) return "فاصلهٔ ماه هر قسط باید عدد صحیح ۰ یا بیشتر باشد.";
      if (!Number.isInteger(day) || day < 1 || day > 31) return "روز واریز هر قسط باید عددی بین ۱ تا ۳۱ باشد.";
      if (percent === null || percent <= 0 || percent > 100) return "سهم هر قسط باید عددی بین ۰ تا ۱۰۰ باشد.";
    }
    const sum = legsSum(legs);
    if (sum === null || Math.abs(sum - 100) > 0.001) {
      return `مجموع سهم قسط‌ها باید ۱۰۰٪ باشد (اکنون ${sum === null ? "—" : faNumber(sum)}٪).`;
    }
    const start = windowStart.trim() === "" ? null : Number(normalizeDigits(windowStart.trim()));
    const end = windowEnd.trim() === "" ? null : Number(normalizeDigits(windowEnd.trim()));
    if ((start !== null && (!Number.isInteger(start) || start < 1 || start > 31)) ||
        (end !== null && (!Number.isInteger(end) || end < 1 || end > 31))) {
      return "روزهای پنجرهٔ واریز باید عددی بین ۱ تا ۳۱ باشند.";
    }
    if (dayLag.trim() !== "") {
      const lag = Number(normalizeDigits(dayLag.trim()));
      if (!Number.isInteger(lag) || lag < 0) return "تأخیر روزانه باید عدد صحیح ۰ یا بیشتر باشد.";
    }
    const grace = Number(normalizeDigits(graceDays.trim()));
    if (!Number.isInteger(grace) || grace < 0) return "مهلت پس از سررسید باید عدد صحیح ۰ یا بیشتر باشد.";
    return null;
  };

  const handleCreateVersion = async () => {
    const problem = validateForm();
    if (problem) {
      setFormError(problem);
      return;
    }
    setFormBusy(true);
    setFormError(null);
    setCreatedVersion(null);
    try {
      const fee = parseDecimalString(normalizeDigits(feePercent.trim()));
      const vat = feeVatRate.trim() === "" ? null : parseDecimalString(normalizeDigits(feeVatRate.trim()));
      const created = await api.createGatewayContract(channelKey, {
        valid_from: validFrom as string,
        fee_percent: String(fee),
        fee_vat_rate: vat === null ? null : String(vat),
        fee_vat_claimable: feeVatClaimable,
        fee_timing: feeTiming,
        fee_accrual_policy: feeAccrual,
        settlement_model: settlementModel === "__custom__" ? customModel.trim() : settlementModel,
        tranche_legs: legs.map((leg) => ({
          month_offset: Number(normalizeDigits(leg.month_offset.trim())),
          payout_day: Number(normalizeDigits(leg.payout_day.trim())),
          percent: String(parseDecimalString(normalizeDigits(leg.percent.trim()))),
        })),
        payout_window_start_day: windowStart.trim() === "" ? null : Number(normalizeDigits(windowStart.trim())),
        payout_window_end_day: windowEnd.trim() === "" ? null : Number(normalizeDigits(windowEnd.trim())),
        payout_calendar: payoutCalendar,
        working_day_adjustment: workingDayAdjustment,
        day_lag: dayLag.trim() === "" ? null : Number(normalizeDigits(dayLag.trim())),
        grace_days_after_due: Number(normalizeDigits(graceDays.trim())),
        match_tolerance_irr: matchToleranceToman === null ? 0 : tomanToIrr(matchToleranceToman),
        tieout_tolerance_irr: tieoutToleranceToman === null ? 0 : tomanToIrr(tieoutToleranceToman),
        auto_post_policy: autoPostPolicy,
        auto_post_max_amount_irr: autoPostCapToman === null ? null : tomanToIrr(autoPostCapToman),
        source_note: sourceNote.trim() === "" ? null : sourceNote.trim(),
      });
      setCreatedVersion(created.version);
      setShowForm(false);
      setReloadKey((k) => k + 1);
    } catch (err: unknown) {
      setFormError(persianApiErrorMessage(err, "ایجاد نسخهٔ جدید قرارداد ناموفق بود."));
    } finally {
      setFormBusy(false);
    }
  };

  const handleRederive = async () => {
    if (rederiveActor.trim() === "" || rederiveReason.trim() === "") {
      setRederiveError("اقدام‌کننده و دلیل بازمشتق الزامی‌اند.");
      return;
    }
    setRederiveBusy(true);
    setRederiveError(null);
    setRederiveResult(null);
    try {
      const result = await api.bulkRederivePayments({
        channel_key: channelKey,
        actor: rederiveActor.trim(),
        reason: rederiveReason.trim(),
      });
      setRederiveResult(result);
    } catch (err: unknown) {
      setRederiveError(persianApiErrorMessage(err, "بازمشتق برنامه‌ها ناموفق بود."));
    } finally {
      setRederiveBusy(false);
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

  if (error && isPermissionDenied(error)) {
    return (
      <PermissionDeniedState
        message={persianApiErrorMessage(error, "دسترسی به قراردادها ممکن نیست.")}
      />
    );
  }

  const versionColumns: DataTableColumn<GatewayContractResponse>[] = [
    {
      key: "version",
      header: "نسخه",
      render: (row) => (
        <span className="flex items-center gap-2">
          {faNumber(row.version)}
          <StatusBadge
            status={row.status}
            label={row.status === "active" ? "فعال" : "جایگزین‌شده"}
            tone={row.status === "active" ? "success" : "outline"}
          />
        </span>
      ),
    },
    { key: "validFrom", header: "مؤثر از", render: (row) => formatIsoDate(row.valid_from) },
    { key: "validTo", header: "تا", render: (row) => (row.valid_to ? formatIsoDate(row.valid_to) : "—") },
    { key: "fee", header: "کارمزد", render: (row) => formatPercentString(row.fee_percent), className: "text-end", headerClassName: "text-end" },
    { key: "vat", header: "مالیات کارمزد", render: (row) => formatPercentString(row.fee_vat_rate), className: "text-end", headerClassName: "text-end" },
    { key: "model", header: "مدل تسویه", render: (row) => settlementModelLabel(row.settlement_model) },
    {
      key: "legs",
      header: "قسط‌ها",
      render: (row) =>
        row.tranche_legs.map((leg) => `${formatPercentString(leg.percent)} · ماه ${faNumber(leg.month_offset)}، روز ${faNumber(leg.payout_day)}`).join(" — "),
    },
    { key: "autoPost", header: "ثبت خودکار", render: (row) => autoPostLabel(row.auto_post_policy) },
  ];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">قراردادهای درگاه</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          نسخه‌های قرارداد هر کانال: تغییر نرخ یا مدل تسویه همیشه یک نسخهٔ جدید
          با تاریخ اثر است؛ تسویه‌های گذشته نرخ قدیم را حفظ می‌کنند.
        </p>
      </div>

      {error ? (
        <ErrorState
          message={persianApiErrorMessage(error, "بارگذاری قراردادها ناموفق بود.")}
          onRetry={() => setReloadKey((k) => k + 1)}
        />
      ) : null}

      {createdVersion !== null ? (
        <p className="text-sm text-muted-foreground">
          {createdVersion === 1
            ? "نخستین نسخهٔ قرارداد ساخته شد."
            : `نسخهٔ ${faNumber(createdVersion)} قرارداد ساخته شد و نسخهٔ پیشین جایگزین شد. تسویه‌های گذشته با نرخ قدیم می‌مانند.`}
        </p>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>کانال پرداخت</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="contract-channel">کانال</Label>
              <Select value={channelKey} onValueChange={(v) => handleChannelChange(v ?? "")}>
                <SelectTrigger id="contract-channel">
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
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {selectedChannel ? (
              <div className="space-y-1.5">
                <Label htmlFor="channel-enabled">کانال فعال است</Label>
                <div className="flex items-center gap-2">
                  <Switch
                    id="channel-enabled"
                    checked={selectedChannel.enabled}
                    onCheckedChange={(checked) => void handleToggleChannel(checked)}
                    disabled={channelBusy}
                  />
                  <span className="text-sm text-muted-foreground">
                    {selectedChannel.enabled ? "فعال" : "غیرفعال"}
                    {selectedChannel.status === "setup" ? " · در حال راه‌اندازی (قرارداد فعال ندارد)" : ""}
                  </span>
                </div>
                {channelError ? <p className="text-sm text-destructive">{channelError}</p> : null}
              </div>
            ) : null}
          </div>
        </CardContent>
      </Card>

      {activeContract ? (
        <Card>
          <CardHeader>
            <CardTitle>قرارداد فعال — نسخهٔ {faNumber(activeContract.version)}</CardTitle>
            <CardDescription>
              مؤثر از {formatIsoDate(activeContract.valid_from)}
              {activeContract.source_note ? ` · مرجع: ${activeContract.source_note}` : ""}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <dl className="grid gap-4 sm:grid-cols-3">
              <div>
                <dt className="text-sm text-muted-foreground">کارمزد</dt>
                <dd className="mt-1 font-medium tabular-nums">{formatPercentString(activeContract.fee_percent)}</dd>
              </div>
              <div>
                <dt className="text-sm text-muted-foreground">مالیات کارمزد</dt>
                <dd className="mt-1 font-medium tabular-nums">{formatPercentString(activeContract.fee_vat_rate)}</dd>
              </div>
              <div>
                <dt className="text-sm text-muted-foreground">مدل تسویه</dt>
                <dd className="mt-1 font-medium">{settlementModelLabel(activeContract.settlement_model)}</dd>
              </div>
              <div>
                <dt className="text-sm text-muted-foreground">زمان‌بندی کارمزد</dt>
                <dd className="mt-1 font-medium">{feeTimingLabel(activeContract.fee_timing)}</dd>
              </div>
              <div>
                <dt className="text-sm text-muted-foreground">شناسایی کارمزد</dt>
                <dd className="mt-1 font-medium">{feeAccrualLabel(activeContract.fee_accrual_policy)}</dd>
              </div>
              <div>
                <dt className="text-sm text-muted-foreground">ثبت خودکار</dt>
                <dd className="mt-1 font-medium">{autoPostLabel(activeContract.auto_post_policy)}</dd>
              </div>
              <div>
                <dt className="text-sm text-muted-foreground">مهلت پس از سررسید</dt>
                <dd className="mt-1 font-medium">{faNumber(activeContract.grace_days_after_due)} روز</dd>
              </div>
              <div>
                <dt className="text-sm text-muted-foreground">تلرانس تطبیق</dt>
                <dd className="mt-1 font-medium tabular-nums">{formatMoney(activeContract.match_tolerance_irr)}</dd>
              </div>
              <div>
                <dt className="text-sm text-muted-foreground">تلرانس tie-out</dt>
                <dd className="mt-1 font-medium tabular-nums">{formatMoney(activeContract.tieout_tolerance_irr)}</dd>
              </div>
            </dl>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardHeader>
            <CardTitle>قرارداد فعالی نیست</CardTitle>
            <CardDescription>
              این کانال هنوز قرارداد ندارد؛ بدون قرارداد فعال، پرداختی ثبت
              نمی‌شود. نخستین نسخه را از فرم زیر بسازید.
            </CardDescription>
          </CardHeader>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>تاریخچهٔ نسخه‌ها</CardTitle>
          <CardDescription>هر تغییر نرخ یک نسخهٔ جدید است؛ نسخه‌های قدیم دست‌نخورده می‌مانند.</CardDescription>
        </CardHeader>
        <CardContent>
          <DataTable
            columns={versionColumns}
            rows={contracts}
            rowKey={(row) => row.id}
            emptyText="برای این کانال قراردادی ثبت نشده است."
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>نسخهٔ جدید قرارداد</CardTitle>
          <CardDescription>
            درصد کارمزد را دستی از روی قرارداد کاغذی/PDF وارد کنید؛ هیچ نرخی
            از پیش پر نشده است.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          {!showForm ? (
            <Button onClick={() => setShowForm(true)}>ایجاد نسخهٔ جدید</Button>
          ) : (
            <>
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-1.5">
                  <Label htmlFor="contract-valid-from">تاریخ اثر (مؤثر از)</Label>
                  <JalaliDatePicker
                    id="contract-valid-from"
                    value={validFrom}
                    onChange={(iso) => setValidFrom(iso)}
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="contract-fee">درصد کارمزد (٪) — دستی</Label>
                  <Input
                    id="contract-fee"
                    value={feePercent}
                    onChange={(e) => setFeePercent(e.target.value)}
                    placeholder="مثلاً ۱۰"
                    inputMode="decimal"
                    dir="ltr"
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="contract-vat">نرخ مالیات کارمزد (٪)</Label>
                  <Input
                    id="contract-vat"
                    value={feeVatRate}
                    onChange={(e) => setFeeVatRate(e.target.value)}
                    placeholder="اختیاری — مثلاً ۱۰"
                    inputMode="decimal"
                    dir="ltr"
                  />
                </div>
                <div className="flex items-end gap-2 pb-2">
                  <Checkbox
                    id="contract-vat-claimable"
                    checked={feeVatClaimable}
                    onCheckedChange={(checked) => setFeeVatClaimable(checked === true)}
                  />
                  <Label htmlFor="contract-vat-claimable">مالیات کارمزد قابل مطالبه است</Label>
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="contract-fee-timing">زمان‌بندی کارمزد</Label>
                  <Select value={feeTiming} onValueChange={(v) => setFeeTiming(v ?? "proportional")}>
                    <SelectTrigger id="contract-fee-timing">
                      <SelectValue>{feeTimingLabel(feeTiming)}</SelectValue>
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="proportional">{feeTimingLabel("proportional")}</SelectItem>
                      <SelectItem value="upfront_first_tranche">{feeTimingLabel("upfront_first_tranche")}</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="contract-fee-accrual">شناسایی کارمزد</Label>
                  <Select value={feeAccrual} onValueChange={(v) => setFeeAccrual(v ?? "at_sale")}>
                    <SelectTrigger id="contract-fee-accrual">
                      <SelectValue>{feeAccrualLabel(feeAccrual)}</SelectValue>
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="at_sale">{feeAccrualLabel("at_sale")}</SelectItem>
                      <SelectItem value="at_settlement">{feeAccrualLabel("at_settlement")}</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="contract-model">مدل تسویه</Label>
                  <Select value={settlementModel} onValueChange={(v) => setSettlementModel(v ?? "tranches_4")}>
                    <SelectTrigger id="contract-model">
                      <SelectValue>
                        {SETTLEMENT_MODELS.find((m) => m.value === settlementModel)?.label ?? settlementModel}
                      </SelectValue>
                    </SelectTrigger>
                    <SelectContent>
                      {SETTLEMENT_MODELS.map((m) => (
                        <SelectItem key={m.value} value={m.value}>
                          {m.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                {settlementModel === "__custom__" ? (
                  <div className="space-y-1.5">
                    <Label htmlFor="contract-model-custom">نام مدل تسویه</Label>
                    <Input
                      id="contract-model-custom"
                      value={customModel}
                      onChange={(e) => setCustomModel(e.target.value)}
                      placeholder="مثلاً tranches_2"
                      dir="ltr"
                    />
                  </div>
                ) : null}
              </div>

              <div className="space-y-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h3 className="text-base font-semibold">قسط‌های تسویه (legs)</h3>
                  <div className="flex gap-2">
                    <Button variant="outline" onClick={fillFourEqualLegs} type="button">
                      ۴ قسط مساوی
                    </Button>
                    <Button variant="outline" onClick={() => setLegs((prev) => [...prev, { ...EMPTY_LEG }])} type="button">
                      افزودن قسط
                    </Button>
                  </div>
                </div>
                <p className="text-sm text-muted-foreground">
                  مجموع سهم قسط‌ها باید ۱۰۰٪ باشد
                  {legsSum(legs) !== null ? ` (اکنون ${faNumber(legsSum(legs) as number)}٪)` : ""}.
                  فاصلهٔ ماه از ماه فروش و روز واریز در ماه سررسید است.
                </p>
                {legs.map((leg, index) => (
                  <div key={index} className="grid gap-4 border-t pt-4 sm:grid-cols-4">
                    <div className="space-y-1.5">
                      <Label htmlFor={`leg-offset-${index}`}>فاصلهٔ ماه (قسط {faNumber(index + 1)})</Label>
                      <Input
                        id={`leg-offset-${index}`}
                        value={leg.month_offset}
                        onChange={(e) =>
                          setLegs((prev) => prev.map((l, i) => (i === index ? { ...l, month_offset: e.target.value } : l)))
                        }
                        inputMode="numeric"
                        dir="ltr"
                      />
                    </div>
                    <div className="space-y-1.5">
                      <Label htmlFor={`leg-day-${index}`}>روز واریز</Label>
                      <Input
                        id={`leg-day-${index}`}
                        value={leg.payout_day}
                        onChange={(e) =>
                          setLegs((prev) => prev.map((l, i) => (i === index ? { ...l, payout_day: e.target.value } : l)))
                        }
                        inputMode="numeric"
                        dir="ltr"
                      />
                    </div>
                    <div className="space-y-1.5">
                      <Label htmlFor={`leg-percent-${index}`}>سهم (٪)</Label>
                      <Input
                        id={`leg-percent-${index}`}
                        value={leg.percent}
                        onChange={(e) =>
                          setLegs((prev) => prev.map((l, i) => (i === index ? { ...l, percent: e.target.value } : l)))
                        }
                        inputMode="decimal"
                        dir="ltr"
                      />
                    </div>
                    <div className="flex items-end">
                      <Button
                        variant="ghost"
                        type="button"
                        disabled={legs.length <= 1}
                        onClick={() => setLegs((prev) => prev.filter((_, i) => i !== index))}
                      >
                        حذف
                      </Button>
                    </div>
                  </div>
                ))}
              </div>

              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-1.5">
                  <Label htmlFor="contract-window-start">آغاز پنجرهٔ واریز (روز ماه)</Label>
                  <Input
                    id="contract-window-start"
                    value={windowStart}
                    onChange={(e) => setWindowStart(e.target.value)}
                    placeholder="اختیاری — مثلاً ۱"
                    inputMode="numeric"
                    dir="ltr"
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="contract-window-end">پایان پنجرهٔ واریز (روز ماه)</Label>
                  <Input
                    id="contract-window-end"
                    value={windowEnd}
                    onChange={(e) => setWindowEnd(e.target.value)}
                    placeholder="اختیاری — مثلاً ۵"
                    inputMode="numeric"
                    dir="ltr"
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="contract-calendar">تقویم واریز</Label>
                  <Select value={payoutCalendar} onValueChange={(v) => setPayoutCalendar(v ?? "jalali")}>
                    <SelectTrigger id="contract-calendar">
                      <SelectValue>{payoutCalendar === "gregorian" ? "میلادی" : "جلالی"}</SelectValue>
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="jalali">جلالی</SelectItem>
                      <SelectItem value="gregorian">میلادی</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="flex items-end gap-2 pb-2">
                  <Checkbox
                    id="contract-working-day"
                    checked={workingDayAdjustment}
                    onCheckedChange={(checked) => setWorkingDayAdjustment(checked === true)}
                  />
                  <Label htmlFor="contract-working-day">سررسید تعطیل به روز کاری بعد منتقل شود</Label>
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="contract-day-lag">تأخیر روزانه (مدل‌های روزشمار)</Label>
                  <Input
                    id="contract-day-lag"
                    value={dayLag}
                    onChange={(e) => setDayLag(e.target.value)}
                    placeholder="اختیاری — روز کاری پس از تأیید"
                    inputMode="numeric"
                    dir="ltr"
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="contract-grace">مهلت پس از سررسید (روز)</Label>
                  <Input
                    id="contract-grace"
                    value={graceDays}
                    onChange={(e) => setGraceDays(e.target.value)}
                    inputMode="numeric"
                    dir="ltr"
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="contract-match-tolerance">تلرانس تطبیق مبلغ</Label>
                  <AmountInput
                    id="contract-match-tolerance"
                    value={matchToleranceToman}
                    onChange={setMatchToleranceToman}
                    unit="تومان"
                    min={0}
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="contract-tieout-tolerance">تلرانس tie-out</Label>
                  <AmountInput
                    id="contract-tieout-tolerance"
                    value={tieoutToleranceToman}
                    onChange={setTieoutToleranceToman}
                    unit="تومان"
                    min={0}
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="contract-auto-post">سیاست ثبت خودکار</Label>
                  <Select value={autoPostPolicy} onValueChange={(v) => setAutoPostPolicy(v ?? "draft_only")}>
                    <SelectTrigger id="contract-auto-post">
                      <SelectValue>{autoPostLabel(autoPostPolicy)}</SelectValue>
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="draft_only">{autoPostLabel("draft_only")}</SelectItem>
                      <SelectItem value="auto_post_within_policy">{autoPostLabel("auto_post_within_policy")}</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="contract-auto-post-cap">سقف ثبت خودکار</Label>
                  <AmountInput
                    id="contract-auto-post-cap"
                    value={autoPostCapToman}
                    onChange={setAutoPostCapToman}
                    unit="تومان"
                    min={0}
                  />
                </div>
                <div className="space-y-1.5 sm:col-span-2">
                  <Label htmlFor="contract-source-note">مرجع ارقام قرارداد</Label>
                  <Textarea
                    id="contract-source-note"
                    value={sourceNote}
                    onChange={(e) => setSourceNote(e.target.value)}
                    placeholder="مثلاً: قرارداد اسنپ‌پی، بند ۳، نسخهٔ امضاشدهٔ آبان ۱۴۰۵"
                  />
                </div>
              </div>

              {formError ? <p className="text-sm text-destructive">{formError}</p> : null}
              <div className="flex flex-wrap gap-4">
                <Button onClick={handleCreateVersion} disabled={formBusy}>
                  {formBusy ? "در حال ثبت…" : "ایجاد نسخهٔ جدید"}
                </Button>
                <Button variant="outline" onClick={() => setShowForm(false)} disabled={formBusy}>
                  انصراف
                </Button>
              </div>
            </>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>بازمشتق برنامه‌های باز</CardTitle>
          <CardDescription>
            برنامه‌های باز این کانال را از روی نسخهٔ فعال قرارداد دوباره
            بسازید. این کار فقط با اقدام صریح انجام می‌شود و در حسابرسی ثبت
            می‌شود؛ قسط‌های تأیید/دریافت‌شده دست نمی‌خورند.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="rederive-actor">اقدام‌کننده</Label>
              <Input
                id="rederive-actor"
                value={rederiveActor}
                onChange={(e) => setRederiveActor(e.target.value)}
                placeholder="نام اقدام‌کننده"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="rederive-reason">دلیل</Label>
              <Input
                id="rederive-reason"
                value={rederiveReason}
                onChange={(e) => setRederiveReason(e.target.value)}
                placeholder="مثلاً: تغییر نرخ قرارداد"
              />
            </div>
          </div>
          {rederiveError ? <p className="text-sm text-destructive">{rederiveError}</p> : null}
          {rederiveResult ? (
            <p className="text-sm text-muted-foreground">
              {faNumber(rederiveResult.rederived_payment_ids.length)} پرداخت بازمشتق شد؛{" "}
              {faNumber(rederiveResult.skipped.length)} پرداخت رد شد.
            </p>
          ) : null}
          <Button onClick={handleRederive} disabled={rederiveBusy || activeContract === null} variant="outline">
            {rederiveBusy ? "در حال اجرا…" : "بازمشتق برنامه‌های باز"}
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}
