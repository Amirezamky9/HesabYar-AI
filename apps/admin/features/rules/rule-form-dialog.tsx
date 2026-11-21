/**
 * The rule create/edit dialog (US-562).
 *
 * «وقتی <رویداد> + <شرط‌ها> ← <اقدام>»: the event picker offers only
 * the twelve canonical types, the condition builder edits the
 * JSONLogic-style DSL as rows (and/or of comparisons — a stored tree
 * beyond the builder opens as raw JSON, never silently rewritten),
 * and the action picker offers only the closed six kinds, each with
 * its own parameter fields. Amounts are typed in تومان and converted
 * to whole Rial at the boundary. The «آزمایش پیش از ذخیره» section
 * dry-runs the unsaved definition (POST /rules/dry-run-definition)
 * so the operator tests before saving.
 *
 * The parent remounts this dialog per rule (key={rule?.id ?? "new"}),
 * so the initial values are read once from initialRule.
 */

"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { Plus, Trash2 } from "lucide-react";
import {
  ApiError,
  api,
  type DryRunRequest,
  type RuleResponse,
} from "@/lib/api-client";
import {
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
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
  Switch,
  Textarea,
} from "@/components/ui";
import { persianApiErrorMessage } from "@/features/shared/api-error";
import { RuleTestPanel } from "./dry-run-panel";
import {
  CONDITION_COMBINATOR_LABELS,
  CONDITION_FIELDS,
  CONDITION_OPERATOR_LABELS,
  CONDITION_OPERATORS,
  NOTIFY_CHANNELS,
  RULE_ACTION_KINDS,
  RULE_EVENT_TYPES,
  RULE_SEVERITY_LABELS,
  actionKindDescription,
  actionKindLabel,
  conditionOperatorLabel,
  conditionsToBuilder,
  emptyConditionRow,
  emptyDraftLine,
  emptyRuleFormValues,
  eventTypeDescription,
  eventTypeLabel,
  formValuesToCreate,
  formValuesToUpdate,
  notifyChannelLabel,
  rowsToConditions,
  ruleSeverityLabel,
  ruleToFormValues,
  validateConditionRow,
  type ConditionCombinator,
  type ConditionOperator,
  type ConditionRow,
  type DraftLineDraft,
  type RuleFormValues,
} from "./rules-data";

interface RuleFormDialogProps {
  open: boolean;
  onClose: () => void;
  /** Null = create a new rule. */
  initialRule: RuleResponse | null;
  /** Called after a successful save so the parent refreshes + closes. */
  onSaved: () => void;
}

