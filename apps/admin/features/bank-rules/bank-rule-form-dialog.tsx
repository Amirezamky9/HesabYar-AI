/**
 * The bank-rule create/edit dialog (US-565, W3).
 *
 * «وقتی <شرح / طرف حساب / مبلغ / جهت> ← <حساب>»: every condition is
 * ANDed, and at least one condition is required — a condition-less
 * rule would match every line. Amounts are typed in تومان and
 * converted to whole Rial at the boundary. A new rule is born in
 * SUGGEST mode; promotion to AUTO happens from the list, after three
 * confirmed suggestions, never from this form.
 *
 * Editing the pattern or the target resets the rule's confirmations
 * (the trust was earned for the old definition) — the dialog warns
 * about that before saving when it applies.
 *
 * The parent remounts this dialog per rule (key={rule?.id ?? "new"}),
 * so the initial values are read once from initialRule.
 */

"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { TriangleAlert } from "lucide-react";
import {
  ApiError,
  api,
  type AccountResponse,
  type BankRuleResponse,
} from "@/lib/api-client";
import { fa } from "@/lib/utils";
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
  BANK_RULE_DIRECTION_OPTIONS,
  bankRuleFormToCreate,
  bankRuleFormToUpdate,
  bankRuleToForm,
  bankRuleUpdateResetsTrust,
  emptyBankRuleForm,
  validateBankRuleForm,
  type BankRuleDirection,
  type BankRuleFormErrors,
  type BankRuleFormValues,
} from "./bank-rules-data";

interface BankRuleFormDialogProps {
  open: boolean;
  onClose: () => void;
  /** Null = create a new rule. */
  initialRule: BankRuleResponse | null;
  companyId: string;
  /** Postable, open accounts of the selected company (for the target picker). */
  accounts: AccountResponse[];
  accountsLoading: boolean;
  /** Called after a successful save so the parent refreshes + closes. */
  onSaved: () => void;
}

