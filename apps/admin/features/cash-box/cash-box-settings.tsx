/**
 * Cash-box settings (US-516) — the installation's variance policy
 * and counting thresholds.
 *
 * Policy (a) «حساب کسر و اضافات صندوق» is the default; policy (ب)
 * books a shortage as a temporary receivable from the cashier. The
 * material-variance threshold decides when a review item is
 * critical; the reminder threshold drives the «شمارش نشده» alert.
 * Updates need the `cashbox:manage` scope.
 */

"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { ApiError, api } from "@/lib/api-client";
import { normalizeDigits } from "@/lib/format-money";
import {
  AmountInput,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Checkbox,
  Input,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Skeleton,
} from "@/components/ui";
import { persianApiErrorMessage } from "@/features/shared/api-error";
import {
  COUNT_SCHEDULES,
  VARIANCE_POLICIES,
  countScheduleLabel,
  variancePolicyDescription,
  variancePolicyLabel,
} from "./cash-box-data";

function parsePositiveInt(raw: string): number | null {
  const value = Number(normalizeDigits(raw.trim()));
  return Number.isInteger(value) && value > 0 ? value : null;
}

export function CashBoxSettings() {
  const router = useRouter();
  const [loading, setLoading] = React.useState(true);
  const [loadError, setLoadError] = React.useState<string | null>(null);
  const [saving, setSaving] = React.useState(false);
  const [saveError, setSaveError] = React.useState<string | null>(null);
  const [saved, setSaved] = React.useState(false);

  const [policy, setPolicy] = React.useState<string>("expense");
  const [thresholdToman, setThresholdToman] = React.useState<number | null>(null);
  const [reminderDays, setReminderDays] = React.useState("7");
  const [schedule, setSchedule] = React.useState<string>("daily");
  const [supervisorRequired, setSupervisorRequired] = React.useState(true);
  const [settlementHours, setSettlementHours] = React.useState("24");

  React.useEffect(() => {
    let cancelled = false;
    api
      .getCashBoxSettings()
      .then((data) => {
        if (cancelled) return;
        setPolicy(data.variance_policy);
        setThresholdToman(
          data.material_variance_threshold_irr === null ? null : data.material_variance_threshold_irr / 10,
        );
        setReminderDays(String(data.count_reminder_days));
        setSchedule(data.count_schedule);
        setSupervisorRequired(data.supervisor_required);
        setSettlementHours(String(data.shortage_settlement_hours));
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.replace("/login");
          return;
        }
        setLoadError(persianApiErrorMessage(err, "دریافت تنظیمات صندوق ناموفق بود."));
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

  const save = async () => {
    const days = parsePositiveInt(reminderDays);
    const hours = parsePositiveInt(settlementHours);
    if (days === null || hours === null) {
      setSaveError("آستانهٔ یادآوری و مهلت تسویه باید عدد صحیح بزرگ‌تر از صفر باشند.");
      return;
    }
    setSaving(true);
    setSaveError(null);
    setSaved(false);
    try {
      await api.updateCashBoxSettings({
        variance_policy: policy,
        material_variance_threshold_irr: thresholdToman === null ? null : thresholdToman * 10,
        count_reminder_days: days,
        count_schedule: schedule,
        supervisor_required: supervisorRequired,
        shortage_settlement_hours: hours,
      });
      setSaved(true);
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setSaveError(persianApiErrorMessage(err, "ذخیرهٔ تنظیمات صندوق ناموفق بود."));
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <Card data-testid="cash-box-settings">
        <CardHeader>
          <CardTitle>تنظیمات صندوق</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4" aria-label="در حال بارگذاری">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-1/2" />
        </CardContent>
      </Card>
    );
  }

  if (loadError) {
    return (
      <Card data-testid="cash-box-settings">
        <CardHeader>
          <CardTitle>تنظیمات صندوق</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-destructive" role="alert">
            {loadError}
          </p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card data-testid="cash-box-settings">
      <CardHeader>
        <CardTitle>تنظیمات صندوق</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div>
          <Label htmlFor="variance-policy">سیاست سند کسری / اضافه</Label>
          <Select value={policy} onValueChange={(v) => v && setPolicy(v)}>
            <SelectTrigger id="variance-policy">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {VARIANCE_POLICIES.map((value) => (
                <SelectItem key={value} value={value}>
                  {variancePolicyLabel(value)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <p className="mt-1 text-sm text-muted-foreground">{variancePolicyDescription(policy)}</p>
        </div>
        <div>
          <Label htmlFor="material-threshold">آستانهٔ مغایرت بااهمیت (اختیاری)</Label>
          <AmountInput
            id="material-threshold"
            value={thresholdToman}
            onChange={setThresholdToman}
            min={0}
            placeholder="بدون آستانه"
          />
          <p className="mt-1 text-sm text-muted-foreground">
            مغایرتی به این مبلغ یا بیشتر، آیتم بازبینی را «بحرانی» می‌کند؛ خالی یعنی همهٔ مغایرت‌ها یکسان
            دیده می‌شوند.
          </p>
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <Label htmlFor="reminder-days">یادآوری صندوق شمارش‌نشده (روز)</Label>
            <Input
              id="reminder-days"
              inputMode="numeric"
              value={reminderDays}
              onChange={(event) => setReminderDays(event.target.value)}
            />
          </div>
          <div>
            <Label htmlFor="count-schedule">برنامهٔ شمارش</Label>
            <Select value={schedule} onValueChange={(v) => v && setSchedule(v)}>
              <SelectTrigger id="count-schedule">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {COUNT_SCHEDULES.map((value) => (
                  <SelectItem key={value} value={value}>
                    {countScheduleLabel(value)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>
        <div>
          <Label htmlFor="settlement-hours">مهلت تسویهٔ کسری در سیاست (ب) (ساعت)</Label>
          <Input
            id="settlement-hours"
            inputMode="numeric"
            value={settlementHours}
            onChange={(event) => setSettlementHours(event.target.value)}
          />
        </div>
        <div className="flex items-center gap-2">
          <Checkbox
            id="supervisor-required"
            checked={supervisorRequired}
            onCheckedChange={(checked) => setSupervisorRequired(checked === true)}
          />
          <Label htmlFor="supervisor-required">
            ناظر باید با صندوقدار متفاوت باشد (کنترل داخلی)
          </Label>
        </div>
        {saveError ? (
          <p className="text-sm text-destructive" role="alert">
            {saveError}
          </p>
        ) : null}
        {saved ? (
          <p className="text-sm text-success" role="status">
            تنظیمات ذخیره شد.
          </p>
        ) : null}
        <Button type="button" onClick={save} disabled={saving}>
          ذخیرهٔ تنظیمات
        </Button>
      </CardContent>
    </Card>
  );
}
