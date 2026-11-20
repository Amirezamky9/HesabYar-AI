/**
 * Review queue page (US-504, Phase 5 F4) — the single queue for all
 * nine exception types (phased design §7.2), under module 1
 * (dashboard ← صف بازبینی).
 *
 * Data is live from the backend via the BFF: the list (filters +
 * default sort critical-first-then-oldest), the summary (per-type
 * counters + admin notifications for critical items past SLA) and
 * the SLA policies. Selecting an item opens a detail dialog with
 * the audit trail, the permitted actions for its type, and
 * assignment. Nothing here is fake: an empty queue renders an
 * honest empty state.
 */

"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { TriangleAlert } from "lucide-react";
import { ApiError, api, type ReviewItemResponse, type ReviewQueueSummaryResponse } from "@/lib/api-client";
import { formatIsoDateTime } from "@/lib/format-date";
import { normalizeDigits, parseMoneyInput } from "@/lib/format-money";
import { faNumber } from "@/lib/utils";
import {
  Badge,
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
  MoneyDisplay,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Skeleton,
  StatusBadge,
  Textarea,
} from "@/components/ui";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import { isPermissionDenied, persianApiErrorMessage } from "@/features/shared/api-error";
import {
  REVIEW_ITEM_TYPES,
  SLA_STATE_LABELS,
  formatAgeHours,
  isWaiting,
  reviewActionLabel,
  reviewResolutionLabel,
  reviewSeverityLabel,
  reviewSeverityTone,
  reviewStatusLabel,
  reviewStatusTone,
  reviewTypeLabel,
  slaState,
} from "./review-queue-data";
import { SlaPolicySettings } from "./sla-policy-settings";

interface Filters {
  type: string;
  severity: string;
  status: string;
  channel: string;
  minAmountToman: string;
  maxAmountToman: string;
  minAgeHours: string;
  maxAgeHours: string;
  overdueOnly: boolean;
  sort: string;
}

const DEFAULT_FILTERS: Filters = {
  type: "all",
  severity: "all",
  status: "all",
  channel: "",
  minAmountToman: "",
  maxAmountToman: "",
  minAgeHours: "",
  maxAgeHours: "",
  overdueOnly: false,
  sort: "priority",
};

const PAGE_SIZE = 20;

const SORT_LABELS: Record<string, string> = {
  priority: "بحرانی اول، بعد قدیمی‌ترین",
  created_at: "قدیمی‌ترین اول",
  "-created_at": "جدیدترین اول",
  amount: "مبلغ (کم به زیاد)",
  "-amount": "مبلغ (زیاد به کم)",
};

function parseHours(raw: string): number | undefined {
  if (!raw.trim()) return undefined;
  const value = Number(normalizeDigits(raw.trim()));
  return Number.isFinite(value) && value >= 0 ? value : undefined;
}

