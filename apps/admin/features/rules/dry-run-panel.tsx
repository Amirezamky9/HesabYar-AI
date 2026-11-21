/**
 * The rule test panel (US-562) — dry-run + live evaluate, shared by
 * the rule detail dialog (stored rule) and the rule form dialog
 * (unsaved definition, dry-run only).
 *
 * One input for both actions: a past inbox event (event_id — the
 * payload is loaded and normalized like the pipeline sees it) or a
 * sample payload built from a few fields (amount in تومان, channel,
 * party) plus optional extra JSON. The dry-run has zero side
 * effects; the live run REALLY executes every matching enabled rule
 * of the event type (per the rule set's match mode — not just this
 * rule), and the panel says so before running.
 *
 * The backend's English `would_do` is never rendered: the panel
 * describes the action in Persian from the rule's own action
 * (describeActionFa), and the gate verdict comes from the details'
 * booleans, never the English reason strings.
 */

"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { FlaskConical, Play } from "lucide-react";
import {
  ApiError,
  type DryRunRequest,
  type DryRunResponse,
  type EvaluateRequest,
  type EvaluateResponse,
  type RuleAction,
} from "@/lib/api-client";
import { formatIsoDateTime } from "@/lib/format-date";
import { faNumber } from "@/lib/utils";
import {
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Input,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  StatusBadge,
  Textarea,
} from "@/components/ui";
import { persianApiErrorMessage } from "@/features/shared/api-error";
import {
  describeActionFa,
  emptyEventTestInput,
  eventTestInputToRequest,
  eventTypeLabel,
  gateVerdict,
  ruleDecisionLabel,
  ruleDecisionTone,
  ruleOutcomeLabel,
  type EventTestInput,
} from "./rules-data";

interface RuleTestPanelProps {
  /** The rule's event type — the sample payload is evaluated as this type. */
  eventType: string;
  /** The action, for the Persian would-do description (null while the form is invalid). */
  action: RuleAction | null;
  /** How the dry-run executes: stored rule (dryRunRule) or definition (dryRunDefinition). */
  runDryRun: (input: DryRunRequest) => Promise<DryRunResponse>;
  /** Live evaluate (stored rules only — absent for an unsaved definition). */
  runLive?: (input: EvaluateRequest) => Promise<EvaluateResponse>;
  /** This rule's id, for the "this rule did not run" note on a live result. */
  ruleId?: string | null;
  /** Rule id → name, for naming live executions. */
  rulesById?: Map<string, string>;
  /** Called after a live run so the history refreshes. */
  onLiveExecuted?: () => void;
  testIdPrefix: string;
}

