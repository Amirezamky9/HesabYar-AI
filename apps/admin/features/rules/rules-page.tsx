/**
 * Rules management page (US-562, Phase 5-7 ۵خ W2) — under
 * یکپارچه‌سازی‌ها ← گردش‌کارها (phased design §7.4 (ح)).
 *
 * Three tabs: the rules list (status, owner, last execution),
 * the execution history + audit log (all rules, newest first),
 * and the rule-set match modes (first_match / all_matches per
 * event type). Create/edit opens the rule form dialog; a rule's
 * detail dialog carries its dry-run panel, live evaluate and its
 * own history. Every action calls the US-561 REST endpoints —
 * no direct DB access from the frontend.
 *
 * Ownership is explicit on every card: user-defined rules carry a
 * «تعریف کاربر» badge, system rules a «سیستمی» badge. The note
 * under the list states the B2 rule plainly: learned mappings
 * enter the engine as SUGGESTIONS only, never as auto-rules —
 * nothing on this page presents them as active automations.
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Lightbulb, Pencil, Plus, Trash2 } from "lucide-react";
import {
  ApiError,
  api,
  type RuleExecutionResponse,
  type RuleResponse,
  type RuleSetConfigResponse,
} from "@/lib/api-client";
import { formatIsoDateTime } from "@/lib/format-date";
import { faNumber } from "@/lib/utils";
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
  StatusBadge,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import { isPermissionDenied, persianApiErrorMessage } from "@/features/shared/api-error";
import { ExecutionRow, RuleDetailDialog } from "./rule-detail-dialog";
import { RuleFormDialog } from "./rule-form-dialog";
import {
  RULE_EVENT_TYPES,
  RULE_OWNER_LABELS,
  RULE_SET_MODES,
  actionKindLabel,
  describeActionFa,
  describeConditionsFa,
  eventTypeLabel,
  lastExecutionByRule,
  ruleDecisionLabel,
  ruleDecisionTone,
  ruleOwner,
  ruleSetModeDescription,
  ruleSetModeLabel,
} from "./rules-data";

type TabId = "rules" | "history" | "sets";

export function RulesPage() {
  const router = useRouter();
  const [tab, setTab] = React.useState<TabId>("rules");
  const [rules, setRules] = React.useState<RuleResponse[]>([]);
  const [executions, setExecutions] = React.useState<RuleExecutionResponse[]>([]);
  const [sets, setSets] = React.useState<RuleSetConfigResponse[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<unknown>(null);
  const [reloadKey, setReloadKey] = React.useState(0);
  const [eventFilter, setEventFilter] = React.useState("all");
  const [statusFilter, setStatusFilter] = React.useState("all");
  const [ownerFilter, setOwnerFilter] = React.useState("all");
  const [historyRuleFilter, setHistoryRuleFilter] = React.useState("all");
  const [formOpen, setFormOpen] = React.useState(false);
  const [formRule, setFormRule] = React.useState<RuleResponse | null>(null);
  const [detailRuleId, setDetailRuleId] = React.useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = React.useState<RuleResponse | null>(null);
  const [deleting, setDeleting] = React.useState(false);
  const [actionError, setActionError] = React.useState<string | null>(null);
  const [setSaving, setSetSaving] = React.useState<string | null>(null);

  const refresh = () => {
    setLoading(true);
    setError(null);
    setReloadKey((k) => k + 1);
  };

  React.useEffect(() => {
    let cancelled = false;
    Promise.all([
      api.listRules(),
      // Newest-first, up to 200: the last-execution map for the list
      // cards and the history tab share this one fetch.
      api.listExecutions({ limit: 200 }),
      api.listRuleSets(),
    ])
      .then(([rulesData, executionsData, setsData]) => {
        if (cancelled) return;
        setRules(rulesData);
        setExecutions(executionsData);
        setSets(setsData);
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
  }, [reloadKey, router]);

  const rulesById = React.useMemo(() => {
    const map = new Map<string, string>();
    for (const rule of rules) map.set(rule.id, rule.name);
    return map;
  }, [rules]);

  const lastByRule = React.useMemo(() => lastExecutionByRule(executions), [executions]);

  const setModeByEvent = React.useMemo(() => {
    const map = new Map<string, string>();
    for (const set of sets) map.set(set.event_type, set.match_mode);
    return map;
  }, [sets]);

  const detailRule = detailRuleId ? (rules.find((rule) => rule.id === detailRuleId) ?? null) : null;

  const filteredRules = rules.filter((rule) => {
    if (eventFilter !== "all" && rule.event_type !== eventFilter) return false;
    if (statusFilter === "enabled" && !rule.enabled) return false;
    if (statusFilter === "disabled" && rule.enabled) return false;
    if (ownerFilter !== "all" && ruleOwner(rule) !== ownerFilter) return false;
    return true;
  });

  const filteredHistory = executions.filter((execution) => {
    if (historyRuleFilter === "all") return true;
    if (historyRuleFilter === "orphan") return execution.rule_id === null;
    return execution.rule_id === historyRuleFilter;
  });

  const openCreate = () => {
    setFormRule(null);
    setFormOpen(true);
  };

  const openEdit = (rule: RuleResponse) => {
    setDetailRuleId(null);
    setFormRule(rule);
    setFormOpen(true);
  };

  const toggleEnabled = async (rule: RuleResponse) => {
    setActionError(null);
    try {
      if (rule.enabled) {
        await api.disableRule(rule.id);
      } else {
        await api.enableRule(rule.id);
      }
      refresh();
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setActionError(persianApiErrorMessage(err, "تغییر وضعیت قانون ناموفق بود."));
    }
  };

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    setActionError(null);
    try {
      await api.deleteRule(deleteTarget.id);
      setDeleteTarget(null);
      setDetailRuleId(null);
      refresh();
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setActionError(persianApiErrorMessage(err, "حذف قانون ناموفق بود."));
      setDeleteTarget(null);
    } finally {
      setDeleting(false);
    }
  };

  const changeSetMode = async (eventType: string, matchMode: string) => {
    setSetSaving(eventType);
    setActionError(null);
    try {
      const updated = await api.setRuleSetMode(eventType, { match_mode: matchMode });
      setSets((prev) => {
        const next = prev.filter((set) => set.event_type !== eventType);
        next.push(updated);
        return next;
      });
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setActionError(persianApiErrorMessage(err, "تغییر حالت مجموعه ناموفق بود."));
    } finally {
      setSetSaving(null);
    }
  };

  if (error && isPermissionDenied(error)) {
    return <PermissionDeniedState message={persianApiErrorMessage(error, "دریافت قوانین ناموفق بود.")} />;
  }

  if (error) {
    return <ErrorState message={persianApiErrorMessage(error, "دریافت قوانین ناموفق بود.")} onRetry={refresh} />;
  }

  return (
    <div className="space-y-6" data-testid="rules-page">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">گردش‌کارها</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            قوانین خودکار: وقتی یک رویداد رخ می‌دهد و شرط‌ها برقرارند، اقدام انجام می‌شود. هر قانون را
            پیش از فعال‌کردن آزمایش کنید.
          </p>
        </div>
        <Button onClick={openCreate} data-testid="rules-new">
          <Plus className="size-4" strokeWidth={2} aria-hidden />
          قانون جدید
        </Button>
      </div>

      {actionError ? (
        <p className="text-sm text-destructive" role="alert">
          {actionError}
        </p>
      ) : null}

      <Tabs value={tab} onValueChange={(value) => value && setTab(value as TabId)}>
        <TabsList aria-label="بخش‌های گردش‌کارها">
          <TabsTrigger value="rules">قوانین</TabsTrigger>
          <TabsTrigger value="history">تاریخچهٔ اجراها</TabsTrigger>
          <TabsTrigger value="sets">حالت تطابق مجموعه‌ها</TabsTrigger>
        </TabsList>

        <TabsContent value="rules" className="space-y-6">
          <Card>
            <CardContent className="flex gap-3 pt-4">
              <Lightbulb className="mt-0.5 size-5 shrink-0 text-muted-foreground" strokeWidth={2} aria-hidden />
              <p className="text-sm">
                نگاشت‌هایی که سیستم از اقدامات شما یاد می‌گیرد، فقط به‌عنوان <strong>پیشنهاد</strong>{" "}
                ذخیره می‌شوند و هرگز خودکار به قانون تبدیل نمی‌شوند؛ آن‌ها را در{" "}
                <Link href="/review-queue" className="font-medium underline">
                  صف بازبینی
                </Link>{" "}
                تأیید کنید. قانون‌های این صفحه فقط همان‌هایی هستند که شما (یا سیستم، با برچسب «سیستمی»)
                ساخته‌اید. قواعد کدگذاری خودکار ردیف‌های بانکی هم قانون‌های گردش‌کارند و در{" "}
                <Link href="/banking/bank-rules" className="font-medium underline">
                  قواعد تطبیق
                </Link>{" "}
                مدیریت می‌شوند.
              </p>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>فیلترها</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="grid gap-4 sm:grid-cols-3">
                <div className="space-y-2">
                  <Label htmlFor="rules-filter-event">رویداد</Label>
                  <Select value={eventFilter} onValueChange={(v) => v && setEventFilter(v)}>
                    <SelectTrigger id="rules-filter-event" data-testid="rules-filter-event">
                      <SelectValue>
                        {eventFilter === "all" ? "همهٔ رویدادها" : eventTypeLabel(eventFilter)}
                      </SelectValue>
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="all">همهٔ رویدادها</SelectItem>
                      {RULE_EVENT_TYPES.map((eventType) => (
                        <SelectItem key={eventType} value={eventType}>
                          {eventTypeLabel(eventType)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="rules-filter-status">وضعیت</Label>
                  <Select value={statusFilter} onValueChange={(v) => v && setStatusFilter(v)}>
                    <SelectTrigger id="rules-filter-status" data-testid="rules-filter-status">
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
                <div className="space-y-2">
                  <Label htmlFor="rules-filter-owner">سازنده</Label>
                  <Select value={ownerFilter} onValueChange={(v) => v && setOwnerFilter(v)}>
                    <SelectTrigger id="rules-filter-owner" data-testid="rules-filter-owner">
                      <SelectValue>
                        {ownerFilter === "all"
                          ? "همهٔ سازنده‌ها"
                          : ownerFilter === "user"
                            ? "تعریف کاربر"
                            : "سیستمی"}
                      </SelectValue>
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="all">همهٔ سازنده‌ها</SelectItem>
                      <SelectItem value="user">تعریف کاربر</SelectItem>
                      <SelectItem value="system">سیستمی</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>
            </CardContent>
          </Card>

          <section aria-label="قوانین" className="space-y-4">
            {loading ? (
              <div className="space-y-4" aria-label="در حال بارگذاری">
                {Array.from({ length: 3 }).map((_, i) => (
                  <Skeleton key={i} className="h-28 w-full" />
                ))}
              </div>
            ) : filteredRules.length === 0 ? (
              <Card data-testid="rules-empty-state">
                <CardContent className="py-8 text-center">
                  <p className="font-medium">
                    {rules.length === 0 ? "هنوز قانونی ساخته نشده است." : "قانونی با این فیلترها نیست."}
                  </p>
                  <p className="mt-1 text-sm text-muted-foreground">
                    {rules.length === 0
                      ? "با «قانون جدید» اولین قانون خود را بسازید؛ پیش از فعال‌کردن، آن را آزمایش کنید."
                      : "فیلترها را تغییر دهید یا قانون جدیدی بسازید."}
                  </p>
                </CardContent>
              </Card>
            ) : (
              <ul className="space-y-4">
                {filteredRules.map((rule) => (
                  <li key={rule.id}>
                    <RuleCard
                      rule={rule}
                      lastExecution={lastByRule.get(rule.id) ?? null}
                      onOpen={() => setDetailRuleId(rule.id)}
                      onEdit={() => openEdit(rule)}
                      onToggle={() => toggleEnabled(rule)}
                      onDelete={() => setDeleteTarget(rule)}
                    />
                  </li>
                ))}
              </ul>
            )}
          </section>
        </TabsContent>

        <TabsContent value="history" className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>تاریخچهٔ اجراها و ممیزی</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <p className="text-sm text-muted-foreground">
                هر آزمایش و هر اجرای زنده اینجا ثبت می‌شود؛ ممیزی فقط-افزودنی است و اجراها حتی پس از حذف
                قانونشان می‌مانند. جدیدترین {faNumber(200)} اجرا نمایش داده می‌شود.
              </p>
              <div className="max-w-sm space-y-2">
                <Label htmlFor="rules-history-filter">قانون</Label>
                <Select value={historyRuleFilter} onValueChange={(v) => v && setHistoryRuleFilter(v)}>
                  <SelectTrigger id="rules-history-filter" data-testid="rules-history-filter">
                    <SelectValue>
                      {historyRuleFilter === "all"
                        ? "همهٔ قوانین"
                        : historyRuleFilter === "orphan"
                          ? "بدون قانون (تعریف ذخیره‌نشده یا حذف‌شده)"
                          : (rulesById.get(historyRuleFilter) ?? historyRuleFilter)}
                    </SelectValue>
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="all">همهٔ قوانین</SelectItem>
                    <SelectItem value="orphan">بدون قانون (تعریف ذخیره‌نشده یا حذف‌شده)</SelectItem>
                    {rules.map((rule) => (
                      <SelectItem key={rule.id} value={rule.id}>
                        {rule.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              {loading ? (
                <div className="space-y-2" aria-label="در حال بارگذاری">
                  <Skeleton className="h-16 w-full" />
                  <Skeleton className="h-16 w-full" />
                </div>
              ) : filteredHistory.length === 0 ? (
                <p className="text-sm text-muted-foreground" data-testid="rules-history-empty">
                  هنوز اجرایی ثبت نشده است.
                </p>
              ) : (
                <ol className="space-y-2">
                  {filteredHistory.map((execution) => (
                    <li key={execution.id} className="space-y-1">
                      <p className="text-sm font-medium">
                        {execution.rule_id && rulesById.get(execution.rule_id)
                          ? rulesById.get(execution.rule_id)
                          : execution.rule_id
                            ? "قانون حذف‌شده"
                            : "تعریف ذخیره‌نشده"}
                      </p>
                      <ExecutionRow execution={execution} />
                    </li>
                  ))}
                </ol>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="sets" className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>حالت تطابق مجموعه‌ها</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <p className="text-sm text-muted-foreground">
                هر نوع رویداد یک «مجموعهٔ قانون» دارد. در حالت «اولین تطبیق» فقط اولین قانونِ مطابق
                (با کمترین اولویت) اجرا می‌شود؛ در حالت «همهٔ تطبیق‌ها» همهٔ قوانین مطابق اجرا می‌شوند.
                مجموعه‌هایی که تنظیم نشده‌اند، «اولین تطبیق» هستند.
              </p>
              {loading ? (
                <div className="space-y-2" aria-label="در حال بارگذاری">
                  <Skeleton className="h-16 w-full" />
                  <Skeleton className="h-16 w-full" />
                </div>
              ) : (
                <ul className="space-y-2">
                  {RULE_EVENT_TYPES.map((eventType) => {
                    const mode = setModeByEvent.get(eventType) ?? "first_match";
                    return (
                      <li
                        key={eventType}
                        className="flex flex-wrap items-center justify-between gap-4 rounded-lg border p-3"
                      >
                        <div>
                          <p className="text-sm font-medium">{eventTypeLabel(eventType)}</p>
                          <p className="text-sm text-muted-foreground">{ruleSetModeDescription(mode)}</p>
                        </div>
                        <div className="w-44">
                          <Select
                            value={mode}
                            onValueChange={(v) => v && changeSetMode(eventType, v)}
                            disabled={setSaving === eventType}
                          >
                            <SelectTrigger
                              data-testid={`rule-set-mode-${eventType}`}
                              aria-label={`حالت تطابق ${eventTypeLabel(eventType)}`}
                            >
                              <SelectValue>{ruleSetModeLabel(mode)}</SelectValue>
                            </SelectTrigger>
                            <SelectContent>
                              {RULE_SET_MODES.map((setMode) => (
                                <SelectItem key={setMode} value={setMode}>
                                  {ruleSetModeLabel(setMode)}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              )}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>

      {formOpen ? (
        <RuleFormDialog
          key={formRule?.id ?? "new"}
          open={formOpen}
          onClose={() => setFormOpen(false)}
          initialRule={formRule}
          onSaved={() => {
            setFormOpen(false);
            refresh();
          }}
        />
      ) : null}

      <RuleDetailDialog
        rule={detailRule}
        rulesById={rulesById}
        onClose={() => setDetailRuleId(null)}
        onMutated={refresh}
        onEdit={openEdit}
        onDelete={(rule) => setDeleteTarget(rule)}
      />

      <AlertDialog
        open={deleteTarget !== null}
        onOpenChange={(isOpen) => {
          if (!isOpen) setDeleteTarget(null);
        }}
      >
        <AlertDialogContent data-testid="rule-delete-dialog">
          <AlertDialogHeader>
            <AlertDialogTitle>حذف قانون؟</AlertDialogTitle>
            <AlertDialogDescription>
              قانون «{deleteTarget?.name}» حذف می‌شود. اجراهای ثبت‌شدهٔ آن در ممیزی می‌مانند و پاک
              نمی‌شوند. این کار قابل بازگشت نیست.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <Button variant="outline" onClick={() => setDeleteTarget(null)}>
              انصراف
            </Button>
            <Button variant="destructive" onClick={confirmDelete} disabled={deleting} data-testid="rule-delete-confirm">
              {deleting ? "در حال حذف…" : "حذف قانون"}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function RuleCard({
  rule,
  lastExecution,
  onOpen,
  onEdit,
  onToggle,
  onDelete,
}: {
  rule: RuleResponse;
  lastExecution: RuleExecutionResponse | null;
  onOpen: () => void;
  onEdit: () => void;
  onToggle: () => void;
  onDelete: () => void;
}) {
  const owner = ruleOwner(rule);
  return (
    <Card data-testid="rule-card">
      <CardContent className="space-y-3 pt-4">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-base font-semibold">{rule.name}</h2>
          <StatusBadge
            status={rule.enabled ? "active" : "inactive"}
            label={rule.enabled ? "فعال" : "غیرفعال"}
            tone={rule.enabled ? "success" : "outline"}
          />
          <StatusBadge status={owner} label={RULE_OWNER_LABELS[owner].label} tone={RULE_OWNER_LABELS[owner].tone} />
          <Badge variant="outline">{eventTypeLabel(rule.event_type)}</Badge>
        </div>
        {rule.description ? <p className="text-sm">{rule.description}</p> : null}
        <p className="text-sm">
          {actionKindLabel(rule.action.kind)} — {describeActionFa(rule.action)}
        </p>
        <p className="text-sm text-muted-foreground">{describeConditionsFa(rule.conditions)}</p>
        <dl className="flex flex-wrap gap-x-6 gap-y-2 text-sm">
          <div className="flex gap-2">
            <dt className="text-muted-foreground">اولویت:</dt>
            <dd>{faNumber(rule.priority)}</dd>
          </div>
          <div className="flex gap-2">
            <dt className="text-muted-foreground">آخرین اجرا:</dt>
            <dd>
              {lastExecution ? (
                <>
                  {formatIsoDateTime(lastExecution.executed_at)} —{" "}
                  <StatusBadge
                    status={lastExecution.decision}
                    label={ruleDecisionLabel(lastExecution.decision)}
                    tone={ruleDecisionTone(lastExecution.decision)}
                  />
                </>
              ) : (
                "هنوز اجرا نشده است"
              )}
            </dd>
          </div>
        </dl>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" onClick={onOpen} data-testid="rule-card-open">
            جزئیات و آزمایش
          </Button>
          <Button variant="outline" size="sm" onClick={onEdit}>
            <Pencil className="size-4" strokeWidth={2} aria-hidden />
            ویرایش
          </Button>
          <Button variant="outline" size="sm" onClick={onToggle} data-testid="rule-card-toggle">
            {rule.enabled ? "غیرفعال کردن" : "فعال کردن"}
          </Button>
          <Button variant="outline" size="sm" onClick={onDelete} data-testid="rule-card-delete">
            <Trash2 className="size-4" strokeWidth={2} aria-hidden />
            حذف
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
