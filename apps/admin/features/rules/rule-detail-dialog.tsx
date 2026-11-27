/**
 * The rule detail dialog (US-562) — one rule's definition, its test
 * panel (dry-run + live evaluate) and its execution history (§6.7
 * audit log, newest first).
 *
 * The rule itself comes from the list (the list response carries the
 * full definition — no extra fetch); only the history is fetched
 * (GET /rules/{id}/executions). Enable/disable and edit report back
 * through onMutated/onEdit so the page refreshes.
 */

"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { Pencil } from "lucide-react";
import {
  ApiError,
  api,
  type RuleExecutionResponse,
  type RuleResponse,
} from "@/lib/api-client";
import { formatIsoDateTime } from "@/lib/format-date";
import { faNumber } from "@/lib/utils";
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Skeleton,
  StatusBadge,
} from "@/components/ui";
import { persianApiErrorMessage } from "@/features/shared/api-error";
import { RuleTestPanel } from "./dry-run-panel";
import {
  RULE_OWNER_LABELS,
  actionKindLabel,
  describeActionFa,
  describeConditionsFa,
  eventTypeLabel,
  executionDetailEntries,
  ruleDecisionLabel,
  ruleDecisionTone,
  ruleModeLabel,
  ruleOutcomeLabel,
  ruleOwner,
} from "./rules-data";

interface RuleDetailDialogProps {
  /** Null = closed. The rule comes from the page's list. */
  rule: RuleResponse | null;
  /** All rules (id → name) for naming live executions. */
  rulesById: Map<string, string>;
  onClose: () => void;
  onMutated: () => void;
  onEdit: (rule: RuleResponse) => void;
  onDelete: (rule: RuleResponse) => void;
}

