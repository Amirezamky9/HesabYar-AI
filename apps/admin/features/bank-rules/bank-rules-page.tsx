/**
 * Bank auto-coding rules page (US-565, Phase 5-7 ۵خ W3) — under
 * بانک و صندوق ← قواعد تطبیق (phased design §7.4 (ح)).
 *
 * Two tabs: the user-defined rules (W3 rules — «وقتی <الگوی ردیف> ←
 * <حساب>») and the suggestions those rules made for statement
 * lines. A rule is born in SUGGEST mode; after three confirmed
 * suggestions a human may promote it to AUTO (the US-552 pattern).
 * Learned B2 mappings never appear here as rules — they enter the
 * engine as suggestions only, via the review queue.
 *
 * The guarantee, stated on the page: an auto-coded line still goes
 * through the tie-out before anything posts, and a wrong auto-code
 * can be undone from the statement detail page (the mapping is
 * corrected; the statement stays a draft until approved).
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowUpCircle, Check, Lightbulb, Pencil, Plus, Trash2, X } from "lucide-react";
import {
  ApiError,
  api,
  type AccountResponse,
  type BankRuleResponse,
  type BankRuleSuggestionResponse,
} from "@/lib/api-client";
import { formatIsoDateTime } from "@/lib/format-date";
import { formatMoney } from "@/lib/format-money";
import { fa } from "@/lib/utils";
import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  Badge,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Skeleton,
  Switch,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import { isPermissionDenied, persianApiErrorMessage } from "@/features/shared/api-error";
import { CompanyPicker } from "@/features/shared/company-picker";
import { useCompany } from "@/features/shared/use-company";
import { BankRuleFormDialog } from "./bank-rule-form-dialog";
import {
  BANK_RULE_MODE_LABELS,
  BANK_RULE_SUGGESTION_STATUS_LABELS,
  describeBankRuleFa,
  formatConfirmationsFa,
  promotionHintFa,
  type BankRuleSuggestionStatus,
} from "./bank-rules-data";

type TabId = "rules" | "suggestions";

export function BankRulesPage() {
  const router = useRouter();
  const { companies, companyId, setCompanyId, loading: companyLoading, error: companyError } =
    useCompany();
  const [tab, setTab] = React.useState<TabId>("rules");
  const [rules, setRules] = React.useState<BankRuleResponse[]>([]);
  const [suggestions, setSuggestions] = React.useState<BankRuleSuggestionResponse[]>([]);
  const [accounts, setAccounts] = React.useState<AccountResponse[]>([]);
  const [accountsCompanyId, setAccountsCompanyId] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<unknown>(null);
  const [reloadKey, setReloadKey] = React.useState(0);
  const [modeFilter, setModeFilter] = React.useState("all");
  const [statusFilter, setStatusFilter] = React.useState("all");
  const [suggestionFilter, setSuggestionFilter] = React.useState("all");
  const [formOpen, setFormOpen] = React.useState(false);
  const [formRule, setFormRule] = React.useState<BankRuleResponse | null>(null);
  const [deleteTarget, setDeleteTarget] = React.useState<BankRuleResponse | null>(null);
  const [deleting, setDeleting] = React.useState(false);
  const [actionError, setActionError] = React.useState<string | null>(null);
  const [actingId, setActingId] = React.useState<string | null>(null);

  const refresh = () => {
    setLoading(true);
    setError(null);
    setReloadKey((k) => k + 1);
  };

  React.useEffect(() => {
    if (!companyId) return;
    let cancelled = false;
    Promise.all([
      api.listBankRules({ company: companyId }),
      api.listBankRuleSuggestions({ company: companyId }),
    ])
      .then(([rulesData, suggestionsData]) => {
        if (cancelled) return;
        setRules(rulesData);
        setSuggestions(suggestionsData);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.replace("/login");
          return;
        }
        setError(err);
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [companyId, reloadKey, router]);

  // Accounts are slow (~33s against remote Tryton) and only label the
  // target accounts — they load independently and must not block the
  // rules list.
  React.useEffect(() => {
    if (!companyId) return;
    let cancelled = false;
    api
      .listAccounts({ company: companyId, limit: 100 })
      .then((page) => {
        if (cancelled) return;
        setAccounts(page.items.filter((a) => a.postable && !a.closed));
        setAccountsCompanyId(companyId);
      })
      .catch(() => {
        if (cancelled) return;
        setAccounts([]);
        setAccountsCompanyId(companyId);
      });
    return () => {
      cancelled = true;
    };
  }, [companyId]);

  // Accounts belong to the company they were loaded for; until the
  // selected company's accounts arrive, the picker shows its loading
  // state (derived, not a second loading flag).
  const accountsLoading = accountsCompanyId !== companyId;

  const accountById = React.useMemo(() => {
    const map = new Map<number, AccountResponse>();
    for (const account of accounts) map.set(account.id, account);
    return map;
  }, [accounts]);

  const accountLabel = (accountId: number): string => {
    const account = accountById.get(accountId);
    return account ? `${fa(account.code)} — ${account.name}` : `حساب ${fa(accountId)}`;
  };

  const filteredRules = rules.filter((rule) => {
    if (modeFilter !== "all" && rule.mode !== modeFilter) return false;
    if (statusFilter === "enabled" && !rule.enabled) return false;
    if (statusFilter === "disabled" && rule.enabled) return false;
    return true;
  });

  const filteredSuggestions = suggestions.filter((suggestion) => {
    if (suggestionFilter === "all") return true;
    return suggestion.status === suggestionFilter;
  });

  const pendingCount = suggestions.filter((s) => s.status === "pending").length;

  const openCreate = () => {
    setFormRule(null);
    setFormOpen(true);
  };

  const openEdit = (rule: BankRuleResponse) => {
    setFormRule(rule);
    setFormOpen(true);
  };

  const runAction = async (id: string, action: () => Promise<unknown>, fallback: string) => {
    setActingId(id);
    setActionError(null);
    try {
      await action();
      refresh();
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setActionError(persianApiErrorMessage(err, fallback));
    } finally {
      setActingId(null);
    }
  };

  const toggleEnabled = (rule: BankRuleResponse) =>
    runAction(
      rule.id,
      () => (rule.enabled ? api.disableBankRule(rule.id) : api.enableBankRule(rule.id)),
      "تغییر وضعیت قاعده ناموفق بود.",
    );

  const promote = (rule: BankRuleResponse) =>
    runAction(rule.id, () => api.promoteBankRule(rule.id), "ارتقای قاعده ناموفق بود.");

  const demote = (rule: BankRuleResponse) =>
    runAction(rule.id, () => api.demoteBankRule(rule.id), "بازگردانی قاعده ناموفق بود.");

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    setActionError(null);
    try {
      await api.deleteBankRule(deleteTarget.id);
      setDeleteTarget(null);
      refresh();
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setActionError(persianApiErrorMessage(err, "حذف قاعده ناموفق بود."));
      setDeleteTarget(null);
    } finally {
      setDeleting(false);
    }
  };

  const confirmSuggestion = (suggestion: BankRuleSuggestionResponse) =>
    runAction(
      suggestion.id,
      () => api.confirmBankRuleSuggestion(suggestion.id),
      "تأیید پیشنهاد ناموفق بود.",
    );

  const rejectSuggestion = (suggestion: BankRuleSuggestionResponse) =>
    runAction(
      suggestion.id,
      () => api.rejectBankRuleSuggestion(suggestion.id),
      "رد پیشنهاد ناموفق بود.",
    );

  const loadError = error ?? companyError;
  if (loadError && isPermissionDenied(loadError)) {
    return (
      <PermissionDeniedState
        message={persianApiErrorMessage(loadError, "دریافت قواعد تطبیق ناموفق بود.")}
      />
    );
  }

  if (loadError) {
    return (
      <ErrorState
        message={persianApiErrorMessage(loadError, "دریافت قواعد تطبیق ناموفق بود.")}
        onRetry={refresh}
      />
    );
  }

  return (
    <div className="space-y-6" data-testid="bank-rules-page">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">قواعد تطبیق</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            وقتی ردیف صورت‌حساب با الگوی قاعده بخواند، قاعده حساب مقصد را پیشنهاد می‌کند؛ پس از سه
            تأیید، می‌توانید قاعده را خودکار کنید تا ردیف‌ها را خودش کد کند.
          </p>
        </div>
        <Button onClick={openCreate} disabled={!companyId} data-testid="bank-rules-new">
          <Plus className="size-4" strokeWidth={2} aria-hidden />
          قاعدهٔ جدید
        </Button>
      </div>

      <CompanyPicker companies={companies} companyId={companyId} onChange={setCompanyId} />

      {actionError ? (
        <p className="text-sm text-destructive" role="alert">
          {actionError}
        </p>
      ) : null}

      <Card>
        <CardContent className="flex gap-3 pt-4">
          <Lightbulb
            className="mt-0.5 size-5 shrink-0 text-muted-foreground"
            strokeWidth={2}
            aria-hidden
          />
          <p className="text-sm">
            نگاشت‌هایی که سیستم از اقدامات شما یاد می‌گیرد، فقط به‌عنوان <strong>پیشنهاد</strong>{" "}
            ذخیره می‌شوند و هرگز خودکار به قانون تبدیل نمی‌شوند. ردیف‌های خودکارکدشده هم پیش از
            ثبت، از کنترل تراز صورت‌حساب می‌گذرند؛ اگر کدگذاری اشتباه بود، از صفحهٔ صورت‌حساب
            می‌توانید آن را برگردانید و تصحیح کنید — صورت‌حساب تا تأیید حسابدار پیش‌نویس می‌ماند.
          </p>
        </CardContent>
      </Card>

      <Tabs value={tab} onValueChange={(value) => value && setTab(value as TabId)}>
        <TabsList aria-label="بخش‌های قواعد تطبیق">
          <TabsTrigger value="rules" data-testid="bank-rules-tab-rules">
            قوانین
          </TabsTrigger>
          <TabsTrigger value="suggestions" data-testid="bank-rules-tab-suggestions">
            پیشنهادها
            {pendingCount > 0 ? (
              <Badge variant="secondary" className="ms-2">
                {fa(pendingCount)}
              </Badge>
            ) : null}
          </TabsTrigger>
        </TabsList>

        <TabsContent value="rules" className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>فیلترها</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="bank-rules-filter-mode">حالت</Label>
                  <Select value={modeFilter} onValueChange={(v) => v && setModeFilter(v)}>
                    <SelectTrigger id="bank-rules-filter-mode" data-testid="bank-rules-filter-mode">
                      <SelectValue>
                        {modeFilter === "all"
                          ? "همهٔ حالت‌ها"
                          : BANK_RULE_MODE_LABELS[modeFilter as "suggest" | "auto"]}
                      </SelectValue>
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="all">همهٔ حالت‌ها</SelectItem>
                      <SelectItem value="suggest">پیشنهاد</SelectItem>
                      <SelectItem value="auto">خودکار</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="bank-rules-filter-status">وضعیت</Label>
                  <Select value={statusFilter} onValueChange={(v) => v && setStatusFilter(v)}>
                    <SelectTrigger
                      id="bank-rules-filter-status"
                      data-testid="bank-rules-filter-status"
                    >
                      <SelectValue>
                        {statusFilter === "all"
                          ? "همهٔ وضعیت‌ها"
                          : statusFilter === "enabled"
                            ? "فعال"
                            : "غیرفعال"}
                      </SelectValue>
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="all">همهٔ وضعیت‌ها</SelectItem>
                      <SelectItem value="enabled">فعال</SelectItem>
                      <SelectItem value="disabled">غیرفعال</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>
            </CardContent>
          </Card>

          {loading || companyLoading ? (
            <div className="space-y-4" aria-label="در حال بارگذاری">
              <Skeleton className="h-32 w-full" />
              <Skeleton className="h-32 w-full" />
            </div>
          ) : filteredRules.length === 0 ? (
            <Card>
              <CardContent className="pt-6">
                <p className="text-sm text-muted-foreground">
                  {rules.length === 0
                    ? "هنوز قاعده‌ای نساخته‌اید. با «قاعدهٔ جدید» اولین الگوی تطبیق را بسازید."
                    : "با این فیلترها قاعده‌ای پیدا نشد."}
                </p>
              </CardContent>
            </Card>
          ) : (
            <div className="space-y-4">
              {filteredRules.map((rule) => (
                <Card key={rule.id} data-testid="bank-rule-card" data-rule-id={rule.id}>
                  <CardHeader>
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <CardTitle className="text-base">{rule.name}</CardTitle>
                        <p className="mt-1 text-sm text-muted-foreground">
                          {describeBankRuleFa(rule)}
                        </p>
                      </div>
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge variant={rule.mode === "auto" ? "default" : "secondary"}>
                          {BANK_RULE_MODE_LABELS[rule.mode as "suggest" | "auto"] ?? rule.mode}
                        </Badge>
                        {!rule.enabled ? <Badge variant="outline">غیرفعال</Badge> : null}
                      </div>
                    </div>
                  </CardHeader>
                  <CardContent className="space-y-4">
                    <dl className="grid gap-2 text-sm sm:grid-cols-3">
                      <div>
                        <dt className="text-muted-foreground">حساب مقصد</dt>
                        <dd className="font-medium">{accountLabel(rule.target_account_id)}</dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground">تأییدها</dt>
                        <dd className="font-medium">{formatConfirmationsFa(rule)}</dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground">اولویت</dt>
                        <dd className="font-medium">{fa(rule.priority)}</dd>
                      </div>
                    </dl>
                    <p className="text-sm text-muted-foreground">{promotionHintFa(rule)}</p>
                    <div className="flex flex-wrap items-center gap-2">
                      {rule.mode === "suggest" && rule.auto_eligible ? (
                        <Button
                          size="sm"
                          onClick={() => promote(rule)}
                          disabled={actingId === rule.id}
                          data-testid="bank-rule-promote"
                        >
                          <ArrowUpCircle className="size-4" strokeWidth={2} aria-hidden />
                          ارتقا به خودکار
                        </Button>
                      ) : null}
                      {rule.mode === "auto" ? (
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => demote(rule)}
                          disabled={actingId === rule.id}
                          data-testid="bank-rule-demote"
                        >
                          بازگشت به پیشنهاد
                        </Button>
                      ) : null}
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => openEdit(rule)}
                        data-testid="bank-rule-edit"
                      >
                        <Pencil className="size-4" strokeWidth={2} aria-hidden />
                        ویرایش
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => setDeleteTarget(rule)}
                        data-testid="bank-rule-delete"
                      >
                        <Trash2 className="size-4" strokeWidth={2} aria-hidden />
                        حذف
                      </Button>
                      <div className="flex items-center gap-2 ms-auto">
                        <Label htmlFor={`bank-rule-enabled-${rule.id}`}>
                          {rule.enabled ? "فعال" : "غیرفعال"}
                        </Label>
                        <Switch
                          id={`bank-rule-enabled-${rule.id}`}
                          data-testid="bank-rule-toggle"
                          checked={rule.enabled}
                          onCheckedChange={() => toggleEnabled(rule)}
                          disabled={actingId === rule.id}
                        />
                      </div>
                    </div>
                  </CardContent>
                </Card>
              ))}
            </div>
          )}
        </TabsContent>

        <TabsContent value="suggestions" className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>فیلترها</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="space-y-2">
                <Label htmlFor="bank-rules-filter-suggestion">وضعیت پیشنهاد</Label>
                <Select
                  value={suggestionFilter}
                  onValueChange={(v) => v && setSuggestionFilter(v)}
                >
                  <SelectTrigger
                    id="bank-rules-filter-suggestion"
                    data-testid="bank-rules-filter-suggestion"
                  >
                    <SelectValue>
                      {suggestionFilter === "all"
                        ? "همهٔ وضعیت‌ها"
                        : BANK_RULE_SUGGESTION_STATUS_LABELS[
                            suggestionFilter as BankRuleSuggestionStatus
                          ]}
                    </SelectValue>
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="all">همهٔ وضعیت‌ها</SelectItem>
                    <SelectItem value="pending">در انتظار بررسی</SelectItem>
                    <SelectItem value="confirmed">تأیید شده</SelectItem>
                    <SelectItem value="rejected">رد شده</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </CardContent>
          </Card>

          {loading || companyLoading ? (
            <div className="space-y-4" aria-label="در حال بارگذاری">
              <Skeleton className="h-24 w-full" />
              <Skeleton className="h-24 w-full" />
            </div>
          ) : filteredSuggestions.length === 0 ? (
            <Card>
              <CardContent className="pt-6">
                <p className="text-sm text-muted-foreground">
                  {suggestions.length === 0
                    ? "هنوز پیشنهادی ثبت نشده است؛ پس از تطبیق صورت‌حساب، پیشنهادهای قواعد اینجا می‌آیند."
                    : "با این فیلتر پیشنهادی پیدا نشد."}
                </p>
              </CardContent>
            </Card>
          ) : (
            <div className="space-y-4">
              {filteredSuggestions.map((suggestion) => (
                <Card
                  key={suggestion.id}
                  data-testid="bank-rule-suggestion-card"
                  data-suggestion-id={suggestion.id}
                >
                  <CardContent className="space-y-3 pt-6">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <p className="font-medium">{suggestion.rule_name}</p>
                        <p className="mt-1 text-sm text-muted-foreground">
                          {suggestion.line_description} — {formatMoney(suggestion.line_amount_irr)}
                        </p>
                      </div>
                      <Badge
                        variant={suggestion.status === "pending" ? "default" : "secondary"}
                      >
                        {BANK_RULE_SUGGESTION_STATUS_LABELS[
                          suggestion.status as BankRuleSuggestionStatus
                        ] ?? suggestion.status}
                      </Badge>
                    </div>
                    <dl className="grid gap-2 text-sm sm:grid-cols-3">
                      <div>
                        <dt className="text-muted-foreground">حساب پیشنهادی</dt>
                        <dd className="font-medium">
                          {accountLabel(suggestion.target_account_id)}
                        </dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground">صورت‌حساب</dt>
                        <dd className="font-medium">
                          <Link
                            href={`/banking/statements/${suggestion.statement_id}`}
                            className="underline"
                          >
                            صورت‌حساب {fa(suggestion.statement_id)}
                          </Link>
                        </dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground">زمان پیشنهاد</dt>
                        <dd className="font-medium">
                          {suggestion.created_at
                            ? formatIsoDateTime(suggestion.created_at)
                            : "—"}
                        </dd>
                      </div>
                    </dl>
                    {suggestion.status === "pending" ? (
                      <div className="flex flex-wrap items-center gap-2">
                        <Button
                          size="sm"
                          onClick={() => confirmSuggestion(suggestion)}
                          disabled={actingId === suggestion.id}
                          data-testid="bank-rule-suggestion-confirm"
                        >
                          <Check className="size-4" strokeWidth={2} aria-hidden />
                          تأیید و اعمال
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => rejectSuggestion(suggestion)}
                          disabled={actingId === suggestion.id}
                          data-testid="bank-rule-suggestion-reject"
                        >
                          <X className="size-4" strokeWidth={2} aria-hidden />
                          رد
                        </Button>
                      </div>
                    ) : null}
                  </CardContent>
                </Card>
              ))}
            </div>
          )}
        </TabsContent>
      </Tabs>

      {formOpen && companyId ? (
        <BankRuleFormDialog
          key={formRule?.id ?? "new"}
          open={formOpen}
          onClose={() => setFormOpen(false)}
          initialRule={formRule}
          companyId={companyId}
          accounts={accounts}
          accountsLoading={accountsLoading}
          onSaved={() => {
            setFormOpen(false);
            refresh();
          }}
        />
      ) : null}

      <AlertDialog
        open={deleteTarget !== null}
        onOpenChange={(next) => !next && setDeleteTarget(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>حذف قاعدهٔ «{deleteTarget?.name}»؟</AlertDialogTitle>
            <AlertDialogDescription>
              قاعده حذف می‌شود و دیگر روی ردیف‌های تازه نمی‌نشیند؛ پیشنهادهای ثبت‌شدهٔ آن باقی
              می‌مانند.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <Button variant="outline" onClick={() => setDeleteTarget(null)} disabled={deleting}>
              انصراف
            </Button>
            <Button variant="destructive" onClick={confirmDelete} disabled={deleting}>
              {deleting ? "در حال حذف…" : "حذف قاعده"}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
