/**
 * Operator monitoring panel (US-704) — under تنظیمات ← پایش سیستم
 * (spec §18 module 13: "system status").
 *
 * Three tabs:
 *
 * - «نمای کلی»: the monitoring snapshot — process health (API,
 *   database, Tryton, worker, MCP), queue depths + lag + 5xx rate +
 *   drift, recent alerts (acknowledge / resolve), token expiries,
 *   and backup status (honestly «نامشخص» until US-703 lands).
 *   «ارزیابی اکنون» runs the evaluation in the API process — the
 *   only way the 5xx rule fires (its counters live there).
 * - «کانال‌های هشدار»: SMS / email / webhook channels (create /
 *   edit / delete). SMS providers come from the registry; nothing
 *   is hard-coded.
 * - «توکن‌ها»: configured token records (the Fintech token's
 *   expiry, until the live token lands) — create / edit / delete.
 *
 * The dashboard's AlertsWidget is the starting point for the alert
 * rows (severity, empty state); the dashboard itself is unchanged.
 */

"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { Check, Pencil, Plus, RefreshCw, Trash2, TriangleAlert } from "lucide-react";
import {
  ApiError,
  api,
  type MonitoringAlertResponse,
  type MonitoringChannelResponse,
  type MonitoringOverviewResponse,
  type MonitoringTokenRecordResponse,
} from "@/lib/api-client";
import { formatIsoDateTime } from "@/lib/format-date";
import { formatIsoJalali } from "@/lib/jalali";
import { fa, faNumber } from "@/lib/utils";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
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
  Skeleton,
  StatusBadge,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import { isPermissionDenied, persianApiErrorMessage } from "@/features/shared/api-error";
import {
  alertCountBySeverity,
  alertRuleLabelFa,
  alertSeverityLabelFa,
  alertStatusLabelFa,
  backupDatabaseLabelFa,
  backupStatusLabelFa,
  channelKindLabelFa,
  formatDaysLeft,
  formatPercent,
  formatRial,
  processLabelFa,
  processStatusLabelFa,
  tokenKindLabelFa,
  worstProcessStatus,
} from "./monitoring-data";
import { MonitoringChannelDialog } from "./monitoring-channel-dialog";
import { MonitoringTokenDialog } from "./monitoring-token-dialog";

type TabId = "overview" | "channels" | "tokens";

const ALERTS_LIMIT = 20;

function severityTone(severity: string): "destructive" | "warning" | "secondary" {
  if (severity === "critical") return "destructive";
  if (severity === "warning") return "warning";
  return "secondary";
}

function processTone(status: string): "success" | "warning" | "destructive" | "outline" {
  if (status === "ok") return "success";
  if (status === "degraded") return "warning";
  if (status === "error") return "destructive";
  return "outline";
}

function backupTone(status: string): "success" | "warning" | "destructive" | "outline" {
  if (status === "ok") return "success";
  if (status === "stale") return "warning";
  if (status === "failed") return "destructive";
  return "outline";
}