export function ReviewQueuePage({ initialType }: { initialType?: string | null } = {}) {
  const router = useRouter();
  const validInitialType =
    initialType !== null && initialType !== undefined && (REVIEW_ITEM_TYPES as readonly string[]).includes(initialType)
      ? initialType
      : "all";
  const [filters, setFilters] = React.useState<Filters>({ ...DEFAULT_FILTERS, type: validInitialType });
  const [items, setItems] = React.useState<ReviewItemResponse[]>([]);
  const [nextCursor, setNextCursor] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [loadingMore, setLoadingMore] = React.useState(false);
  const [error, setError] = React.useState<unknown>(null);
  const [summary, setSummary] = React.useState<ReviewQueueSummaryResponse | null>(null);
  const [summaryError, setSummaryError] = React.useState(false);
  const [reloadKey, setReloadKey] = React.useState(0);
  const [selectedId, setSelectedId] = React.useState<string | null>(null);

  const setFilter = <K extends keyof Filters>(key: K, value: Filters[K]) => {
    setLoading(true);
    setError(null);
    setFilters((prev) => ({ ...prev, [key]: value }));
  };

  const retry = () => {
    setLoading(true);
    setError(null);
    setReloadKey((k) => k + 1);
  };

  const clearFilters = () => {
    setLoading(true);
    setError(null);
    setFilters(DEFAULT_FILTERS);
    // Force a refetch even when the filters were already default (in
    // which case setFilters alone would not re-run the fetch effect
    // and loading would never clear).
    setReloadKey((k) => k + 1);
  };

  const buildParams = React.useCallback(
    (cursor?: string) => ({
      type: filters.type === "all" ? undefined : filters.type,
      severity: filters.severity === "all" ? undefined : filters.severity,
      status: filters.status === "all" ? undefined : filters.status,
      channel: filters.channel.trim() || undefined,
      min_amount: filters.minAmountToman.trim()
        ? (parseMoneyInput(filters.minAmountToman) ?? undefined)
        : undefined,
      max_amount: filters.maxAmountToman.trim()
        ? (parseMoneyInput(filters.maxAmountToman) ?? undefined)
        : undefined,
      min_age_hours: parseHours(filters.minAgeHours),
      max_age_hours: parseHours(filters.maxAgeHours),
      overdue: filters.overdueOnly || undefined,
      sort: filters.sort,
      limit: PAGE_SIZE,
      cursor,
    }),
    [filters],
  );

  // The list refetches whenever the filters change (300ms debounce for
  // the free-text fields, so typing a channel does not fire per keystroke).
  // Loading state is set by the event handlers (filter change / retry),
  // never synchronously inside this effect.
  React.useEffect(() => {
    let cancelled = false;
    const timer = setTimeout(() => {
      api
        .listReviewItems(buildParams())
        .then((page) => {
          if (cancelled) return;
          setItems(page.items);
          setNextCursor(page.next_cursor);
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
    }, 300);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [buildParams, reloadKey, router]);

  // The summary loads once per mount/retry (it is filter-independent).
  React.useEffect(() => {
    let cancelled = false;
    api
      .getReviewQueueSummary()
      .then((data) => {
        if (!cancelled) setSummary(data);
      })
      .catch(() => {
        if (!cancelled) setSummaryError(true);
      });
    return () => {
      cancelled = true;
    };
  }, [reloadKey]);

  const loadMore = async () => {
    if (!nextCursor) return;
    setLoadingMore(true);
    try {
      const page = await api.listReviewItems(buildParams(nextCursor));
      setItems((prev) => [...prev, ...page.items]);
      setNextCursor(page.next_cursor);
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setError(err);
    } finally {
      setLoadingMore(false);
    }
  };

  const refreshAfterMutation = () => {
    // Show skeletons while the refetch runs: without this, the stale
    // pre-action list stays rendered and a reader (or test) can act on
    // items whose status just changed, until the refetch replaces them.
    setLoading(true);
    setError(null);
    setReloadKey((k) => k + 1);
  };

  if (error && isPermissionDenied(error)) {
    return (
      <PermissionDeniedState message={persianApiErrorMessage(error, "دریافت صف بازبینی ناموفق بود.")} />
    );
  }

  if (error) {
    return (
      <ErrorState
        message={persianApiErrorMessage(error, "دریافت صف بازبینی ناموفق بود.")}
        onRetry={retry}
      />
    );
  }

  const waitingTotal = summary ? summary.total_open + summary.total_in_review : null;

  return (
    <div className="space-y-6" data-testid="review-queue-page">
      <div>
        <h1 className="text-2xl font-bold">صف بازبینی</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          یک صف واحد برای همهٔ استثناها؛ بحرانی‌ها اول، بعد قدیمی‌ترین. هر اقدام روی هر آیتم در ممیزی ثبت
          می‌شود.
        </p>
      </div>

      {summary && summary.critical_overdue_count > 0 ? (
        <Card className="border-destructive/40 bg-destructive/5" data-testid="review-admin-alert">
          <CardHeader>
            <div className="flex items-center gap-2">
              <TriangleAlert className="size-5 text-destructive" strokeWidth={2} aria-hidden />
              <CardTitle className="text-destructive">نیازمند توجه مدیر</CardTitle>
            </div>
          </CardHeader>
          <CardContent className="space-y-2">
            <p className="text-sm">
              {faNumber(summary.critical_overdue_count)} آیتم بحرانی از مهلت خود گذشته است.
            </p>
            <ul className="space-y-1">
              {summary.admin_notifications.map((note) => (
                <li key={note.item_id}>
                  <Button
                    variant="link"
                    className="h-auto min-h-11 p-0 text-start text-sm"
                    onClick={() => setSelectedId(note.item_id)}
                  >
                    {reviewTypeLabel(note.item_type)} — {formatAgeHours(note.overdue_hours)} گذشته از مهلت
                  </Button>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      ) : null}

      <section aria-label="شمارنده‌های صف">
        {summaryError ? (
          <p className="text-sm text-muted-foreground">دریافت شمارنده‌ها ناموفق بود؛ فهرست زیر همچنان به‌روز است.</p>
        ) : summary === null ? (
          <div className="grid gap-4 sm:grid-cols-3 lg:grid-cols-5" aria-label="در حال بارگذاری">
            {Array.from({ length: 5 }).map((_, i) => (
              <Skeleton key={i} className="h-20 w-full" />
            ))}
          </div>
        ) : (
          <div className="grid gap-4 sm:grid-cols-3 lg:grid-cols-5">
            <CounterCard label="در انتظار بررسی" value={waitingTotal ?? 0} testId="counter-waiting" />
            <CounterCard label="باز" value={summary.total_open} testId="counter-open" />
            <CounterCard label="در حال بررسی" value={summary.total_in_review} testId="counter-in-review" />
            <CounterCard label="گذشته از مهلت" value={summary.total_overdue} testId="counter-overdue" />
            <CounterCard label="بحرانی گذشته از مهلت" value={summary.critical_overdue_count} testId="counter-critical-overdue" />
          </div>
        )}
      </section>

      <Card>
        <CardHeader>
          <CardTitle>فیلترها</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <div className="space-y-2">
              <Label htmlFor="filter-type">نوع آیتم</Label>
              <Select value={filters.type} onValueChange={(v) => v && setFilter("type", v)}>
                <SelectTrigger id="filter-type">
                  {/* Base UI resolves the trigger label from the popup
                  items, which mount only on first open — a controlled
                  value (e.g. the ?type= deep link) would otherwise
                  display its raw code. Pass the label explicitly. */}
                  <SelectValue>
                    {filters.type === "all" ? "همهٔ انواع" : reviewTypeLabel(filters.type)}
                  </SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">همهٔ انواع</SelectItem>
                  {REVIEW_ITEM_TYPES.map((type) => (
                    <SelectItem key={type} value={type}>
                      {reviewTypeLabel(type)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="filter-severity">شدت</Label>
              <Select value={filters.severity} onValueChange={(v) => v && setFilter("severity", v)}>
                <SelectTrigger id="filter-severity">
                  <SelectValue>
                    {filters.severity === "all" ? "همهٔ شدت‌ها" : reviewSeverityLabel(filters.severity)}
                  </SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">همهٔ شدت‌ها</SelectItem>
                  <SelectItem value="critical">بحرانی</SelectItem>
                  <SelectItem value="warning">هشدار</SelectItem>
                  <SelectItem value="info">اطلاع</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="filter-status">وضعیت</Label>
              <Select value={filters.status} onValueChange={(v) => v && setFilter("status", v)}>
                <SelectTrigger id="filter-status">
                  <SelectValue>
                    {filters.status === "all" ? "همهٔ وضعیت‌ها" : reviewStatusLabel(filters.status)}
                  </SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">همهٔ وضعیت‌ها</SelectItem>
                  <SelectItem value="open">باز</SelectItem>
                  <SelectItem value="in_review">در حال بررسی</SelectItem>
                  <SelectItem value="resolved">حل‌شده</SelectItem>
                  <SelectItem value="written_off">بسته‌شده</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="filter-sort">مرتب‌سازی</Label>
              <Select value={filters.sort} onValueChange={(v) => v && setFilter("sort", v)}>
                <SelectTrigger id="filter-sort">
                  <SelectValue>{SORT_LABELS[filters.sort] ?? filters.sort}</SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="priority">بحرانی اول، بعد قدیمی‌ترین</SelectItem>
                  <SelectItem value="created_at">قدیمی‌ترین اول</SelectItem>
                  <SelectItem value="-created_at">جدیدترین اول</SelectItem>
                  <SelectItem value="amount">مبلغ (کم به زیاد)</SelectItem>
                  <SelectItem value="-amount">مبلغ (زیاد به کم)</SelectItem>
                  <SelectItem value="age">قدمت (کم به زیاد)</SelectItem>
                  <SelectItem value="-age">قدمت (زیاد به کم)</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="filter-channel">کانال / درگاه</Label>
              <Input
                id="filter-channel"
                value={filters.channel}
                onChange={(e) => setFilter("channel", e.target.value)}
                placeholder="مثلاً zarinpal"
                dir="ltr"
                className="text-start"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="filter-min-amount">حداقل مبلغ (تومان)</Label>
              <Input
                id="filter-min-amount"
                inputMode="numeric"
                value={filters.minAmountToman}
                onChange={(e) => setFilter("minAmountToman", e.target.value)}
                placeholder="۰"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="filter-max-amount">حداکثر مبلغ (تومان)</Label>
              <Input
                id="filter-max-amount"
                inputMode="numeric"
                value={filters.maxAmountToman}
                onChange={(e) => setFilter("maxAmountToman", e.target.value)}
                placeholder="۰"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="filter-min-age">حداقل قدمت (ساعت)</Label>
              <Input
                id="filter-min-age"
                inputMode="numeric"
                value={filters.minAgeHours}
                onChange={(e) => setFilter("minAgeHours", e.target.value)}
                placeholder="۰"
              />
            </div>
            <div className="flex items-end gap-2">
              <div className="flex min-h-11 items-center gap-2">
                <Checkbox
                  id="filter-overdue"
                  checked={filters.overdueOnly}
                  onCheckedChange={(checked) => setFilter("overdueOnly", checked === true)}
                />
                <Label htmlFor="filter-overdue">فقط گذشته از مهلت</Label>
              </div>
            </div>
            <div className="flex items-end">
              <Button variant="outline" onClick={clearFilters}>
                پاک کردن فیلترها
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      <section aria-label="آیتم‌های صف" className="space-y-4">
        {loading ? (
          <div className="space-y-4" aria-label="در حال بارگذاری">
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-28 w-full" />
            ))}
          </div>
        ) : items.length === 0 ? (
          <Card data-testid="review-empty-state">
            <CardContent className="py-8 text-center">
              <p className="font-medium">آیتمی در صف نیست.</p>
              <p className="mt-1 text-sm text-muted-foreground">
                استثناهای ماژول‌ها — تسویه، چک، بانک، بسته‌بندی و بقیه — اینجا جمع می‌شوند.
              </p>
            </CardContent>
          </Card>
        ) : (
          <>
            <ul className="space-y-4">
              {items.map((item) => (
                <li key={item.id}>
                  <ReviewItemCard item={item} onOpen={() => setSelectedId(item.id)} />
                </li>
              ))}
            </ul>
            {nextCursor ? (
              <div className="flex justify-center">
                <Button variant="outline" onClick={loadMore} disabled={loadingMore}>
                  {loadingMore ? "در حال بارگذاری…" : "بارگذاری بیشتر"}
                </Button>
              </div>
            ) : null}
          </>
        )}
      </section>

      <SlaPolicySettings />

      <ReviewItemDialog
        itemId={selectedId}
        onClose={() => setSelectedId(null)}
        onMutated={refreshAfterMutation}
      />
    </div>
  );
}

function CounterCard({ label, value, testId }: { label: string; value: number; testId: string }) {
  return (
    <Card data-testid={testId}>
      <CardContent className="pt-4">
        <p className="text-2xl font-bold">{faNumber(value)}</p>
        <p className="mt-1 text-sm text-muted-foreground">{label}</p>
      </CardContent>
    </Card>
  );
}

function ReviewItemCard({ item, onOpen }: { item: ReviewItemResponse; onOpen: () => void }) {
  const sla = slaState(item);
  const slaInfo = SLA_STATE_LABELS[sla];
  return (
    <Card data-testid="review-item-card">
      <CardContent className="space-y-3 pt-4">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-base font-semibold">{reviewTypeLabel(item.item_type)}</h2>
          <StatusBadge status={item.severity} label={reviewSeverityLabel(item.severity)} tone={reviewSeverityTone(item.severity)} />
          <StatusBadge status={item.status} label={reviewStatusLabel(item.status)} tone={reviewStatusTone(item.status)} />
          <StatusBadge status={sla} label={slaInfo.label} tone={slaInfo.tone} />
          {item.channel ? <Badge variant="outline">{item.channel}</Badge> : null}
        </div>
        <p className="text-sm">{item.reason}</p>
        <p className="text-sm text-muted-foreground">پیشنهاد: {item.suggested_action}</p>
        <dl className="flex flex-wrap gap-x-6 gap-y-2 text-sm">
          <div className="flex gap-2">
            <dt className="text-muted-foreground">قدمت:</dt>
            <dd>{formatAgeHours(item.age_hours)}</dd>
          </div>
          <div className="flex gap-2">
            <dt className="text-muted-foreground">مهلت:</dt>
            <dd>{formatIsoDateTime(item.sla_deadline)}</dd>
          </div>
          {item.amount_irr !== null ? (
            <div className="flex gap-2">
              <dt className="text-muted-foreground">مبلغ:</dt>
              <dd>
                <MoneyDisplay amountIrr={item.amount_irr} />
              </dd>
            </div>
          ) : null}
          {item.assignee ? (
            <div className="flex gap-2">
              <dt className="text-muted-foreground">مسئول:</dt>
              <dd>{item.assignee}</dd>
            </div>
          ) : null}
          {item.resolution ? (
            <div className="flex gap-2">
              <dt className="text-muted-foreground">نتیجه:</dt>
              <dd>{reviewResolutionLabel(item.resolution)}</dd>
            </div>
          ) : null}
        </dl>
        <div>
          <Button variant="outline" size="sm" onClick={onOpen}>
            جزئیات و اقدام
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function ReviewItemDialog({
  itemId,
  onClose,
  onMutated,
}: {
  itemId: string | null;
  onClose: () => void;
  onMutated: () => void;
}) {
  const router = useRouter();
  const [item, setItem] = React.useState<ReviewItemResponse | null>(null);
  const [loadError, setLoadError] = React.useState<{ itemId: string; message: string } | null>(null);
  const [actionCode, setActionCode] = React.useState("");
  const [note, setNote] = React.useState("");
  const [acting, setActing] = React.useState(false);
  const [actionError, setActionError] = React.useState<string | null>(null);
  const [assignee, setAssignee] = React.useState("");
  const [assigning, setAssigning] = React.useState(false);

  // Loading and error are derived during render (no setState inside the
  // effect): open with an id whose detail has not arrived (or failed) yet.
  // The error is scoped to the item it belongs to, so opening another
  // item never shows a stale failure.
  const open = itemId !== null;
  const error = loadError !== null && loadError.itemId === itemId ? loadError.message : null;
  const loading = open && (item === null || item.id !== itemId) && error === null;

  const handleClose = () => {
    setItem(null);
    setLoadError(null);
    setActionCode("");
    setNote("");
    setActionError(null);
    onClose();
  };

  React.useEffect(() => {
    if (itemId === null) return;
    // Already showing this item (e.g. after an action) — no refetch.
    if (item !== null && item.id === itemId) return;
    let cancelled = false;
    api
      .getReviewItem(itemId)
      .then((data) => {
        if (cancelled) return;
        setItem(data);
        setAssignee(data.assignee ?? "");
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.replace("/login");
          return;
        }
        setLoadError({ itemId, message: persianApiErrorMessage(err, "دریافت جزئیات آیتم ناموفق بود.") });
      });
    return () => {
      cancelled = true;
    };
  }, [itemId, item, router]);

  const performAction = async () => {
    if (!item || !actionCode) return;
    setActing(true);
    setActionError(null);
    try {
      const updated = await api.performReviewAction(item.id, {
        action: actionCode,
        note: note.trim() || null,
      });
      setItem(updated);
      setActionCode("");
      setNote("");
      onMutated();
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setActionError(persianApiErrorMessage(err, "انجام اقدام ناموفق بود."));
    } finally {
      setActing(false);
    }
  };

  const assign = async (value: string | null) => {
    if (!item) return;
    setAssigning(true);
    setActionError(null);
    try {
      const updated = await api.assignReviewItem(item.id, { assignee: value });
      setItem(updated);
      setAssignee(updated.assignee ?? "");
      onMutated();
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setActionError(persianApiErrorMessage(err, "تخصیص ناموفق بود."));
    } finally {
      setAssigning(false);
    }
  };

  const waiting = item ? isWaiting(item) : false;

  return (
    <Dialog
      open={open}
      onOpenChange={(isOpen) => {
        if (!isOpen) handleClose();
      }}
    >
      <DialogContent
        className="max-h-[90vh] overflow-y-auto"
        data-testid="review-item-dialog"
        // No built-in X close: it duplicates the footer «بستن» button's
        // accessible name (two «بستن» buttons in one dialog) and its
        // icon-sm hit target is below the 44px minimum. The footer
        // button is the dialog's single close control; Escape still works.
        showCloseButton={false}
      >
        <DialogHeader>
          <DialogTitle>{item ? reviewTypeLabel(item.item_type) : "جزئیات آیتم"}</DialogTitle>
          <DialogDescription>
            {item ? item.reason : "در حال دریافت جزئیات…"}
          </DialogDescription>
        </DialogHeader>
        {loading ? (
          <div className="space-y-4" aria-label="در حال بارگذاری">
            <Skeleton className="h-6 w-full" />
            <Skeleton className="h-6 w-2/3" />
            <Skeleton className="h-24 w-full" />
          </div>
        ) : error ? (
          <p className="text-sm text-destructive" role="alert">
            {error}
          </p>
        ) : item ? (
          <div className="space-y-6">
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge status={item.severity} label={reviewSeverityLabel(item.severity)} tone={reviewSeverityTone(item.severity)} />
              <StatusBadge status={item.status} label={reviewStatusLabel(item.status)} tone={reviewStatusTone(item.status)} />
              <StatusBadge
                status={slaState(item)}
                label={SLA_STATE_LABELS[slaState(item)].label}
                tone={SLA_STATE_LABELS[slaState(item)].tone}
              />
            </div>
            <dl className="grid gap-2 text-sm sm:grid-cols-2">
              <div className="flex gap-2">
                <dt className="text-muted-foreground">پیشنهاد اقدام:</dt>
                <dd>{item.suggested_action}</dd>
              </div>
              <div className="flex gap-2">
                <dt className="text-muted-foreground">قدمت:</dt>
                <dd>{formatAgeHours(item.age_hours)}</dd>
              </div>
              <div className="flex gap-2">
                <dt className="text-muted-foreground">مهلت:</dt>
                <dd>{formatIsoDateTime(item.sla_deadline)}</dd>
              </div>
              <div className="flex gap-2">
                <dt className="text-muted-foreground">ثبت‌شده:</dt>
                <dd>{formatIsoDateTime(item.created_at)}</dd>
              </div>
              {item.amount_irr !== null ? (
                <div className="flex gap-2">
                  <dt className="text-muted-foreground">مبلغ:</dt>
                  <dd>
                    <MoneyDisplay amountIrr={item.amount_irr} />
                  </dd>
                </div>
              ) : null}
              {item.channel ? (
                <div className="flex gap-2">
                  <dt className="text-muted-foreground">کانال:</dt>
                  <dd>{item.channel}</dd>
                </div>
              ) : null}
              {item.source_event_ref ? (
                <div className="flex gap-2">
                  <dt className="text-muted-foreground">رویداد مبدأ:</dt>
                  <dd dir="ltr">{item.source_event_ref}</dd>
                </div>
              ) : null}
              {item.resolution ? (
                <div className="flex gap-2">
                  <dt className="text-muted-foreground">نتیجه:</dt>
                  <dd>{reviewResolutionLabel(item.resolution)}</dd>
                </div>
              ) : null}
            </dl>

            {waiting ? (
              <div className="space-y-4 rounded-lg border p-4">
                <h3 className="font-medium">انجام اقدام</h3>
                <div className="space-y-2">
                  <Label htmlFor="review-action">اقدام</Label>
                  <Select value={actionCode} onValueChange={(v) => v && setActionCode(v)}>
                    <SelectTrigger id="review-action">
                      <SelectValue placeholder="یک اقدام انتخاب کنید" />
                    </SelectTrigger>
                    <SelectContent>
                      {item.actions.map((action) => (
                        <SelectItem key={action.code} value={action.code}>
                          {reviewActionLabel(action.code)}
                          {action.requires_manager ? " (نیازمند مدیر)" : ""}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="review-note">یادداشت (اختیاری)</Label>
                  <Textarea
                    id="review-note"
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    placeholder="دلیل یا توضیح اقدام…"
                  />
                </div>
                <Button onClick={performAction} disabled={!actionCode || acting} data-testid="perform-action">
                  {acting ? "در حال انجام…" : "انجام اقدام"}
                </Button>
                <div className="space-y-2 border-t pt-4">
                  <Label htmlFor="review-assignee">تخصیص به</Label>
                  <div className="flex flex-wrap gap-2">
                    <Input
                      id="review-assignee"
                      value={assignee}
                      onChange={(e) => setAssignee(e.target.value)}
                      placeholder="نام مسئول"
                      className="min-w-44 flex-1"
                    />
                    <Button variant="outline" onClick={() => assign(assignee.trim() || null)} disabled={assigning}>
                      {assigning ? "در حال تخصیص…" : "تخصیص"}
                    </Button>
                    {item.assignee ? (
                      <Button variant="ghost" onClick={() => assign(null)} disabled={assigning}>
                        برداشتن تخصیص
                      </Button>
                    ) : null}
                  </div>
                </div>
                {actionError ? (
                  <p className="text-sm text-destructive" role="alert">
                    {actionError}
                  </p>
                ) : null}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">
                این آیتم بسته شده است؛ اقدام دیگری روی آن ممکن نیست.
              </p>
            )}

            <div className="space-y-2">
              <h3 className="font-medium">ممیزی</h3>
              {item.audit.length === 0 ? (
                <p className="text-sm text-muted-foreground">هنوز اقدامی روی این آیتم ثبت نشده است.</p>
              ) : (
                <ol className="space-y-2">
                  {item.audit.map((entry) => (
                    <li key={entry.id} className="rounded-lg border p-3 text-sm">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-medium">{reviewActionLabel(entry.action)}</span>
                        <span className="text-muted-foreground">توسط {entry.actor}</span>
                        <span className="text-muted-foreground">
                          {entry.created_at ? formatIsoDateTime(entry.created_at) : "—"}
                        </span>
                      </div>
                      {entry.decision ? (
                        <p className="mt-1 text-muted-foreground">تصمیم: {entry.decision}</p>
                      ) : null}
                      {entry.note ? <p className="mt-1">{entry.note}</p> : null}
                    </li>
                  ))}
                </ol>
              )}
            </div>
          </div>
        ) : null}
        <DialogFooter>
          <Button variant="outline" onClick={handleClose}>
            بستن
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