/** A plain Error thrown by the parent carries a Persian validation message; ApiErrors map as usual. */
function testErrorMessage(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return persianApiErrorMessage(err, fallback);
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

export function RuleTestPanel({
  eventType,
  action,
  runDryRun,
  runLive,
  ruleId,
  rulesById,
  onLiveExecuted,
  testIdPrefix,
}: RuleTestPanelProps) {
  const router = useRouter();
  const [input, setInput] = React.useState<EventTestInput>(emptyEventTestInput);
  const [dryRun, setDryRun] = React.useState<DryRunResponse | null>(null);
  const [live, setLive] = React.useState<EvaluateResponse | null>(null);
  const [running, setRunning] = React.useState<"dry" | "live" | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const setField = <K extends keyof EventTestInput>(key: K, value: EventTestInput[K]) => {
    setInput((prev) => ({ ...prev, [key]: value }));
  };

  const resolveInput = () => {
    const resolved = eventTestInputToRequest(input, eventType);
    if ("error" in resolved) {
      setError(resolved.error);
      return null;
    }
    return resolved.request;
  };

  const handleUnauthorized = (err: unknown): boolean => {
    if (err instanceof ApiError && err.isUnauthorized) {
      router.replace("/login");
      return true;
    }
    return false;
  };

  const doDryRun = async () => {
    const request = resolveInput();
    if (!request) return;
    setRunning("dry");
    setError(null);
    setLive(null);
    try {
      const result = await runDryRun(request);
      setDryRun(result);
    } catch (err: unknown) {
      if (!handleUnauthorized(err)) setError(testErrorMessage(err, "اجرای آزمایشی ناموفق بود."));
    } finally {
      setRunning(null);
    }
  };

  const doLive = async () => {
    if (!runLive) return;
    const request = resolveInput();
    if (!request) return;
    setRunning("live");
    setError(null);
    setDryRun(null);
    try {
      const result = await runLive(request);
      setLive(result);
      onLiveExecuted?.();
    } catch (err: unknown) {
      if (!handleUnauthorized(err)) setError(testErrorMessage(err, "اجرای زنده ناموفق بود."));
    } finally {
      setRunning(null);
    }
  };

  const gate = dryRun ? gateVerdict(dryRun.details) : null;
  const thisRuleRanLive =
    live && ruleId ? live.executions.some((execution) => execution.rule_id === ruleId) : null;

  return (
    <div className="space-y-4" data-testid={`${testIdPrefix}-test-panel`}>
      <div className="space-y-2">
        <Label htmlFor={`${testIdPrefix}-mode`}>رویداد آزمون</Label>
        <Select value={input.mode} onValueChange={(v) => v && setField("mode", v as "sample" | "past")}>
          <SelectTrigger id={`${testIdPrefix}-mode`} data-testid={`${testIdPrefix}-mode`}>
            <SelectValue>{input.mode === "sample" ? "نمونهٔ آزمایشی" : "رویداد گذشته"}</SelectValue>
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="sample">نمونهٔ آزمایشی</SelectItem>
            <SelectItem value="past">رویداد گذشته</SelectItem>
          </SelectContent>
        </Select>
        <p className="text-sm text-muted-foreground">
          {input.mode === "sample"
            ? `نمونه با رویداد «${eventTypeLabel(eventType)}» ارزیابی می‌شود.`
            : "پیلود رویداد گذشته دقیقاً مثل زمانی که خط لوله آن را دیده است بارگذاری و نرمال می‌شود."}
        </p>
      </div>

      {input.mode === "sample" ? (
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-2">
            <Label htmlFor={`${testIdPrefix}-amount`}>مبلغ نمونه (تومان)</Label>
            <Input
              id={`${testIdPrefix}-amount`}
              data-testid={`${testIdPrefix}-amount`}
              inputMode="numeric"
              value={input.amountToman}
              onChange={(e) => setField("amountToman", e.target.value)}
              placeholder="مثلاً ۱٬۵۰۰٬۰۰۰"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor={`${testIdPrefix}-channel`}>کانال نمونه</Label>
            <Input
              id={`${testIdPrefix}-channel`}
              data-testid={`${testIdPrefix}-channel`}
              value={input.channel}
              onChange={(e) => setField("channel", e.target.value)}
              placeholder="مثلاً zarinpal"
              dir="ltr"
              className="text-start"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor={`${testIdPrefix}-party`}>شناسهٔ طرف نمونه</Label>
            <Input
              id={`${testIdPrefix}-party`}
              data-testid={`${testIdPrefix}-party`}
              value={input.partyId}
              onChange={(e) => setField("partyId", e.target.value)}
              placeholder="مثلاً cust-1"
              dir="ltr"
              className="text-start"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor={`${testIdPrefix}-extra-json`}>پیلود اضافی (JSON، اختیاری)</Label>
            <Textarea
              id={`${testIdPrefix}-extra-json`}
              data-testid={`${testIdPrefix}-extra-json`}
              value={input.extraJson}
              onChange={(e) => setField("extraJson", e.target.value)}
              placeholder='{"total_amount": 15000000}'
              dir="ltr"
              className="text-start"
            />
          </div>
        </div>
      ) : (
        <div className="space-y-2">
          <Label htmlFor={`${testIdPrefix}-event-id`}>شناسهٔ رویداد گذشته</Label>
          <Input
            id={`${testIdPrefix}-event-id`}
            data-testid={`${testIdPrefix}-event-id`}
            value={input.eventId}
            onChange={(e) => setField("eventId", e.target.value)}
            placeholder="شناسهٔ رویداد در صندوق ورودی"
            dir="ltr"
            className="text-start"
          />
        </div>
      )}

      <div className="flex flex-wrap gap-2">
        <Button onClick={doDryRun} disabled={running !== null} data-testid={`${testIdPrefix}-run-dry`}>
          <FlaskConical className="size-4" strokeWidth={2} aria-hidden />
          {running === "dry" ? "در حال آزمایش…" : "اجرای آزمایشی"}
        </Button>
        {runLive ? (
          <Button
            variant="outline"
            onClick={doLive}
            disabled={running !== null}
            data-testid={`${testIdPrefix}-run-live`}
          >
            <Play className="size-4" strokeWidth={2} aria-hidden />
            {running === "live" ? "در حال اجرا…" : "اجرای زنده روی نمونه"}
          </Button>
        ) : null}
      </div>
      <p className="text-sm text-muted-foreground">
        اجرای آزمایشی هیچ اثر جانبی ندارد؛ هیچ پیش‌نویس، اطلاع‌رسانی یا تغییری انجام نمی‌شود.
        {runLive
          ? " اجرای زنده واقعاً اقدام‌ها را انجام می‌دهد و همهٔ قوانین فعال این نوع رویداد را — طبق حالت مجموعه — اجرا می‌کند، نه فقط همین قانون را؛ ثبت پیش‌نویس همچنان از درگاه سیاستی می‌گذرد."
          : null}
      </p>

      {error ? (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      ) : null}

      {dryRun ? (
        <Card data-testid={`${testIdPrefix}-dry-result`}>
          <CardHeader>
            <div className="flex flex-wrap items-center gap-2">
              <CardTitle>نتیجهٔ آزمایش</CardTitle>
              <StatusBadge
                status={dryRun.decision}
                label={ruleDecisionLabel(dryRun.decision)}
                tone={ruleDecisionTone(dryRun.decision)}
              />
            </div>
          </CardHeader>
          <CardContent className="space-y-2">
            {dryRun.matched ? (
              <p className="text-sm">
                این قانون روی این رویداد اجرا می‌شد
                {action ? ` — ${describeActionFa(action)}.` : "."}
              </p>
            ) : (
              <p className="text-sm">
                این قانون روی این رویداد اجرا نمی‌شد؛ شرط‌ها برقرار نبودند
                {dryRun.event_type !== eventType
                  ? ` (رویداد ارزیابی‌شده «${eventTypeLabel(dryRun.event_type)}» بود، نه «${eventTypeLabel(eventType)}»).`
                  : "."}
              </p>
            )}
            {gate && gate.evaluated ? (
              <p className="text-sm">
                درگاه سیاستی ارزیابی شد:{" "}
                {gate.wouldPost === true
                  ? "ثبت پیش‌نویس مجاز بود."
                  : gate.wouldPost === false
                    ? "ثبت پیش‌نویس مجاز نبود؛ پیش‌نویس می‌ماند."
                    : "نتیجهٔ ثبت مشخص نشد."}
              </p>
            ) : action?.kind === "post_if_policy_allows" ? (
              <p className="text-sm text-muted-foreground">
                درگاه سیاستی ارزیابی نشد؛ بدون درگاه، ثبت به‌صورت بسته انجام می‌شد و پیش‌نویس می‌ماند.
              </p>
            ) : null}
            <p className="text-sm text-muted-foreground">
              نتیجه: {ruleOutcomeLabel(dryRun.outcome)} — این آزمایش در ممیزی ثبت شد؛ شناسهٔ اجرا{" "}
              <span dir="ltr">{dryRun.execution_id}</span>
            </p>
          </CardContent>
        </Card>
      ) : null}

      {live ? (
        <Card data-testid={`${testIdPrefix}-live-result`}>
          <CardHeader>
            <CardTitle>نتیجهٔ اجرای زنده</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <p className="text-sm">
              {live.matched_rules === 0
                ? "هیچ قانونی روی این رویداد تطبیق نکرد و چیزی اجرا نشد."
                : `${faNumber(live.matched_rules)} قانون تطبیق کرد و اجرا شد.`}
            </p>
            {thisRuleRanLive === false ? (
              <p className="text-sm text-muted-foreground">
                این قانون در این اجرا فعال نشد؛ شاید قانون دیگری با اولویت بالاتر اول تطبیق کرد (حالت
                «اولین تطبیق») یا شرط‌هایش برقرار نبود.
              </p>
            ) : null}
            {live.executions.length > 0 ? (
              <ul className="space-y-2">
                {live.executions.map((execution) => (
                  <li key={execution.id} className="rounded-lg border p-3 text-sm">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">
                        {execution.rule_id && rulesById?.get(execution.rule_id)
                          ? rulesById.get(execution.rule_id)
                          : "قانون نامشخص"}
                      </span>
                      <StatusBadge
                        status={execution.decision}
                        label={ruleDecisionLabel(execution.decision)}
                        tone={ruleDecisionTone(execution.decision)}
                      />
                      <span className="text-muted-foreground">{ruleOutcomeLabel(execution.outcome)}</span>
                      <span className="text-muted-foreground">{formatIsoDateTime(execution.executed_at)}</span>
                    </div>
                  </li>
                ))}
              </ul>
            ) : null}
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