export function MonitoringPanel() {
  const router = useRouter();
  const [tab, setTab] = React.useState<TabId>("overview");
  const [reloadKey, setReloadKey] = React.useState(0);

  const [overview, setOverview] = React.useState<MonitoringOverviewResponse | null>(null);
  const [alerts, setAlerts] = React.useState<MonitoringAlertResponse[]>([]);
  const [overviewLoading, setOverviewLoading] = React.useState(true);
  const [overviewError, setOverviewError] = React.useState<unknown>(null);

  const [channels, setChannels] = React.useState<MonitoringChannelResponse[]>([]);
  const [channelsLoading, setChannelsLoading] = React.useState(false);
  const [channelsError, setChannelsError] = React.useState<unknown>(null);
  const [channelsLoaded, setChannelsLoaded] = React.useState(false);

  const [tokens, setTokens] = React.useState<MonitoringTokenRecordResponse[]>([]);
  const [tokensLoading, setTokensLoading] = React.useState(false);
  const [tokensError, setTokensError] = React.useState<unknown>(null);
  const [tokensLoaded, setTokensLoaded] = React.useState(false);

  const [evaluating, setEvaluating] = React.useState(false);
  const [evaluateNote, setEvaluateNote] = React.useState<string | null>(null);
  const [actingId, setActingId] = React.useState<string | null>(null);
  const [actionError, setActionError] = React.useState<string | null>(null);

  const [channelDialog, setChannelDialog] = React.useState<{
    open: boolean;
    channel: MonitoringChannelResponse | null;
  }>({ open: false, channel: null });
  const [channelDeleteTarget, setChannelDeleteTarget] =
    React.useState<MonitoringChannelResponse | null>(null);
  const [tokenDialog, setTokenDialog] = React.useState<{
    open: boolean;
    record: MonitoringTokenRecordResponse | null;
  }>({ open: false, record: null });
  const [tokenDeleteTarget, setTokenDeleteTarget] =
    React.useState<MonitoringTokenRecordResponse | null>(null);

  const refreshOverview = React.useCallback(() => {
    setOverviewLoading(true);
    setOverviewError(null);
    setReloadKey((k) => k + 1);
  }, []);

  React.useEffect(() => {
    if (tab !== "overview") return;
    let cancelled = false;
    Promise.all([
      api.getMonitoringOverview(),
      api.listMonitoringAlerts({ limit: ALERTS_LIMIT }),
    ])
      .then(([snapshot, recent]) => {
        if (cancelled) return;
        setOverview(snapshot);
        setAlerts(recent);
        setOverviewLoading(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.replace("/login");
          return;
        }
        setOverviewError(err);
        setOverviewLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [tab, reloadKey, router]);

  React.useEffect(() => {
    if (tab !== "channels" || channelsLoaded) return;
    let cancelled = false;
    api
      .listMonitoringChannels()
      .then((data) => {
        if (cancelled) return;
        setChannels(data);
        setChannelsLoaded(true);
        setChannelsLoading(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.replace("/login");
          return;
        }
        setChannelsError(err);
        setChannelsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [tab, channelsLoaded, router]);

  React.useEffect(() => {
    if (tab !== "tokens" || tokensLoaded) return;
    let cancelled = false;
    api
      .listMonitoringTokens()
      .then((data) => {
        if (cancelled) return;
        setTokens(data);
        setTokensLoaded(true);
        setTokensLoading(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.replace("/login");
          return;
        }
        setTokensError(err);
        setTokensLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [tab, tokensLoaded, router]);

  const loadChannels = async () => {
    setChannelsLoading(true);
    setChannelsError(null);
    try {
      setChannels(await api.listMonitoringChannels());
      setChannelsLoaded(true);
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setChannelsError(err);
    } finally {
      setChannelsLoading(false);
    }
  };

  const loadTokens = async () => {
    setTokensLoading(true);
    setTokensError(null);
    try {
      setTokens(await api.listMonitoringTokens());
      setTokensLoaded(true);
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setTokensError(err);
    } finally {
      setTokensLoading(false);
    }
  };

  const handleEvaluate = async () => {
    setEvaluating(true);
    setEvaluateNote(null);
    setActionError(null);
    try {
      const report = await api.evaluateMonitoring();
      setEvaluateNote(
        report.fired.length > 0
          ? `${fa(report.fired.length)} هشدار جدید ثبت شد.`
          : report.already_open > 0
            ? `هشدار جدیدی نبود؛ ${fa(report.already_open)} هشدار همچنان باز است.`
            : "هشدار جدیدی نبود.",
      );
      refreshOverview();
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setActionError(persianApiErrorMessage(err, "اجرای ارزیابی ناموفق بود."));
    } finally {
      setEvaluating(false);
    }
  };

  const handleAlertAction = async (alert: MonitoringAlertResponse, action: "ack" | "resolve") => {
    setActingId(alert.id);
    setActionError(null);
    try {
      const updated =
        action === "ack"
          ? await api.acknowledgeMonitoringAlert(alert.id)
          : await api.resolveMonitoringAlert(alert.id);
      setAlerts((prev) => prev.map((a) => (a.id === updated.id ? updated : a)));
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setActionError(persianApiErrorMessage(err, "این اقدام ناموفق بود."));
    } finally {
      setActingId(null);
    }
  };

  const handleDeleteChannel = async () => {
    if (!channelDeleteTarget) return;
    setActingId(channelDeleteTarget.id);
    try {
      await api.deleteMonitoringChannel(channelDeleteTarget.id);
      setChannelDeleteTarget(null);
      await loadChannels();
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setActionError(persianApiErrorMessage(err, "حذف کانال ناموفق بود."));
    } finally {
      setActingId(null);
    }
  };

  const handleDeleteToken = async () => {
    if (!tokenDeleteTarget) return;
    setActingId(tokenDeleteTarget.id);
    try {
      await api.deleteMonitoringToken(tokenDeleteTarget.id);
      setTokenDeleteTarget(null);
      await loadTokens();
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setActionError(persianApiErrorMessage(err, "حذف توکن ناموفق بود."));
    } finally {
      setActingId(null);
    }
  };

  const counts = alertCountBySeverity(alerts);
  const worst = overview ? worstProcessStatus(overview.processes) : "unknown";

  return (
    <div data-testid="monitoring-overview" className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">پایش سیستم</h1>
          <p className="text-sm text-muted-foreground">
            سلامت فرایندها، صف‌ها، هشدارها، انقضای توکن‌ها و وضعیت پشتیبان‌گیری
          </p>
        </div>
        {tab === "overview" ? (
          <Button onClick={handleEvaluate} disabled={evaluating} className="min-h-11">
            <RefreshCw className="size-4" aria-hidden />
            {evaluating ? "در حال ارزیابی…" : "ارزیابی اکنون"}
          </Button>
        ) : null}
      </div>

      {evaluateNote ? (
        <p role="status" className="text-sm text-muted-foreground">
          {evaluateNote}
        </p>
      ) : null}
      {actionError ? (
        <p role="alert" className="text-sm text-destructive">
          {actionError}
        </p>
      ) : null}

      <Tabs
        value={tab}
        onValueChange={(value) => {
          const next = value as TabId;
          setTab(next);
          // The loading flag is raised here (an event handler), never
          // synchronously in the lazy-load effects below (the
          // mcp-clients pattern — react-hooks/set-state-in-effect).
          if (next === "channels" && !channelsLoaded) {
            setChannelsLoading(true);
            setChannelsError(null);
          }
          if (next === "tokens" && !tokensLoaded) {
            setTokensLoading(true);
            setTokensError(null);
          }
        }}
        className="space-y-6"
      >
        <TabsList>
          <TabsTrigger value="overview" className="min-h-11">
            نمای کلی
          </TabsTrigger>
          <TabsTrigger value="channels" className="min-h-11">
            کانال‌های هشدار
          </TabsTrigger>
          <TabsTrigger value="tokens" className="min-h-11">
            توکن‌ها
          </TabsTrigger>
        </TabsList>

        <TabsContent value="overview" className="space-y-6">
          {overviewLoading ? (
            <div className="space-y-4" aria-label="در حال بارگذاری">
              <Skeleton className="h-40 w-full" />
              <Skeleton className="h-40 w-full" />
              <Skeleton className="h-40 w-full" />
            </div>
          ) : overviewError ? (
            isPermissionDenied(overviewError) ? (
              <PermissionDeniedState
                message={persianApiErrorMessage(
                  overviewError,
                  "برای مشاهدهٔ پایش سیستم دسترسی ندارید.",
                )}
              />
            ) : (
              <ErrorState
                message={persianApiErrorMessage(
                  overviewError,
                  "بارگذاری پایش سیستم ناموفق بود.",
                )}
                onRetry={refreshOverview}
              />
            )
          ) : overview ? (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm text-muted-foreground">وضعیت کلی:</span>
                <StatusBadge
                  status={worst}
                  label={processStatusLabelFa(worst)}
                  tone={processTone(worst)}
                />
                {counts.critical > 0 ? (
                  <Badge variant="destructive">{fa(counts.critical)} هشدار بحرانی باز</Badge>
                ) : null}
                {counts.warning > 0 ? (
                  <Badge variant="secondary">{fa(counts.warning)} هشدار باز</Badge>
                ) : null}
                <span className="text-sm text-muted-foreground">
                  آخرین به‌روزرسانی: {formatIsoDateTime(overview.generated_at)}
                </span>
              </div>

              <div className="grid gap-6 lg:grid-cols-2">
                <Card data-testid="monitoring-processes">
                  <CardHeader>
                    <CardTitle>سلامت فرایندها</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <ul className="space-y-1">
                      {overview.processes.map((process) => (
                        <li
                          key={process.name}
                          className="flex min-h-11 items-center justify-between gap-4 rounded-lg px-2 py-1"
                        >
                          <span className="text-sm font-medium">
                            {processLabelFa(process.name)}
                          </span>
                          <span className="flex items-center gap-2">
                            {process.detail ? (
                              <span className="max-w-48 truncate text-xs text-muted-foreground">
                                {process.detail}
                              </span>
                            ) : null}
                            <StatusBadge
                              status={process.status}
                              label={processStatusLabelFa(process.status)}
                              tone={processTone(process.status)}
                            />
                          </span>
                        </li>
                      ))}
                    </ul>
                  </CardContent>
                </Card>

                <Card data-testid="monitoring-queues">
                  <CardHeader>
                    <CardTitle>صف‌ها و تأخیر</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <dl className="space-y-1 text-sm">
                      <div className="flex min-h-11 items-center justify-between gap-4 rounded-lg px-2 py-1">
                        <dt className="text-muted-foreground">رویدادهای در انتظار صندوق ورودی</dt>
                        <dd className="font-medium">{fa(overview.queue_depths.inbox_received)}</dd>
                      </div>
                      <div className="flex min-h-11 items-center justify-between gap-4 rounded-lg px-2 py-1">
                        <dt className="text-muted-foreground">رویدادهای در حال پردازش</dt>
                        <dd className="font-medium">{fa(overview.queue_depths.inbox_processing)}</dd>
                      </div>
                      <div className="flex min-h-11 items-center justify-between gap-4 rounded-lg px-2 py-1">
                        <dt className="text-muted-foreground">رویدادهای ناموفق</dt>
                        <dd className="font-medium">{fa(overview.queue_depths.inbox_failed)}</dd>
                      </div>
                      <div className="flex min-h-11 items-center justify-between gap-4 rounded-lg px-2 py-1">
                        <dt className="text-muted-foreground">رویدادهای گیرکرده (بیش از آستانه)</dt>
                        <dd className="font-medium">{fa(overview.stale_inbox_count)}</dd>
                      </div>
                      <div className="flex min-h-11 items-center justify-between gap-4 rounded-lg px-2 py-1">
                        <dt className="text-muted-foreground">خروجی در انتظار ارسال</dt>
                        <dd className="font-medium">{fa(overview.queue_depths.outbox_pending)}</dd>
                      </div>
                      <div className="flex min-h-11 items-center justify-between gap-4 rounded-lg px-2 py-1">
                        <dt className="text-muted-foreground">تأخیر کارگر</dt>
                        <dd className="font-medium">
                          {overview.worker_lag_minutes === null
                            ? "هنوز اجرا نشده است"
                            : `${fa(overview.worker_lag_minutes)} دقیقه`}
                        </dd>
                      </div>
                      <div className="flex min-h-11 items-center justify-between gap-4 rounded-lg px-2 py-1">
                        <dt className="text-muted-foreground">وب‌هوک‌های ناموفق (۲۴ ساعت)</dt>
                        <dd className="font-medium">{fa(overview.failed_webhook_attempts_24h)}</dd>
                      </div>
                      <div className="flex min-h-11 items-center justify-between gap-4 rounded-lg px-2 py-1">
                        <dt className="text-muted-foreground">مغایرت‌های باز تسویه</dt>
                        <dd className="font-medium">
                          {overview.open_drift_exceptions === 0
                            ? "۰"
                            : `${fa(overview.open_drift_exceptions)} مغایرت، مجموع ${formatRial(overview.drift_total_irr)}`}
                        </dd>
                      </div>
                      <div className="flex min-h-11 items-center justify-between gap-4 rounded-lg px-2 py-1">
                        <dt className="text-muted-foreground">نرخ خطای ۵xx (این فرایند)</dt>
                        <dd className="font-medium">
                          {overview.error_rate_5xx === null
                            ? "داده‌ای نیست"
                            : `${formatPercent(overview.error_rate_5xx)} از ${faNumber(overview.requests_total)} درخواست`}
                        </dd>
                      </div>
                      {overview.queue_depths.procrastinate_todo === null ? (
                        <div className="flex min-h-11 items-center justify-between gap-4 rounded-lg px-2 py-1">
                          <dt className="text-muted-foreground">کارهای در انتظار صف‌ها</dt>
                          <dd className="font-medium">نامشخص (کارگر هنوز اجرا نشده است)</dd>
                        </div>
                      ) : (
                        Object.entries(overview.queue_depths.procrastinate_todo).map(
                          ([queue, count]) => (
                            <div
                              key={queue}
                              className="flex min-h-11 items-center justify-between gap-4 rounded-lg px-2 py-1"
                            >
                              <dt className="text-muted-foreground">
                                کارهای در انتظار صف <span dir="ltr">{queue}</span>
                              </dt>
                              <dd className="font-medium">{fa(count)}</dd>
                            </div>
                          ),
                        )
                      )}
                    </dl>
                  </CardContent>
                </Card>
              </div>

              <Card data-testid="monitoring-alerts">
                <CardHeader>
                  <CardTitle>هشدارهای اخیر</CardTitle>
                </CardHeader>
                <CardContent>
                  {alerts.length === 0 ? (
                    <p className="text-sm text-muted-foreground">
                      هشداری نیست؛ همه‌چیز در وضعیت عادی است.
                    </p>
                  ) : (
                    <ul className="space-y-1">
                      {alerts.map((alert) => (
                        <li
                          key={alert.id}
                          className="space-y-2 rounded-lg px-2 py-2 hover:bg-muted/50"
                        >
                          <div className="flex min-h-11 items-start gap-2">
                            <TriangleAlert
                              className={`mt-1 size-4 shrink-0 ${
                                alert.severity === "critical"
                                  ? "text-destructive"
                                  : "text-warning-foreground"
                              }`}
                              strokeWidth={2}
                              aria-hidden
                            />
                            <span className="min-w-0 flex-1">
                              <span
                                className={`block text-sm font-medium ${
                                  alert.severity === "critical" ? "text-destructive" : ""
                                }`}
                              >
                                {alert.title_fa}
                              </span>
                              <span className="block text-xs text-muted-foreground">
                                {alert.message_fa}
                              </span>
                              <span className="mt-1 flex flex-wrap items-center gap-2">
                                <Badge variant="outline">{alertRuleLabelFa(alert.rule)}</Badge>
                                <StatusBadge
                                  status={alert.severity}
                                  label={alertSeverityLabelFa(alert.severity)}
                                  tone={severityTone(alert.severity)}
                                />
                                <StatusBadge
                                  status={alert.status}
                                  label={alertStatusLabelFa(alert.status)}
                                  tone={alert.status === "resolved" ? "success" : "outline"}
                                />
                                {alert.fired_at ? (
                                  <span className="text-xs text-muted-foreground">
                                    {formatIsoDateTime(alert.fired_at)}
                                  </span>
                                ) : null}
                              </span>
                              {alert.runbook_ref ? (
                                <span className="block text-xs text-muted-foreground">
                                  {alert.runbook_ref}
                                </span>
                              ) : null}
                            </span>
                          </div>
                          {alert.status !== "resolved" ? (
                            <div className="flex flex-wrap gap-2 ps-6">
                              {alert.status === "open" ? (
                                <Button
                                  variant="outline"
                                  size="sm"
                                  className="min-h-11"
                                  disabled={actingId === alert.id}
                                  onClick={() => handleAlertAction(alert, "ack")}
                                >
                                  <Check className="size-4" aria-hidden />
                                  تأیید
                                </Button>
                              ) : null}
                              <Button
                                variant="outline"
                                size="sm"
                                className="min-h-11"
                                disabled={actingId === alert.id}
                                onClick={() => handleAlertAction(alert, "resolve")}
                              >
                                حل‌شده
                              </Button>
                            </div>
                          ) : null}
                        </li>
                      ))}
                    </ul>
                  )}
                </CardContent>
              </Card>

              <div className="grid gap-6 lg:grid-cols-2">
                <Card data-testid="monitoring-tokens">
                  <CardHeader>
                    <CardTitle>انقضای توکن‌ها</CardTitle>
                  </CardHeader>
                  <CardContent>
                    {overview.token_expiries.length === 0 ? (
                      <p className="text-sm text-muted-foreground">
                        توکنی با تاریخ انقضا ثبت نشده است.
                      </p>
                    ) : (
                      <ul className="space-y-1">
                        {overview.token_expiries.map((expiry) => (
                          <li
                            key={`${expiry.source}-${expiry.record_id ?? expiry.label}`}
                            className="flex min-h-11 items-center justify-between gap-4 rounded-lg px-2 py-1"
                          >
                            <span className="min-w-0">
                              <span className="block truncate text-sm font-medium">
                                {expiry.label}
                              </span>
                              <span className="block text-xs text-muted-foreground">
                                {tokenKindLabelFa(expiry.kind)} — انقضا{" "}
                                {formatIsoJalali(expiry.expires_at.slice(0, 10))}
                              </span>
                            </span>
                            {expiry.severity ? (
                              <StatusBadge
                                status={expiry.severity}
                                label={`${alertSeverityLabelFa(expiry.severity)} — ${formatDaysLeft(expiry.days_left)}`}
                                tone={severityTone(expiry.severity)}
                              />
                            ) : (
                              <span className="shrink-0 text-sm text-muted-foreground">
                                {formatDaysLeft(expiry.days_left)}
                              </span>
                            )}
                          </li>
                        ))}
                      </ul>
                    )}
                  </CardContent>
                </Card>

                <Card data-testid="monitoring-backups">
                  <CardHeader>
                    <CardTitle>وضعیت پشتیبان‌گیری</CardTitle>
                  </CardHeader>
                  <CardContent>
                    {overview.backups.length === 0 ? (
                      <p className="text-sm text-muted-foreground">
                        اطلاعاتی از پشتیبان‌گیری در دسترس نیست.
                      </p>
                    ) : (
                      <ul className="space-y-1">
                        {overview.backups.map((backup) => (
                          <li
                            key={backup.database}
                            className="flex min-h-11 items-center justify-between gap-4 rounded-lg px-2 py-1"
                          >
                            <span className="min-w-0">
                              <span className="block text-sm font-medium">
                                {backupDatabaseLabelFa(backup.database)}
                              </span>
                              <span className="block text-xs text-muted-foreground">
                                {backup.last_success_at
                                  ? `آخرین پشتیبان موفق: ${formatIsoDateTime(backup.last_success_at)}`
                                  : backup.status === "unknown"
                                    ? "هنوز پشتیبان‌گیری‌ای ثبت نشده است"
                                    : "پشتیبان موفقی ثبت نشده است"}
                              </span>
                              {backup.error ? (
                                <span className="block text-xs text-destructive">
                                  {backup.error}
                                </span>
                              ) : null}
                            </span>
                            <StatusBadge
                              status={backup.status}
                              label={backupStatusLabelFa(backup.status)}
                              tone={backupTone(backup.status)}
                            />
                          </li>
                        ))}
                      </ul>
                    )}
                  </CardContent>
                </Card>
              </div>
            </>
          ) : null}
        </TabsContent>

        <TabsContent value="channels" className="space-y-6">
          <div className="flex justify-end">
            <Button
              onClick={() => setChannelDialog({ open: true, channel: null })}
              className="min-h-11"
            >
              <Plus className="size-4" aria-hidden />
              کانال جدید
            </Button>
          </div>
          {channelsLoading ? (
            <div className="space-y-4" aria-label="در حال بارگذاری">
              <Skeleton className="h-24 w-full" />
              <Skeleton className="h-24 w-full" />
            </div>
          ) : channelsError ? (
            isPermissionDenied(channelsError) ? (
              <PermissionDeniedState
                message={persianApiErrorMessage(
                  channelsError,
                  "برای مشاهدهٔ کانال‌های هشدار دسترسی ندارید.",
                )}
              />
            ) : (
              <ErrorState
                message={persianApiErrorMessage(channelsError, "بارگذاری کانال‌ها ناموفق بود.")}
                onRetry={loadChannels}
              />
            )
          ) : channels.length === 0 ? (
            <Card>
              <CardContent className="py-6">
                <p className="text-sm text-muted-foreground">
                  کانالی تعریف نشده است؛ هشدارها فقط در همین صفحه دیده می‌شوند.
                </p>
              </CardContent>
            </Card>
          ) : (
            <Card>
              <CardContent>
                <ul className="space-y-1">
                  {channels.map((channel) => (
                    <li
                      key={channel.id}
                      className="flex min-h-11 flex-wrap items-center justify-between gap-4 rounded-lg px-2 py-2"
                    >
                      <span className="min-w-0">
                        <span className="block text-sm font-medium">{channel.name}</span>
                        <span className="block text-xs text-muted-foreground">
                          {channelKindLabelFa(channel.kind)} —{" "}
                          <span dir="ltr">{channel.target}</span>
                          {channel.provider_key ? (
                            <>
                              {" — "}
                              <span dir="ltr">{channel.provider_key}</span>
                            </>
                          ) : null}
                        </span>
                      </span>
                      <span className="flex flex-wrap items-center gap-2">
                        <Badge variant="outline">
                          از سطح {alertSeverityLabelFa(channel.min_severity)}
                        </Badge>
                        <StatusBadge
                          status={channel.enabled ? "active" : "inactive"}
                          tone={channel.enabled ? "success" : "outline"}
                        />
                        <Button
                          variant="outline"
                          size="sm"
                          className="min-h-11"
                          onClick={() => setChannelDialog({ open: true, channel })}
                          aria-label={`ویرایش ${channel.name}`}
                        >
                          <Pencil className="size-4" aria-hidden />
                          ویرایش
                        </Button>
                        <Button
                          variant="destructive"
                          size="sm"
                          className="min-h-11"
                          disabled={actingId === channel.id}
                          onClick={() => setChannelDeleteTarget(channel)}
                          aria-label={`حذف ${channel.name}`}
                        >
                          <Trash2 className="size-4" aria-hidden />
                          حذف
                        </Button>
                      </span>
                    </li>
                  ))}
                </ul>
              </CardContent>
            </Card>
          )}
        </TabsContent>

        <TabsContent value="tokens" className="space-y-6">
          <div className="flex justify-end">
            <Button
              onClick={() => setTokenDialog({ open: true, record: null })}
              className="min-h-11"
            >
              <Plus className="size-4" aria-hidden />
              توکن جدید
            </Button>
          </div>
          {tokensLoading ? (
            <div className="space-y-4" aria-label="در حال بارگذاری">
              <Skeleton className="h-24 w-full" />
              <Skeleton className="h-24 w-full" />
            </div>
          ) : tokensError ? (
            isPermissionDenied(tokensError) ? (
              <PermissionDeniedState
                message={persianApiErrorMessage(
                  tokensError,
                  "برای مشاهدهٔ توکن‌ها دسترسی ندارید.",
                )}
              />
            ) : (
              <ErrorState
                message={persianApiErrorMessage(tokensError, "بارگذاری توکن‌ها ناموفق بود.")}
                onRetry={loadTokens}
              />
            )
          ) : tokens.length === 0 ? (
            <Card>
              <CardContent className="py-6">
                <p className="text-sm text-muted-foreground">
                  توکنی ثبت نشده است؛ کلیدهای API حسابیار به‌صورت خودکار در نمای
                  کلی دیده می‌شوند.
                </p>
              </CardContent>
            </Card>
          ) : (
            <Card>
              <CardContent>
                <ul className="space-y-1">
                  {tokens.map((record) => (
                    <li
                      key={record.id}
                      className="flex min-h-11 flex-wrap items-center justify-between gap-4 rounded-lg px-2 py-2"
                    >
                      <span className="min-w-0">
                        <span className="block text-sm font-medium">{record.label}</span>
                        <span className="block text-xs text-muted-foreground">
                          {tokenKindLabelFa(record.kind)} — انقضا{" "}
                          {formatIsoJalali(record.expires_at.slice(0, 10))}
                          {record.notes ? ` — ${record.notes}` : ""}
                        </span>
                      </span>
                      <span className="flex flex-wrap items-center gap-2">
                        <StatusBadge
                          status={record.enabled ? "active" : "inactive"}
                          tone={record.enabled ? "success" : "outline"}
                        />
                        <Button
                          variant="outline"
                          size="sm"
                          className="min-h-11"
                          onClick={() => setTokenDialog({ open: true, record })}
                          aria-label={`ویرایش ${record.label}`}
                        >
                          <Pencil className="size-4" aria-hidden />
                          ویرایش
                        </Button>
                        <Button
                          variant="destructive"
                          size="sm"
                          className="min-h-11"
                          disabled={actingId === record.id}
                          onClick={() => setTokenDeleteTarget(record)}
                          aria-label={`حذف ${record.label}`}
                        >
                          <Trash2 className="size-4" aria-hidden />
                          حذف
                        </Button>
                      </span>
                    </li>
                  ))}
                </ul>
              </CardContent>
            </Card>
          )}
        </TabsContent>
      </Tabs>

      {channelDialog.open ? (
        <MonitoringChannelDialog
          key={channelDialog.channel?.id ?? "new"}
          open
          channel={channelDialog.channel}
          onClose={() => setChannelDialog({ open: false, channel: null })}
          onSaved={loadChannels}
        />
      ) : null}

      {tokenDialog.open ? (
        <MonitoringTokenDialog
          key={tokenDialog.record?.id ?? "new"}
          open
          record={tokenDialog.record}
          onClose={() => setTokenDialog({ open: false, record: null })}
          onSaved={loadTokens}
        />
      ) : null}

      <AlertDialog
        open={channelDeleteTarget !== null}
        onOpenChange={(next) => !next && setChannelDeleteTarget(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>حذف کانال هشدار</AlertDialogTitle>
            <AlertDialogDescription>
              کانال «{channelDeleteTarget?.name}» حذف شود؟ پس از حذف، هشدارها دیگر به
              این کانال نمی‌روند.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>انصراف</AlertDialogCancel>
            <AlertDialogAction onClick={handleDeleteChannel}>حذف</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={tokenDeleteTarget !== null}
        onOpenChange={(next) => !next && setTokenDeleteTarget(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>حذف توکن</AlertDialogTitle>
            <AlertDialogDescription>
              رکورد «{tokenDeleteTarget?.label}» حذف شود؟ پس از حذف، برای این توکن
              هشدار انقضا نمی‌آید.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>انصراف</AlertDialogCancel>
            <AlertDialogAction onClick={handleDeleteToken}>حذف</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
