/**
 * The token-record create/edit dialog (US-704).
 *
 * A configured token record tells HesabYar a token's expiry so the
 * token-expiry rule can alert before it — the Fintech OAuth token
 * is the first (no live token exists yet; the record IS the source
 * of truth until the ۵ت remainder lands one). The expiry is picked
 * on the Jalali calendar; the token stays valid through that day.
 *
 * The parent remounts this dialog per open (key={...}), so the form
 * starts from the existing record (edit) or empty (create) on
 * every open.
 */

"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import {
  ApiError,
  api,
  type MonitoringTokenRecordResponse,
} from "@/lib/api-client";
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Input,
  JalaliDatePicker,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Switch,
  Textarea,
} from "@/components/ui";
import { persianApiErrorMessage } from "@/features/shared/api-error";
import {
  TOKEN_KIND_LABELS,
  emptyTokenForm,
  tokenFormFromExisting,
  tokenFormToRequest,
  validateTokenForm,
  type TokenFormErrors,
  type TokenFormValues,
  type TokenKind,
} from "./monitoring-data";

interface MonitoringTokenDialogProps {
  open: boolean;
  onClose: () => void;
  /** The record being edited, or null to create one. */
  record: MonitoringTokenRecordResponse | null;
  onSaved: () => void;
}

export function MonitoringTokenDialog({
  open,
  onClose,
  record,
  onSaved,
}: MonitoringTokenDialogProps) {
  const router = useRouter();
  const [values, setValues] = React.useState<TokenFormValues>(() =>
    record ? tokenFormFromExisting(record) : emptyTokenForm(),
  );
  const [errors, setErrors] = React.useState<TokenFormErrors>({});
  const [saving, setSaving] = React.useState(false);
  const [submitError, setSubmitError] = React.useState<string | null>(null);

  const set = <K extends keyof TokenFormValues>(key: K, value: TokenFormValues[K]) => {
    setValues((prev) => ({ ...prev, [key]: value }));
  };

  const handleSave = async () => {
    const validation = validateTokenForm(values);
    setErrors(validation);
    if (Object.keys(validation).length > 0) return;
    setSaving(true);
    setSubmitError(null);
    try {
      const body = tokenFormToRequest(values);
      if (record) await api.updateMonitoringToken(record.id, body);
      else await api.createMonitoringToken(body);
      onSaved();
      onClose();
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setSubmitError(persianApiErrorMessage(err, "ذخیرهٔ توکن ناموفق بود."));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{record ? "ویرایش توکن" : "توکن جدید"}</DialogTitle>
          <DialogDescription>
            تاریخ انقضای توکن را ثبت کنید تا پیش از انقضا هشدار بیاید؛ پس از
            چرخش توکن، همین رکورد را با تاریخ جدید به‌روز کنید.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="monitoring-token-kind">نوع توکن</Label>
            <Select
              value={values.kind}
              onValueChange={(value) => set("kind", value as TokenKind)}
            >
              <SelectTrigger id="monitoring-token-kind">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(
                  Object.keys(TOKEN_KIND_LABELS).filter(
                    (kind) => kind !== "api_key",
                  ) as TokenKind[]
                ).map((kind) => (
                    <SelectItem key={kind} value={kind}>
                      {TOKEN_KIND_LABELS[kind]}
                    </SelectItem>
                  ))}
              </SelectContent>
            </Select>
            <p className="text-sm text-muted-foreground">
              کلیدهای API حسابیار از جدول کلیدها خوانده می‌شوند و نیازی به ثبت
              دستی ندارند.
            </p>
          </div>
          <div className="space-y-2">
            <Label htmlFor="monitoring-token-label">برچسب توکن</Label>
            <Input
              id="monitoring-token-label"
              value={values.label}
              onChange={(e) => set("label", e.target.value)}
              placeholder="توکن فینوتک"
            />
            {errors.label ? (
              <p className="text-sm text-destructive">{errors.label}</p>
            ) : null}
          </div>
          <div className="space-y-2">
            <Label htmlFor="monitoring-token-expires">تاریخ انقضا</Label>
            <JalaliDatePicker
              id="monitoring-token-expires"
              value={values.expiresDate || null}
              onChange={(iso) => set("expiresDate", iso ?? "")}
            />
            {errors.expiresDate ? (
              <p className="text-sm text-destructive">{errors.expiresDate}</p>
            ) : null}
          </div>
          <div className="space-y-2">
            <Label htmlFor="monitoring-token-runbook">مرجع راهنمای چرخش (اختیاری)</Label>
            <Input
              id="monitoring-token-runbook"
              dir="ltr"
              value={values.rotationRunbookRef}
              onChange={(e) => set("rotationRunbookRef", e.target.value)}
              placeholder="docs/monitoring.md — بخش «چرخش توکن فینوتک»"
              className="text-start"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="monitoring-token-notes">یادداشت (اختیاری)</Label>
            <Textarea
              id="monitoring-token-notes"
              value={values.notes}
              onChange={(e) => set("notes", e.target.value)}
              placeholder="مثلاً: توکن محیط تولید"
            />
            {errors.notes ? (
              <p className="text-sm text-destructive">{errors.notes}</p>
            ) : null}
          </div>
          <div className="flex items-center gap-2">
            <Switch
              id="monitoring-token-enabled"
              checked={values.enabled}
              onCheckedChange={(checked) => set("enabled", checked)}
            />
            <Label htmlFor="monitoring-token-enabled">فعال</Label>
          </div>
          {submitError ? (
            <p role="alert" className="text-sm text-destructive">
              {submitError}
            </p>
          ) : null}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={saving}>
            انصراف
          </Button>
          <Button onClick={handleSave} disabled={saving}>
            {saving ? "در حال ذخیره…" : "ذخیره"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