export function RuleDetailDialog({
  rule,
  rulesById,
  onClose,
  onMutated,
  onEdit,
  onDelete,
}: RuleDetailDialogProps) {
  const router = useRouter();
  const [history, setHistory] = React.useState<{
    ruleId: string;
    executions: RuleExecutionResponse[];
  } | null>(null);
  const [historyError, setHistoryError] = React.useState<{ ruleId: string; message: string } | null>(null);
  const [historyKey, setHistoryKey] = React.useState(0);
  const [toggling, setToggling] = React.useState(false);
  const [actionError, setActionError] = React.useState<string | null>(null);

  const open = rule !== null;

  // Loading and error are derived during render (no setState inside
  // the effect): open with a rule whose history has not arrived (or
  // failed) yet. Both are scoped to the rule they belong to, so
  // opening another rule never shows a stale list or failure.
  const historyLoading =
    open &&
    rule !== null &&
    (history === null || history.ruleId !== rule.id) &&
    (historyError === null || historyError.ruleId !== rule.id);
  const historyExecutions =
    rule && history && history.ruleId === rule.id ? history.executions : [];
  const historyErrorMessage =
    rule && historyError && historyError.ruleId === rule.id ? historyError.message : null;

  React.useEffect(() => {
    if (!open || !rule) return;
    let cancelled = false;
    api
      .listRuleExecutions(rule.id, { limit: 50 })
      .then((data) => {
        if (!cancelled) {
          setHistory({ ruleId: rule.id, executions: data });
          setHistoryError(null);
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.replace("/login");
          return;
        }
        setHistoryError({
          ruleId: rule.id,
          message: persianApiErrorMessage(err, "دریافت تاریخچهٔ اجراها ناموفق بود."),
        });
      });
    return () => {
      cancelled = true;
    };
    // historyKey refreshes after a live run from this dialog.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, rule?.id, historyKey, router]);

  const toggleEnabled = async () => {
    if (!rule) return;
    setToggling(true);
    setActionError(null);
    try {
      if (rule.enabled) {
        await api.disableRule(rule.id);
      } else {
        await api.enableRule(rule.id);
      }
      onMutated();
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setActionError(persianApiErrorMessage(err, "تغییر وضعیت قانون ناموفق بود."));
    } finally {
      setToggling(false);
    }
  };

  const owner = rule ? ruleOwner(rule) : null;

  return (
    <Dialog
      open={open}
      onOpenChange={(isOpen) => {
        if (!isOpen) onClose();
      }}
    >
      <DialogContent
        className="max-h-[90vh] overflow-y-auto"
        data-testid="rule-detail-dialog"
        showCloseButton={false}
      >
        <DialogHeader>
          <DialogTitle>{rule ? rule.name : "جزئیات قانون"}</DialogTitle>
          <DialogDescription>
            {rule ? eventTypeLabel(rule.event_type) : "در حال دریافت جزئیات…"}
          </DialogDescription>
        </DialogHeader>

        {rule ? (
          <div className="space-y-6">
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge
                status={rule.enabled ? "active" : "inactive"}
                label={rule.enabled ? "فعال" : "غیرفعال"}
                tone={rule.enabled ? "success" : "outline"}
              />
              {owner ? (
                <StatusBadge
                  status={owner}
                  label={RULE_OWNER_LABELS[owner].label}
                  tone={RULE_OWNER_LABELS[owner].tone}
                />
              ) : null}
              {rule.installation_id === null ? (
                <StatusBadge status="tenant-wide" label="همهٔ نصب‌ها" tone="outline" />
              ) : null}
            </div>

            {rule.description ? <p className="text-sm">{rule.description}</p> : null}

            <dl className="grid gap-2 text-sm sm:grid-cols-2">
              <div className="flex gap-2">
                <dt className="text-muted-foreground">رویداد:</dt>
                <dd>{eventTypeLabel(rule.event_type)}</dd>
              </div>
              <div className="flex gap-2">
                <dt className="text-muted-foreground">اقدام:</dt>
                <dd>
                  {actionKindLabel(rule.action.kind)} — {describeActionFa(rule.action)}
                </dd>
              </div>
              <div className="flex gap-2 sm:col-span-2">
                <dt className="text-muted-foreground">شرط‌ها:</dt>
                <dd>{describeConditionsFa(rule.conditions)}</dd>
              </div>
              <div className="flex gap-2">
                <dt className="text-muted-foreground">اولویت:</dt>
                <dd>{faNumber(rule.priority)}</dd>
              </div>
              <div className="flex gap-2">
                <dt className="text-muted-foreground">ساخته‌شده:</dt>
                <dd>{formatIsoDateTime(rule.created_at)}</dd>
              </div>
              {rule.updated_at ? (
                <div className="flex gap-2">
                  <dt className="text-muted-foreground">آخرین تغییر:</dt>
                  <dd>{formatIsoDateTime(rule.updated_at)}</dd>
                </div>
              ) : null}
              {rule.created_by ? (
                <div className="flex gap-2">
                  <dt className="text-muted-foreground">ساخته‌شده توسط:</dt>
                  <dd dir="ltr">{rule.created_by}</dd>
                </div>
              ) : null}
            </dl>

            <div className="flex flex-wrap gap-2">
              <Button variant="outline" onClick={() => onEdit(rule)} data-testid="rule-detail-edit">
                <Pencil className="size-4" strokeWidth={2} aria-hidden />
                ویرایش
              </Button>
              <Button variant="outline" onClick={toggleEnabled} disabled={toggling} data-testid="rule-detail-toggle">
                {toggling ? "در حال تغییر…" : rule.enabled ? "غیرفعال کردن" : "فعال کردن"}
              </Button>
              <Button variant="outline" onClick={() => onDelete(rule)} data-testid="rule-detail-delete">
                حذف قانون
              </Button>
            </div>
            {actionError ? (
              <p className="text-sm text-destructive" role="alert">
                {actionError}
              </p>
            ) : null}

            <div className="space-y-4 rounded-lg border p-4">
              <h3 className="font-medium">آزمایش و اجرای زنده</h3>
              <RuleTestPanel
                eventType={rule.event_type}
                action={rule.action}
                runDryRun={(input) => api.dryRunRule(rule.id, input)}
                runLive={(input) => api.evaluateRules(input)}
                ruleId={rule.id}
                rulesById={rulesById}
                onLiveExecuted={() => setHistoryKey((k) => k + 1)}
                testIdPrefix="rule-detail"
              />
            </div>

            <div className="space-y-3">
              <h3 className="font-medium">تاریخچهٔ اجراها و ممیزی</h3>
              {historyLoading ? (
                <div className="space-y-2" aria-label="در حال بارگذاری">
                  <Skeleton className="h-16 w-full" />
                  <Skeleton className="h-16 w-full" />
                </div>
              ) : historyErrorMessage ? (
                <p className="text-sm text-destructive" role="alert">
                  {historyErrorMessage}
                </p>
              ) : historyExecutions.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  هنوز اجرایی برای این قانون ثبت نشده است؛ آزمایش یا اجرای زنده، اولین ردیف ممیزی را
                  می‌سازد.
                </p>
              ) : (
                <ol className="space-y-2">
                  {historyExecutions.map((execution) => (
                    <ExecutionRow key={execution.id} execution={execution} />
                  ))}
                </ol>
              )}
            </div>
          </div>
        ) : null}

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            بستن
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function ExecutionRow({ execution }: { execution: RuleExecutionResponse }) {
  const [expanded, setExpanded] = React.useState(false);
  const details = executionDetailEntries(execution);
  return (
    <li className="rounded-lg border p-3 text-sm" data-testid="rule-execution-row">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge
          status={execution.decision}
          label={ruleDecisionLabel(execution.decision)}
          tone={ruleDecisionTone(execution.decision)}
        />
        <StatusBadge
          status={execution.mode}
          label={ruleModeLabel(execution.mode)}
          tone={execution.mode === "live" ? "default" : "outline"}
        />
        <span className="text-muted-foreground">{ruleOutcomeLabel(execution.outcome)}</span>
        <span className="text-muted-foreground">{formatIsoDateTime(execution.executed_at)}</span>
        {execution.action_kind ? (
          <span className="text-muted-foreground">{actionKindLabel(execution.action_kind)}</span>
        ) : null}
      </div>
      {execution.event_id ? (
        <p className="mt-1 text-muted-foreground">
          رویداد: <span dir="ltr">{execution.event_id}</span>
        </p>
      ) : null}
      {execution.error ? (
        <p className="mt-1">
          خطا:{" "}
          <code dir="ltr" className="text-destructive">
            {execution.error}
          </code>
        </p>
      ) : null}
      {details.length > 0 ? (
        <div className="mt-2">
          <Button variant="link" className="h-auto min-h-11 p-0 text-sm" onClick={() => setExpanded((v) => !v)}>
            {expanded ? "پنهان‌کردن جزئیات" : "نمایش جزئیات"}
          </Button>
          {expanded ? (
            <dl className="mt-2 grid gap-2 sm:grid-cols-2">
              {details.map((entry) => (
                <div key={`${entry.label}-${entry.value}`} className="flex gap-2">
                  <dt className="text-muted-foreground">{entry.label}:</dt>
                  <dd dir={entry.ltr ? "ltr" : undefined} className={entry.ltr ? "text-start" : undefined}>
                    {entry.value}
                  </dd>
                </div>
              ))}
            </dl>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}
