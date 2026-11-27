/**
 * The alert-channel create/edit dialog (US-704).
 *
 * Kind (SMS / email / webhook) + name + target + minimum severity +
 * enabled flag. For SMS the provider comes from the installation's
 * SMS registry (`listSmsProviders` — no provider hard-coded here
 * either); the installation defaults to the caller's server-side.
 *
 * The parent remounts this dialog per open (key={...}), so the form
 * starts from the existing channel (edit) or empty (create) on
 * every open.
 */

"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import {
  ApiError,
  api,
  type MonitoringChannelResponse,
  type SmsProviderResponse,
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
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Switch,
} from "@/components/ui";
import { persianApiErrorMessage } from "@/features/shared/api-error";
import {
  ALERT_SEVERITY_LABELS,
  CHANNEL_KIND_LABELS,
  CHANNEL_TARGET_LABELS,
  CHANNEL_TARGET_PLACEHOLDERS,
  channelFormFromExisting,
  channelFormToRequest,
  emptyChannelForm,
  validateChannelForm,
  type ChannelFormErrors,
  type ChannelFormValues,
  type ChannelKind,
} from "./monitoring-data";

interface MonitoringChannelDialogProps {
  open: boolean;
  onClose: () => void;
  /** The channel being edited, or null to create one. */
  channel: MonitoringChannelResponse | null;
  onSaved: () => void;
}

export function MonitoringChannelDialog({
  open,
  onClose,
  channel,
  onSaved,
}: MonitoringChannelDialogProps) {
  const router = useRouter();
  const [values, setValues] = React.useState<ChannelFormValues>(() =>
    channel ? channelFormFromExisting(channel) : emptyChannelForm(),
  );
  const [errors, setErrors] = React.useState<ChannelFormErrors>({});
  const [saving, setSaving] = React.useState(false);
  const [submitError, setSubmitError] = React.useState<string | null>(null);
  const [providers, setProviders] = React.useState<SmsProviderResponse[] | null>(null);

  // The SMS provider list loads only when the dialog is an SMS
  // channel (or could become one): the registry is the source of
  // truth for provider keys.
  React.useEffect(() => {
    if (!open) return;
    let cancelled = false;
    api
      .listSmsProviders()
      .then((data) => {
        if (!cancelled) setProviders(data);
      })
      .catch(() => {
        if (!cancelled) setProviders([]);
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  const enabledProviders = (providers ?? []).filter((p) => p.enabled);
  const targetLabel = CHANNEL_TARGET_LABELS[values.kind];
  const targetPlaceholder = CHANNEL_TARGET_PLACEHOLDERS[values.kind];

  const set = <K extends keyof ChannelFormValues>(key: K, value: ChannelFormValues[K]) => {
    setValues((prev) => ({ ...prev, [key]: value }));
  };

  const handleSave = async () => {
    const validation = validateChannelForm(values);
    setErrors(validation);
    if (Object.keys(validation).length > 0) return;
    setSaving(true);
    setSubmitError(null);
    try {
      const body = channelFormToRequest(values);
      if (channel) await api.updateMonitoringChannel(channel.id, body);
      else await api.createMonitoringChannel(body);
      onSaved();
      onClose();
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setSubmitError(persianApiErrorMessage(err, "ذخیرهٔ کانال ناموفق بود."));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{channel ? "ویرایش کانال هشدار" : "کانال هشدار جدید"}</DialogTitle>
          <DialogDescription>
            هشدارهای جدید به این کانال ارسال می‌شوند؛ پیامک از طریق رجیستری پیامک
            نصب، ایمیل پس از اتصال ارائه‌دهندهٔ ایمیل، و وب‌هوک با POST مستقیم.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="monitoring-channel-kind">نوع کانال</Label>
            <Select
              value={values.kind}
              onValueChange={(value) => set("kind", value as ChannelKind)}
            >
              <SelectTrigger id="monitoring-channel-kind">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(Object.keys(CHANNEL_KIND_LABELS) as ChannelKind[]).map((kind) => (
                  <SelectItem key={kind} value={kind}>
                    {CHANNEL_KIND_LABELS[kind]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-2">
            <Label htmlFor="monitoring-channel-name">نام کانال</Label>
            <Input
              id="monitoring-channel-name"
              value={values.name}
              onChange={(e) => set("name", e.target.value)}
              placeholder="وب‌هوک عملیات"
            />
            {errors.name ? <p className="text-sm text-destructive">{errors.name}</p> : null}
          </div>
          <div className="space-y-2">
            <Label htmlFor="monitoring-channel-target">{targetLabel}</Label>
            <Input
              id="monitoring-channel-target"
              dir="ltr"
              value={values.target}
              onChange={(e) => set("target", e.target.value)}
              placeholder={targetPlaceholder}
              className="text-start"
            />
            {errors.target ? (
              <p className="text-sm text-destructive">{errors.target}</p>
            ) : null}
          </div>
          {values.kind === "sms" ? (
            <div className="space-y-2">
              <Label htmlFor="monitoring-channel-provider">ارائه‌دهندهٔ پیامک</Label>
              {providers === null ? (
                <Select disabled value="">
                  <SelectTrigger id="monitoring-channel-provider">
                    <SelectValue placeholder="در حال بارگذاری…" />
                  </SelectTrigger>
                </Select>
              ) : enabledProviders.length > 0 ? (
                <Select
                  value={values.providerKey}
                  onValueChange={(value) => set("providerKey", value ?? "")}
                >
                  <SelectTrigger id="monitoring-channel-provider">
                    <SelectValue placeholder="یک ارائه‌دهنده انتخاب کنید" />
                  </SelectTrigger>
                  <SelectContent>
                    {enabledProviders.map((provider) => (
                      <SelectItem key={provider.provider_key} value={provider.provider_key}>
                        {provider.display_name_fa}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              ) : (
                <>
                  <Input
                    id="monitoring-channel-provider"
                    dir="ltr"
                    value={values.providerKey}
                    onChange={(e) => set("providerKey", e.target.value)}
                    placeholder="kavenegar"
                    className="text-start"
                  />
                  <p className="text-sm text-muted-foreground">
                    ارائه‌دهندهٔ فعالی در رجیستری پیامک پیدا نشد؛ کلید ارائه‌دهنده را
                    دستی وارد کنید.
                  </p>
                </>
              )}
              {errors.providerKey ? (
                <p className="text-sm text-destructive">{errors.providerKey}</p>
              ) : null}
            </div>
          ) : null}
          <div className="space-y-2">
            <Label htmlFor="monitoring-channel-severity">کمترین سطح هشدار</Label>
            <Select
              value={values.minSeverity}
              onValueChange={(value) =>
                set("minSeverity", value as ChannelFormValues["minSeverity"])
              }
            >
              <SelectTrigger id="monitoring-channel-severity">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(["info", "warning", "critical"] as const).map((severity) => (
                  <SelectItem key={severity} value={severity}>
                    {ALERT_SEVERITY_LABELS[severity]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-sm text-muted-foreground">
              فقط هشدارهایی با این سطح یا بالاتر به این کانال می‌روند.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Switch
              id="monitoring-channel-enabled"
              checked={values.enabled}
              onCheckedChange={(checked) => set("enabled", checked)}
            />
            <Label htmlFor="monitoring-channel-enabled">فعال</Label>
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
