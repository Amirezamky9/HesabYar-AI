/**
 * SLA policy settings (US-504) — per-type ageing/SLA configuration.
 *
 * Lists the effective policy for all nine types (configured row over
 * the skeleton default) and lets an admin change the deadline and
 * warn window per type. Updates need the `settings:manage` scope;
 * a key without it gets a Persian 403 explanation instead of a
 * silent failure. Invalid windows (0 < warn < sla) are rejected by
 * the backend with a typed error.
 */

"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { ApiError, api, type SlaPolicyResponse } from "@/lib/api-client";
import { faNumber } from "@/lib/utils";
import { Button, Card, CardContent, CardHeader, CardTitle, Input, Label, Skeleton } from "@/components/ui";
import { persianApiErrorMessage } from "@/features/shared/api-error";
import { REVIEW_ITEM_TYPES, reviewTypeLabel } from "./review-queue-data";

export function SlaPolicySettings() {
  const router = useRouter();
  const [policies, setPolicies] = React.useState<SlaPolicyResponse[] | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [loadError, setLoadError] = React.useState<string | null>(null);
  const [drafts, setDrafts] = React.useState<Record<string, { sla: string; warn: string }>>({});
  const [saving, setSaving] = React.useState<string | null>(null);
  const [saveError, setSaveError] = React.useState<string | null>(null);
  const [savedType, setSavedType] = React.useState<string | null>(null);

  React.useEffect(() => {
    let cancelled = false;
    api
      .listSlaPolicies()
      .then((data) => {
        if (cancelled) return;
        setPolicies(data);
        setDrafts(
          Object.fromEntries(
            data.map((policy) => [
              policy.item_type,
              { sla: String(policy.sla_hours), warn: String(policy.warn_hours) },
            ]),
          ),
        );
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.replace("/login");
          return;
        }
        setLoadError(persianApiErrorMessage(err, "دریافت سیاست‌های مهلت ناموفق بود."));
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

  const save = async (itemType: string) => {
    const draft = drafts[itemType];
    if (!draft) return;
    const slaHours = Number(draft.sla);
    const warnHours = Number(draft.warn);
    if (!Number.isInteger(slaHours) || !Number.isInteger(warnHours)) {
      setSaveError("مهلت و هشدار باید عدد صحیح باشند.");
      return;
    }
    setSaving(itemType);
    setSaveError(null);
    setSavedType(null);
    try {
      const updated = await api.updateSlaPolicy(itemType, { sla_hours: slaHours, warn_hours: warnHours });
      setPolicies((prev) =>
        prev === null ? prev : prev.map((policy) => (policy.item_type === itemType ? updated : policy)),
      );
      setSavedType(itemType);
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setSaveError(persianApiErrorMessage(err, "ذخیرهٔ سیاست مهلت ناموفق بود."));
    } finally {
      setSaving(null);
    }
  };

  return (
    <Card data-testid="sla-policy-settings">
      <CardHeader>
        <CardTitle>تنظیمات مهلت (SLA)</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted-foreground">
          مهلت هر نوع آیتم از زمان ثبت آن محاسبه می‌شود؛ «هشدار» یعنی چند ساعت پیش از مهلت، آیتم به‌عنوان
          «نزدیک مهلت» نشان داده شود. تغییر سیاست، صف را فوراً دوباره سنجه می‌کند.
        </p>
        {loading ? (
          <div className="space-y-2" aria-label="در حال بارگذاری">
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-12 w-full" />
            ))}
          </div>
        ) : loadError ? (
          <p className="text-sm text-destructive" role="alert">
            {loadError}
          </p>
        ) : policies === null || policies.length === 0 ? (
          <p className="text-sm text-muted-foreground">سیاستی پیدا نشد.</p>
        ) : (
          <ul className="space-y-4">
            {REVIEW_ITEM_TYPES.map((itemType) => {
              const policy = policies.find((p) => p.item_type === itemType);
              if (!policy) return null;
              const draft = drafts[itemType] ?? { sla: String(policy.sla_hours), warn: String(policy.warn_hours) };
              const changed =
                Number(draft.sla) !== policy.sla_hours || Number(draft.warn) !== policy.warn_hours;
              return (
                <li key={itemType} className="space-y-2 rounded-lg border p-4">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <p className="font-medium">{reviewTypeLabel(itemType)}</p>
                    <p className="text-sm text-muted-foreground">
                      فعلی: مهلت {faNumber(policy.sla_hours)} ساعت · هشدار {faNumber(policy.warn_hours)} ساعت
                      {policy.updated_at === null ? " (پیش‌فرض)" : ""}
                    </p>
                  </div>
                  <div className="grid gap-4 sm:grid-cols-3">
                    <div className="space-y-2">
                      <Label htmlFor={`sla-${itemType}`}>مهلت (ساعت)</Label>
                      <Input
                        id={`sla-${itemType}`}
                        inputMode="numeric"
                        value={draft.sla}
                        onChange={(e) =>
                          setDrafts((prev) => ({ ...prev, [itemType]: { ...draft, sla: e.target.value } }))
                        }
                      />
                    </div>
                    <div className="space-y-2">
                      <Label htmlFor={`warn-${itemType}`}>هشدار (ساعت)</Label>
                      <Input
                        id={`warn-${itemType}`}
                        inputMode="numeric"
                        value={draft.warn}
                        onChange={(e) =>
                          setDrafts((prev) => ({ ...prev, [itemType]: { ...draft, warn: e.target.value } }))
                        }
                      />
                    </div>
                    <div className="flex items-end">
                      <Button onClick={() => save(itemType)} disabled={!changed || saving === itemType}>
                        {saving === itemType ? "در حال ذخیره…" : "ذخیره"}
                      </Button>
                    </div>
                  </div>
                  {savedType === itemType ? (
                    <p className="text-sm text-success" role="status">
                      ذخیره شد.
                    </p>
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}
        {saveError ? (
          <p className="text-sm text-destructive" role="alert">
            {saveError}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}