export function BankRuleFormDialog({
  open,
  onClose,
  initialRule,
  companyId,
  accounts,
  accountsLoading,
  onSaved,
}: BankRuleFormDialogProps) {
  const router = useRouter();
  const [values, setValues] = React.useState<BankRuleFormValues>(() =>
    initialRule ? bankRuleToForm(initialRule) : emptyBankRuleForm(),
  );
  const [errors, setErrors] = React.useState<BankRuleFormErrors>({});
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const setValue = <K extends keyof BankRuleFormValues>(key: K, value: BankRuleFormValues[K]) => {
    setValues((prev) => ({ ...prev, [key]: value }));
    setErrors((prev) => ({ ...prev, [key]: undefined, form: undefined }));
  };

  const resetsTrust =
    initialRule !== null &&
    (initialRule.confirmations > 0 || initialRule.mode === "auto") &&
    bankRuleUpdateResetsTrust(bankRuleFormToUpdate(values, initialRule));

  const save = async () => {
    const validation = validateBankRuleForm(values);
    setErrors(validation);
    if (Object.keys(validation).length > 0) return;
    setSaving(true);
    setError(null);
    try {
      if (initialRule) {
        const update = bankRuleFormToUpdate(values, initialRule);
        if (Object.keys(update).length > 0) {
          await api.updateBankRule(initialRule.id, update);
        }
      } else {
        await api.createBankRule(bankRuleFormToCreate(values, companyId));
      }
      onSaved();
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setError(
        persianApiErrorMessage(
          err,
          initialRule ? "ویرایش قاعده ناموفق بود." : "ایجاد قاعده ناموفق بود.",
        ),
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent
        className="max-h-[90vh] overflow-y-auto"
        data-testid="bank-rule-form-dialog"
      >
        <DialogHeader>
          <DialogTitle>{initialRule ? "ویرایش قاعدهٔ بانکی" : "قاعدهٔ بانکی جدید"}</DialogTitle>
          <DialogDescription>
            وقتی ردیف صورت‌حساب با این الگو بخواند، قاعده حساب مقصد را پیشنهاد می‌کند؛
            همهٔ شرط‌ها با هم باید برقرار باشند (و).
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="bank-rule-name">نام قاعده</Label>
            <Input
              id="bank-rule-name"
              data-testid="bank-rule-form-name"
              value={values.name}
              onChange={(event) => setValue("name", event.target.value)}
              placeholder="مثلاً کارمزد اسنپ‌پی"
            />
            {errors.name ? (
              <p className="text-sm text-destructive" role="alert">
                {errors.name}
              </p>
            ) : null}
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="bank-rule-direction">جهت مبلغ</Label>
              <Select
                value={values.direction}
                onValueChange={(v) => v && setValue("direction", v as BankRuleDirection)}
              >
                <SelectTrigger id="bank-rule-direction" data-testid="bank-rule-form-direction">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {BANK_RULE_DIRECTION_OPTIONS.map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="bank-rule-bank-key">کلید بانک (اختیاری)</Label>
              <Input
                id="bank-rule-bank-key"
                data-testid="bank-rule-form-bank-key"
                value={values.bankKey}
                onChange={(event) => setValue("bankKey", event.target.value)}
                placeholder="خالی یعنی همهٔ بانک‌ها"
                dir="ltr"
              />
            </div>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="bank-rule-description">شرح شامل</Label>
              <Input
                id="bank-rule-description"
                data-testid="bank-rule-form-description"
                value={values.descriptionContains}
                onChange={(event) => setValue("descriptionContains", event.target.value)}
                placeholder="مثلاً اسنپ"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="bank-rule-counterparty">طرف حساب شامل</Label>
              <Input
                id="bank-rule-counterparty"
                data-testid="bank-rule-form-counterparty"
                value={values.counterpartyContains}
                onChange={(event) => setValue("counterpartyContains", event.target.value)}
                placeholder="مثلاً اسنپ‌پی"
              />
            </div>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="bank-rule-amount-min">حداقل مبلغ (تومان)</Label>
              <Input
                id="bank-rule-amount-min"
                data-testid="bank-rule-form-amount-min"
                value={values.amountMinToman}
                onChange={(event) => setValue("amountMinToman", event.target.value)}
                inputMode="numeric"
                placeholder="بدون حداقل"
              />
              {errors.amountMinToman ? (
                <p className="text-sm text-destructive" role="alert">
                  {errors.amountMinToman}
                </p>
              ) : null}
            </div>
            <div className="space-y-2">
              <Label htmlFor="bank-rule-amount-max">حداکثر مبلغ (تومان)</Label>
              <Input
                id="bank-rule-amount-max"
                data-testid="bank-rule-form-amount-max"
                value={values.amountMaxToman}
                onChange={(event) => setValue("amountMaxToman", event.target.value)}
                inputMode="numeric"
                placeholder="بدون حداکثر"
              />
              {errors.amountMaxToman ? (
                <p className="text-sm text-destructive" role="alert">
                  {errors.amountMaxToman}
                </p>
              ) : null}
            </div>
          </div>

          <div className="space-y-2">
            <Label htmlFor="bank-rule-target-account">حساب مقصد</Label>
            <Select
              value={values.targetAccountId === null ? "" : String(values.targetAccountId)}
              onValueChange={(v) => setValue("targetAccountId", v ? Number(v) : null)}
              disabled={accountsLoading}
            >
              <SelectTrigger id="bank-rule-target-account" data-testid="bank-rule-form-target-account">
                <SelectValue
                  placeholder={accountsLoading ? "در حال بارگذاری حساب‌ها…" : "یک حساب انتخاب کنید"}
                />
              </SelectTrigger>
              <SelectContent>
                {accounts.map((account) => (
                  <SelectItem key={account.id} value={String(account.id)}>
                    {fa(account.code)} — {account.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {errors.targetAccountId ? (
              <p className="text-sm text-destructive" role="alert">
                {errors.targetAccountId}
              </p>
            ) : null}
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="bank-rule-target-party">شناسهٔ طرف حساب (اختیاری)</Label>
              <Input
                id="bank-rule-target-party"
                data-testid="bank-rule-form-target-party"
                value={values.targetPartyId}
                onChange={(event) => setValue("targetPartyId", event.target.value)}
                inputMode="numeric"
                dir="ltr"
              />
              {errors.targetPartyId ? (
                <p className="text-sm text-destructive" role="alert">
                  {errors.targetPartyId}
                </p>
              ) : null}
            </div>
            <div className="space-y-2">
              <Label htmlFor="bank-rule-priority">اولویت (کمتر زودتر اجرا می‌شود)</Label>
              <Input
                id="bank-rule-priority"
                data-testid="bank-rule-form-priority"
                value={values.priority}
                onChange={(event) => setValue("priority", event.target.value)}
                inputMode="numeric"
                dir="ltr"
              />
              {errors.priority ? (
                <p className="text-sm text-destructive" role="alert">
                  {errors.priority}
                </p>
              ) : null}
            </div>
          </div>

          <div className="flex items-center justify-between gap-4">
            <Label htmlFor="bank-rule-enabled">فعال</Label>
            <Switch
              id="bank-rule-enabled"
              data-testid="bank-rule-form-enabled"
              checked={values.enabled}
              onCheckedChange={(checked) => setValue("enabled", checked)}
            />
          </div>

          {resetsTrust ? (
            <p className="flex items-start gap-2 text-sm text-amber-700" role="alert">
              <TriangleAlert className="mt-0.5 size-4 shrink-0" strokeWidth={2} aria-hidden />
              تغییر الگو یا حساب مقصد، تأییدهای این قاعده را صفر می‌کند و اگر خودکار باشد، دوباره به
              حالت پیشنهاد برمی‌گردد.
            </p>
          ) : null}

          {errors.form ? (
            <p className="text-sm text-destructive" role="alert">
              {errors.form}
            </p>
          ) : null}

          {error ? (
            <p className="text-sm text-destructive" role="alert">
              {error}
            </p>
          ) : null}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={saving}>
            انصراف
          </Button>
          <Button onClick={save} disabled={saving} data-testid="bank-rule-form-save">
            {saving ? "در حال ذخیره…" : initialRule ? "ذخیرهٔ تغییرات" : "ایجاد قاعده"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
