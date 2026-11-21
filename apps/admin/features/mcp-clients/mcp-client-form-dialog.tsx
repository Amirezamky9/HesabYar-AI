/**
 * The MCP client create dialog (US-606).
 *
 * Name + description + a scope checklist (mcp:connect is mandatory
 * and locked on) + an optional single-company grant + an optional
 * expiry. The backend issues an ordinary API key for the client and
 * returns the secret exactly once — the parent shows it in the
 * secret dialog and never stores it.
 *
 * The parent remounts this dialog per open (key={open ? "open" : "closed"}),
 * so the form starts empty on every open.
 */

"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { ApiError, api, type McpClientWithSecretResponse } from "@/lib/api-client";
import {
  Button,
  Checkbox,
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
  Textarea,
} from "@/components/ui";
import { persianApiErrorMessage } from "@/features/shared/api-error";
import { useCompany } from "@/features/shared/use-company";
import {
  MCP_CLIENT_SCOPES,
  MCP_CONNECT_SCOPE,
  MCP_EXPIRY_OPTIONS,
  emptyMcpClientForm,
  mcpClientFormToCreate,
  validateMcpClientForm,
  type McpClientFormErrors,
  type McpClientFormValues,
} from "./mcp-clients-data";

interface McpClientFormDialogProps {
  open: boolean;
  onClose: () => void;
  /** Called with the created client + its one-time secret. */
  onCreated: (created: McpClientWithSecretResponse) => void;
}

export function McpClientFormDialog({ open, onClose, onCreated }: McpClientFormDialogProps) {
  const router = useRouter();
  const { companies, loading: companiesLoading } = useCompany();
  const [values, setValues] = React.useState<McpClientFormValues>(emptyMcpClientForm);
  const [errors, setErrors] = React.useState<McpClientFormErrors>({});
  const [saving, setSaving] = React.useState(false);
  const [submitError, setSubmitError] = React.useState<string | null>(null);

  const toggleScope = (scope: string, checked: boolean) => {
    setValues((prev) => ({
      ...prev,
      scopes: checked
        ? [...prev.scopes, scope]
        : prev.scopes.filter((s) => s !== scope),
    }));
  };

  const handleSave = async () => {
    const validation = validateMcpClientForm(values);
    setErrors(validation);
    if (Object.keys(validation).length > 0) return;
    setSaving(true);
    setSubmitError(null);
    try {
      const created = await api.createMcpClient(mcpClientFormToCreate(values));
      onCreated(created);
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setSubmitError(persianApiErrorMessage(err, "ایجاد کلاینت ناموفق بود."));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-h-[90vh] overflow-y-auto" data-testid="mcp-client-form-dialog">
        <DialogHeader>
          <DialogTitle>کلاینت MCP جدید</DialogTitle>
          <DialogDescription>
            یک کلید دسترسی برای میزبان هوش مصنوعی (مثل Claude Desktop) صادر می‌شود. کلید فقط
            یک بار نمایش داده می‌شود؛ آن را در جای امنی نگه دارید.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor="mcp-client-name">نام کلاینت</Label>
            <Input
              id="mcp-client-name"
              data-testid="mcp-client-name-input"
              value={values.name}
              onChange={(e) => setValues((prev) => ({ ...prev, name: e.target.value }))}
              placeholder="مثلاً Claude Desktop"
              autoComplete="off"
            />
            {errors.name && <p className="text-sm text-destructive">{errors.name}</p>}
          </div>

          <div className="flex flex-col gap-2">
            <Label htmlFor="mcp-client-description">توضیح (اختیاری)</Label>
            <Textarea
              id="mcp-client-description"
              data-testid="mcp-client-description-input"
              value={values.description}
              onChange={(e) => setValues((prev) => ({ ...prev, description: e.target.value }))}
              placeholder="این کلاینت برای چیست؟"
            />
          </div>

          <div className="flex flex-col gap-2">
            <Label>دسترسی‌ها</Label>
            <p className="text-sm text-muted-foreground">
              اتصال MCP همیشه فعال است؛ بقیهٔ دسترسی‌ها فقط خواندنی‌اند — کلاینت MCP هرگز
              نمی‌تواند چیزی ثبت یا ارسال کند.
            </p>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {MCP_CLIENT_SCOPES.map(({ scope, label }) => {
                const locked = scope === MCP_CONNECT_SCOPE;
                const checked = locked || values.scopes.includes(scope);
                return (
                  <label
                    key={scope}
                    className="flex min-h-11 cursor-pointer items-center gap-2 rounded-md border border-border px-3 py-2"
                  >
                    <Checkbox
                      checked={checked}
                      disabled={locked}
                      onCheckedChange={(next) => toggleScope(scope, next === true)}
                      aria-label={label}
                    />
                    <span className="text-sm">
                      {label}
                      <span className="ms-2 text-muted-foreground" dir="ltr">
                        {scope}
                      </span>
                    </span>
                  </label>
                );
              })}
            </div>
            {errors.scopes && <p className="text-sm text-destructive">{errors.scopes}</p>}
          </div>

          <div className="flex flex-col gap-2">
            <Label htmlFor="mcp-client-company">دسترسی به شرکت</Label>
            <Select
              value={values.companyId}
              onValueChange={(next) =>
                next && setValues((prev) => ({ ...prev, companyId: next }))
              }
              disabled={companiesLoading}
            >
              <SelectTrigger id="mcp-client-company" data-testid="mcp-client-company-select">
                <SelectValue placeholder="همهٔ شرکت‌ها" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">همهٔ شرکت‌ها</SelectItem>
                {companies.map((company) => (
                  <SelectItem key={company.id} value={company.id}>
                    {company.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-sm text-muted-foreground">
              اگر یک شرکت انتخاب شود، کلاینت فقط داده‌های همان شرکت را می‌بیند.
            </p>
          </div>

          <div className="flex flex-col gap-2">
            <Label htmlFor="mcp-client-expiry">انقضای کلید</Label>
            <Select
              value={String(values.expiryDays)}
              onValueChange={(next) =>
                next && setValues((prev) => ({ ...prev, expiryDays: Number(next) }))
              }
            >
              <SelectTrigger id="mcp-client-expiry" data-testid="mcp-client-expiry-select">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {MCP_EXPIRY_OPTIONS.map(({ days, label }) => (
                  <SelectItem key={days} value={String(days)}>
                    {label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {submitError && (
            <p className="text-sm text-destructive" role="alert">
              {submitError}
            </p>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={saving}>
            انصراف
          </Button>
          <Button
            onClick={handleSave}
            disabled={saving}
            data-testid="mcp-client-submit-button"
          >
            {saving ? "در حال صدور…" : "صدور کلید"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