export function RuleFormDialog({ open, onClose, initialRule, onSaved }: RuleFormDialogProps) {
  const router = useRouter();
  const [values, setValues] = React.useState<RuleFormValues>(() =>
    initialRule ? ruleToFormValues(initialRule) : emptyRuleFormValues(),
  );
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const setValue = <K extends keyof RuleFormValues>(key: K, value: RuleFormValues[K]) => {
    setValues((prev) => ({ ...prev, [key]: value }));
  };

  const setRow = (id: string, patch: Partial<ConditionRow>) => {
    setValues((prev) => ({
      ...prev,
      rows: prev.rows.map((row) => (row.id === id ? { ...row, ...patch } : row)),
    }));
  };

  const addRow = () => {
    setValues((prev) => ({
      ...prev,
      rows: [...prev.rows, emptyConditionRow(crypto.randomUUID())],
    }));
  };

  const removeRow = (id: string) => {
    setValues((prev) => ({ ...prev, rows: prev.rows.filter((row) => row.id !== id) }));
  };

  const switchToAdvanced = () => {
    const conditions = rowsToConditions({ combinator: values.combinator, rows: values.rows });
    setValues((prev) => ({
      ...prev,
      conditionsAdvanced: true,
      rawConditions: JSON.stringify(conditions ?? {}, null, 2),
    }));
  };

  const switchToBuilder = () => {
    try {
      const parsed: unknown = JSON.parse(values.rawConditions);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        setError("شرط‌ها باید یک شیء JSON باشند.");
        return;
      }
      const { state, unsupported } = conditionsToBuilder(parsed as Record<string, unknown>);
      if (unsupported) {
        setError("این شرط‌ها با سازندهٔ شرط قابل ویرایش نیستند؛ در حالت JSON بمانید.");
        return;
      }
      setError(null);
      setValues((prev) => ({
        ...prev,
        conditionsAdvanced: false,
        combinator: state.combinator,
        rows: state.rows,
      }));
    } catch {
      setError("JSON شرط‌ها معتبر نیست.");
    }
  };

  const setLine = (id: string, patch: Partial<DraftLineDraft>) => {
    setValues((prev) => ({
      ...prev,
      lines: prev.lines.map((line) => (line.id === id ? { ...line, ...patch } : line)),
    }));
  };

  const addLine = () => {
    setValues((prev) => ({
      ...prev,
      lines: [...prev.lines, emptyDraftLine(crypto.randomUUID(), "credit")],
    }));
  };

  const removeLine = (id: string) => {
    setValues((prev) => ({ ...prev, lines: prev.lines.filter((line) => line.id !== id) }));
  };

  const toggleChannel = (channel: string, checked: boolean) => {
    setValues((prev) => ({
      ...prev,
      channels: checked
        ? [...prev.channels, channel]
        : prev.channels.filter((c) => c !== channel),
    }));
  };

  const save = async () => {
    setError(null);
    setSaving(true);
    try {
      if (initialRule) {
        const converted = formValuesToUpdate(values);
        if ("error" in converted) {
          setError(converted.error);
          return;
        }
        await api.updateRule(initialRule.id, converted.body);
      } else {
        const converted = formValuesToCreate(values);
        if ("error" in converted) {
          setError(converted.error);
          return;
        }
        await api.createRule(converted.body);
      }
      onSaved();
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setError(persianApiErrorMessage(err, "ذخیرهٔ قانون ناموفق بود."));
    } finally {
      setSaving(false);
    }
  };

  const draft = formValuesToCreate(values);
  const draftAction = "body" in draft ? draft.body.action : null;

  const runDryRun = async (input: DryRunRequest) => {
    const converted = formValuesToCreate(values);
    if ("error" in converted) throw new Error(converted.error);
    return api.dryRunDefinition({
      ...input,
      name: converted.body.name,
      conditions: converted.body.conditions ?? {},
      action: converted.body.action,
    });
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(isOpen) => {
        if (!isOpen) onClose();
      }}
    >
      <DialogContent
        className="max-h-[90vh] overflow-y-auto"
        data-testid="rule-form-dialog"
        showCloseButton={false}
      >
        <DialogHeader>
          <DialogTitle>{initialRule ? "ویرایش قانون" : "قانون جدید"}</DialogTitle>
          <DialogDescription>
            وقتی یک رویداد رخ می‌دهد و شرط‌ها برقرارند، اقدام انجام می‌شود. اقدام فقط از مجموعهٔ مجاز
            انتخاب می‌شود و ثبت پیش‌نویس همیشه از درگاه سیاستی می‌گذرد.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-6">
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="rule-form-name">نام قانون</Label>
              <Input
                id="rule-form-name"
                data-testid="rule-form-name"
                value={values.name}
                onChange={(e) => setValue("name", e.target.value)}
                placeholder="مثلاً اطلاع‌رسانی سفارش‌های بزرگ زرین‌پال"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="rule-form-event">رویداد</Label>
              <Select value={values.eventType} onValueChange={(v) => v && setValue("eventType", v)}>
                <SelectTrigger id="rule-form-event" data-testid="rule-form-event">
                  <SelectValue>{eventTypeLabel(values.eventType)}</SelectValue>
                </SelectTrigger>
                <SelectContent>
                  {RULE_EVENT_TYPES.map((eventType) => (
                    <SelectItem key={eventType} value={eventType}>
                      {eventTypeLabel(eventType)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-sm text-muted-foreground">{eventTypeDescription(values.eventType)}</p>
            </div>
            <div className="space-y-2 sm:col-span-2">
              <Label htmlFor="rule-form-description">توضیحات (اختیاری)</Label>
              <Textarea
                id="rule-form-description"
                data-testid="rule-form-description"
                value={values.description}
                onChange={(e) => setValue("description", e.target.value)}
                placeholder="این قانون چه کار می‌کند؟"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="rule-form-priority">اولویت</Label>
              <Input
                id="rule-form-priority"
                data-testid="rule-form-priority"
                inputMode="numeric"
                value={values.priority}
                onChange={(e) => setValue("priority", e.target.value)}
                placeholder="۱۰۰"
              />
              <p className="text-sm text-muted-foreground">
                عدد کوچک‌تر زودتر اجرا می‌شود؛ پیش‌فرض ۱۰۰ است.
              </p>
            </div>
            <div className="flex min-h-11 items-center gap-2">
              <Switch
                id="rule-form-enabled"
                data-testid="rule-form-enabled"
                checked={values.enabled}
                onCheckedChange={(checked) => setValue("enabled", checked)}
              />
              <Label htmlFor="rule-form-enabled">فعال (قانون غیرفعال زنده اجرا نمی‌شود، ولی آزمایش آن ممکن است)</Label>
            </div>
          </div>

          <Card>
            <CardHeader>
              <CardTitle>شرط‌ها</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              {values.conditionsAdvanced ? (
                <div className="space-y-2">
                  <p className="text-sm text-muted-foreground">
                    این شرط‌ها خارج از سازندهٔ شرط هستند و فقط به‌صورت JSON ویرایش می‌شوند؛ ذخیره،
                    آن‌ها را دست‌نخورده نگه می‌دارد.
                  </p>
                  <Label htmlFor="rule-form-raw-conditions">شرط‌ها (JSON)</Label>
                  <Textarea
                    id="rule-form-raw-conditions"
                    data-testid="rule-form-raw-conditions"
                    value={values.rawConditions}
                    onChange={(e) => setValue("rawConditions", e.target.value)}
                    dir="ltr"
                    className="min-h-32 text-start font-mono"
                  />
                  <div>
                    <Button variant="outline" onClick={switchToBuilder}>
                      بازگشت به سازندهٔ شرط
                    </Button>
                  </div>
                </div>
              ) : (
                <>
                  {values.rows.length === 0 ? (
                    <p className="text-sm text-muted-foreground">
                      بدون شرط — قانون روی همهٔ رویدادهای این نوع اجرا می‌شود. برای محدودکردن، شرط
                      اضافه کنید.
                    </p>
                  ) : null}
                  {values.rows.length >= 2 ? (
                    <div className="space-y-2">
                      <Label htmlFor="rule-form-combinator">ترکیب شرط‌ها</Label>
                      <Select
                        value={values.combinator}
                        onValueChange={(v) => v && setValue("combinator", v as ConditionCombinator)}
                      >
                        <SelectTrigger id="rule-form-combinator" data-testid="rule-form-combinator">
                          <SelectValue>{CONDITION_COMBINATOR_LABELS[values.combinator]}</SelectValue>
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="and">{CONDITION_COMBINATOR_LABELS.and}</SelectItem>
                          <SelectItem value="or">{CONDITION_COMBINATOR_LABELS.or}</SelectItem>
                        </SelectContent>
                      </Select>
                    </div>
                  ) : null}
                  <ul className="space-y-4">
                    {values.rows.map((row, index) => (
                      <ConditionRowEditor
                        key={row.id}
                        row={row}
                        index={index}
                        onChange={(patch) => setRow(row.id, patch)}
                        onRemove={() => removeRow(row.id)}
                      />
                    ))}
                  </ul>
                  <div className="flex flex-wrap gap-2">
                    <Button variant="outline" onClick={addRow} data-testid="rule-form-add-condition">
                      <Plus className="size-4" strokeWidth={2} aria-hidden />
                      افزودن شرط
                    </Button>
                    <Button variant="ghost" onClick={switchToAdvanced}>
                      ویرایش JSON
                    </Button>
                  </div>
                </>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>اقدام</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="rule-form-action">اقدام (از مجموعهٔ مجاز)</Label>
                <Select value={values.actionKind} onValueChange={(v) => v && setValue("actionKind", v)}>
                  <SelectTrigger id="rule-form-action" data-testid="rule-form-action">
                    <SelectValue>{actionKindLabel(values.actionKind)}</SelectValue>
                  </SelectTrigger>
                  <SelectContent>
                    {RULE_ACTION_KINDS.map((kind) => (
                      <SelectItem key={kind} value={kind}>
                        {actionKindLabel(kind)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-sm text-muted-foreground">{actionKindDescription(values.actionKind)}</p>
              </div>

              {values.actionKind === "create_draft" ? (
                <div className="space-y-4">
                  <div className="space-y-2">
                    <Label htmlFor="rule-form-draft-description">شرح پیش‌نویس (اختیاری)</Label>
                    <Input
                      id="rule-form-draft-description"
                      data-testid="rule-form-draft-description"
                      value={values.draftDescription}
                      onChange={(e) => setValue("draftDescription", e.target.value)}
                      placeholder="شرح سند روزنامه"
                    />
                  </div>
                  <ul className="space-y-4">
                    {values.lines.map((line, index) => (
                      <li key={line.id} className="space-y-4 rounded-lg border p-4">
                        <div className="flex items-center justify-between gap-2">
                          <h3 className="font-medium">ردیف {index + 1}</h3>
                          <Button
                            variant="ghost"
                            size="sm"
                            className="min-h-11 min-w-11"
                            onClick={() => removeLine(line.id)}
                            disabled={values.lines.length <= 2}
                            aria-label={`حذف ردیف ${index + 1}`}
                          >
                            <Trash2 className="size-4" strokeWidth={2} aria-hidden />
                          </Button>
                        </div>
                        <div className="grid gap-4 sm:grid-cols-2">
                          <div className="space-y-2">
                            <Label htmlFor={`rule-form-line-account-${index}`}>کد حساب</Label>
                            <Input
                              id={`rule-form-line-account-${index}`}
                              data-testid={`rule-form-line-account-${index}`}
                              value={line.accountCode}
                              onChange={(e) => setLine(line.id, { accountCode: e.target.value })}
                              placeholder="مثلاً 101"
                              dir="ltr"
                              className="text-start"
                            />
                          </div>
                          <div className="space-y-2">
                            <Label htmlFor={`rule-form-line-side-${index}`}>سمت</Label>
                            <Select
                              value={line.side}
                              onValueChange={(v) => v && setLine(line.id, { side: v as "debit" | "credit" })}
                            >
                              <SelectTrigger id={`rule-form-line-side-${index}`}>
                                <SelectValue>{line.side === "debit" ? "بدهکار" : "بستانکار"}</SelectValue>
                              </SelectTrigger>
                              <SelectContent>
                                <SelectItem value="debit">بدهکار</SelectItem>
                                <SelectItem value="credit">بستانکار</SelectItem>
                              </SelectContent>
                            </Select>
                          </div>
                          <div className="space-y-2">
                            <Label htmlFor={`rule-form-line-amount-kind-${index}`}>مبلغ</Label>
                            <Select
                              value={line.amountKind}
                              onValueChange={(v) => v && setLine(line.id, { amountKind: v as "literal" | "var" })}
                            >
                              <SelectTrigger id={`rule-form-line-amount-kind-${index}`}>
                                <SelectValue>
                                  {line.amountKind === "literal" ? "مبلغ ثابت (تومان)" : "از رویداد"}
                                </SelectValue>
                              </SelectTrigger>
                              <SelectContent>
                                <SelectItem value="literal">مبلغ ثابت (تومان)</SelectItem>
                                <SelectItem value="var">از رویداد</SelectItem>
                              </SelectContent>
                            </Select>
                          </div>
                          {line.amountKind === "literal" ? (
                            <div className="space-y-2">
                              <Label htmlFor={`rule-form-line-amount-${index}`}>مبلغ (تومان)</Label>
                              <Input
                                id={`rule-form-line-amount-${index}`}
                                data-testid={`rule-form-line-amount-${index}`}
                                inputMode="numeric"
                                value={line.amountToman}
                                onChange={(e) => setLine(line.id, { amountToman: e.target.value })}
                                placeholder="مثلاً ۱٬۰۰۰٬۰۰۰"
                              />
                            </div>
                          ) : (
                            <div className="space-y-2">
                              <Label htmlFor={`rule-form-line-var-${index}`}>فیلد رویداد</Label>
                              <Input
                                id={`rule-form-line-var-${index}`}
                                data-testid={`rule-form-line-var-${index}`}
                                value={line.varPath}
                                onChange={(e) => setLine(line.id, { varPath: e.target.value })}
                                placeholder="amount_irr"
                                dir="ltr"
                                className="text-start"
                              />
                            </div>
                          )}
                          <div className="space-y-2 sm:col-span-2">
                            <Label htmlFor={`rule-form-line-description-${index}`}>شرح ردیف (اختیاری)</Label>
                            <Input
                              id={`rule-form-line-description-${index}`}
                              value={line.description}
                              onChange={(e) => setLine(line.id, { description: e.target.value })}
                              placeholder="شرح این ردیف"
                            />
                          </div>
                        </div>
                      </li>
                    ))}
                  </ul>
                  <div>
                    <Button variant="outline" onClick={addLine} data-testid="rule-form-add-line">
                      <Plus className="size-4" strokeWidth={2} aria-hidden />
                      افزودن ردیف
                    </Button>
                  </div>
                </div>
              ) : null}

              {values.actionKind === "post_if_policy_allows" ? (
                <div className="grid gap-4 sm:grid-cols-2">
                  <div className="space-y-2">
                    <Label htmlFor="rule-form-draft-id">شناسهٔ پیش‌نویس (اختیاری)</Label>
                    <Input
                      id="rule-form-draft-id"
                      data-testid="rule-form-draft-id"
                      inputMode="numeric"
                      value={values.draftId}
                      onChange={(e) => setValue("draftId", e.target.value)}
                      placeholder="خالی یعنی پیش‌نویس خود رویداد"
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="rule-form-draft-id-field">فیلد شناسهٔ پیش‌نویس (اختیاری)</Label>
                    <Input
                      id="rule-form-draft-id-field"
                      data-testid="rule-form-draft-id-field"
                      value={values.draftIdField}
                      onChange={(e) => setValue("draftIdField", e.target.value)}
                      placeholder="مثلاً payload.draft_id"
                      dir="ltr"
                      className="text-start"
                    />
                  </div>
                  <p className="text-sm text-muted-foreground sm:col-span-2">
                    اگر هیچ‌کدام را ندهید، پیش‌نویسی ثبت می‌شود که خود رویداد ساخته است؛ اگر پیش‌نویسی
                    نباشد، اقدام رد می‌شود و چیزی ساخته نمی‌شود.
                  </p>
                </div>
              ) : null}

              {values.actionKind === "notify" ? (
                <div className="space-y-4">
                  <div className="space-y-2">
                    <Label htmlFor="rule-form-recipients">گیرنده‌ها</Label>
                    <Textarea
                      id="rule-form-recipients"
                      data-testid="rule-form-recipients"
                      value={values.recipients}
                      onChange={(e) => setValue("recipients", e.target.value)}
                      placeholder="گیرنده‌ها را با «،» جدا کنید"
                    />
                  </div>
                  <fieldset className="space-y-2">
                    <legend className="text-sm font-medium">کانال‌ها</legend>
                    <div className="flex flex-wrap gap-4">
                      {NOTIFY_CHANNELS.map((channel) => (
                        <div key={channel} className="flex min-h-11 items-center gap-2">
                          <Checkbox
                            id={`rule-form-channel-${channel}`}
                            data-testid={`rule-form-channel-${channel}`}
                            checked={values.channels.includes(channel)}
                            onCheckedChange={(checked) => toggleChannel(channel, checked === true)}
                          />
                          <Label htmlFor={`rule-form-channel-${channel}`}>{notifyChannelLabel(channel)}</Label>
                        </div>
                      ))}
                    </div>
                    <p className="text-sm text-muted-foreground">
                      در این نسخه، ایمیل بک‌اند ندارد (ارسال نمی‌شود ولی ثبت می‌شود) و پیامک با US-564
                      فعال می‌شود.
                    </p>
                  </fieldset>
                  <div className="space-y-2">
                    <Label htmlFor="rule-form-subject">موضوع</Label>
                    <Input
                      id="rule-form-subject"
                      data-testid="rule-form-subject"
                      value={values.subject}
                      onChange={(e) => setValue("subject", e.target.value)}
                      placeholder="موضوع اطلاع‌رسانی"
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="rule-form-message">متن</Label>
                    <Textarea
                      id="rule-form-message"
                      data-testid="rule-form-message"
                      value={values.message}
                      onChange={(e) => setValue("message", e.target.value)}
                      placeholder="متن اطلاع‌رسانی"
                    />
                    <p className="text-sm text-muted-foreground">
                      {"می‌توانید از جای‌نگهدارهایی مثل {{amount_irr}} استفاده کنید؛ مقدار آن‌ها از رویداد پر می‌شود."}
                    </p>
                  </div>
                </div>
              ) : null}

              {values.actionKind === "assign" ? (
                <div className="grid gap-4 sm:grid-cols-2">
                  <div className="space-y-2">
                    <Label htmlFor="rule-form-assignee">تخصیص به</Label>
                    <Input
                      id="rule-form-assignee"
                      data-testid="rule-form-assignee"
                      value={values.assignee}
                      onChange={(e) => setValue("assignee", e.target.value)}
                      placeholder="نام مسئول"
                    />
                  </div>
                  <ReviewItemIdField value={values.reviewItemId} onChange={(v) => setValue("reviewItemId", v)} />
                </div>
              ) : null}

              {values.actionKind === "tag" ? (
                <div className="grid gap-4 sm:grid-cols-2">
                  <div className="space-y-2">
                    <Label htmlFor="rule-form-tag">برچسب</Label>
                    <Input
                      id="rule-form-tag"
                      data-testid="rule-form-tag"
                      value={values.tag}
                      onChange={(e) => setValue("tag", e.target.value)}
                      placeholder="مثلاً فوری"
                    />
                  </div>
                  <ReviewItemIdField value={values.reviewItemId} onChange={(v) => setValue("reviewItemId", v)} />
                </div>
              ) : null}

              {values.actionKind === "set_field" ? (
                <div className="grid gap-4 sm:grid-cols-2">
                  <div className="space-y-2">
                    <Label htmlFor="rule-form-severity">شدت جدید</Label>
                    <Select value={values.severity} onValueChange={(v) => v && setValue("severity", v)}>
                      <SelectTrigger id="rule-form-severity" data-testid="rule-form-severity">
                        <SelectValue>{ruleSeverityLabel(values.severity)}</SelectValue>
                      </SelectTrigger>
                      <SelectContent>
                        {Object.keys(RULE_SEVERITY_LABELS).map((severity) => (
                          <SelectItem key={severity} value={severity}>
                            {ruleSeverityLabel(severity)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <p className="text-sm text-muted-foreground">
                      در این نسخه فقط شدت آیتم بازبینی تغییر می‌کند.
                    </p>
                  </div>
                  <ReviewItemIdField value={values.reviewItemId} onChange={(v) => setValue("reviewItemId", v)} />
                </div>
              ) : null}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>آزمایش پیش از ذخیره</CardTitle>
            </CardHeader>
            <CardContent>
              <RuleTestPanel
                eventType={values.eventType}
                action={draftAction}
                runDryRun={runDryRun}
                testIdPrefix="rule-form"
              />
            </CardContent>
          </Card>

          {error ? (
            <p className="text-sm text-destructive" role="alert">
              {error}
            </p>
          ) : null}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            انصراف
          </Button>
          <Button onClick={save} disabled={saving} data-testid="rule-form-save">
            {saving ? "در حال ذخیره…" : initialRule ? "ذخیرهٔ تغییرات" : "ساخت قانون"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ReviewItemIdField({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  return (
    <div className="space-y-2">
      <Label htmlFor="rule-form-review-item-id">آیتم بازبینی (اختیاری)</Label>
      <Input
        id="rule-form-review-item-id"
        data-testid="rule-form-review-item-id"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="خالی یعنی آیتم خود رویداد"
        dir="ltr"
        className="text-start"
      />
      <p className="text-sm text-muted-foreground">
        اگر خالی بماند، آیتم بازبینی خود رویداد هدف است؛ اگر رویداد آیتمی نداشته باشد، اقدام رد می‌شود.
      </p>
    </div>
  );
}

function ConditionRowEditor({
  row,
  index,
  onChange,
  onRemove,
}: {
  row: ConditionRow;
  index: number;
  onChange: (patch: Partial<ConditionRow>) => void;
  onRemove: () => void;
}) {
  const isCustomField = !CONDITION_FIELDS.some((field) => field.path === row.field);
  const rowError = validateConditionRow(row);
  const isListOperator = row.operator === "in" || row.operator === "not_in";
  const isAmount = row.field === "amount_irr";
  const isDate = row.field === "occurred_at";
  const valueLabel =
    isAmount && isListOperator
      ? "مقدارها (تومان، با «،» جدا کنید)"
      : isAmount
        ? "مبلغ (تومان)"
        : isListOperator
          ? "مقدارها (با «،» جدا کنید)"
          : "مقدار";
  return (
    <li className="space-y-2 rounded-lg border p-4" data-testid={`rule-form-condition-row-${index}`}>
      <div className="flex items-center justify-between gap-2">
        <h3 className="font-medium">شرط {index + 1}</h3>
        <Button
          variant="ghost"
          size="sm"
          className="min-h-11 min-w-11"
          onClick={onRemove}
          aria-label={`حذف شرط ${index + 1}`}
        >
          <Trash2 className="size-4" strokeWidth={2} aria-hidden />
        </Button>
      </div>
      <div className="grid gap-4 sm:grid-cols-3">
        <div className="space-y-2">
          <Label htmlFor={`rule-form-condition-field-${index}`}>فیلد</Label>
          <Select
            value={isCustomField ? "custom" : row.field}
            onValueChange={(v) => {
              if (!v) return;
              if (v === "custom") {
                onChange({ field: "payload.total_amount" });
              } else {
                onChange({ field: v });
              }
            }}
          >
            <SelectTrigger
              id={`rule-form-condition-field-${index}`}
              data-testid={`rule-form-condition-field-${index}`}
            >
              <SelectValue>
                {isCustomField
                  ? "فیلد دلخواه از پیلود"
                  : (CONDITION_FIELDS.find((f) => f.path === row.field)?.label ?? row.field)}
              </SelectValue>
            </SelectTrigger>
            <SelectContent>
              {CONDITION_FIELDS.map((field) => (
                <SelectItem key={field.path} value={field.path}>
                  {field.label}
                </SelectItem>
              ))}
              <SelectItem value="custom">فیلد دلخواه از پیلود…</SelectItem>
            </SelectContent>
          </Select>
          {isCustomField ? (
            <Input
              value={row.field}
              onChange={(e) => onChange({ field: e.target.value })}
              placeholder="مثلاً payload.total_amount"
              dir="ltr"
              className="text-start"
              aria-label="مسیر فیلد دلخواه"
              data-testid={`rule-form-condition-custom-field-${index}`}
            />
          ) : (
            <p className="text-sm text-muted-foreground">
              {CONDITION_FIELDS.find((f) => f.path === row.field)?.hint}
            </p>
          )}
        </div>
        <div className="space-y-2">
          <Label htmlFor={`rule-form-condition-operator-${index}`}>عملگر</Label>
          <Select
            value={row.operator}
            onValueChange={(v) => v && onChange({ operator: v as ConditionOperator })}
          >
            <SelectTrigger
              id={`rule-form-condition-operator-${index}`}
              data-testid={`rule-form-condition-operator-${index}`}
            >
              <SelectValue>{conditionOperatorLabel(row.operator)}</SelectValue>
            </SelectTrigger>
            <SelectContent>
              {CONDITION_OPERATORS.map((operator) => (
                <SelectItem key={operator} value={operator}>
                  {CONDITION_OPERATOR_LABELS[operator]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-2">
          <Label htmlFor={`rule-form-condition-value-${index}`}>{valueLabel}</Label>
          <Input
            id={`rule-form-condition-value-${index}`}
            data-testid={`rule-form-condition-value-${index}`}
            inputMode={isAmount ? "numeric" : undefined}
            value={row.value}
            onChange={(e) => onChange({ value: e.target.value })}
            placeholder={
              isAmount ? "مثلاً ۱٬۰۰۰٬۰۰۰" : isDate ? "2026-11-21" : isListOperator ? "الف، ب" : "مقدار"
            }
            dir={isAmount || row.field === "channel" || isDate ? "ltr" : undefined}
            className={isAmount || row.field === "channel" || isDate ? "text-start" : undefined}
          />
        </div>
      </div>
      {rowError && row.value.trim() !== "" ? (
        <p className="text-sm text-destructive" role="alert">
          {rowError}
        </p>
      ) : null}
    </li>
  );
}
