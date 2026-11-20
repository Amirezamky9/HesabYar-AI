/**
 * Counting-session dialog (US-516) — the Iranian three-step method
 * in one place: enter the counted amount (with an optional
 * denomination breakdown), collect the signatures (cashier +
 * supervisor + manager on the minutes, only when the variance is
 * non-zero), and — after the manager approves the variance in the
 * review queue — post the DRAFT variance journal.
 *
 * Money the operator types is Toman (AmountInput); the API takes
 * integer IRR (×10 at the boundary). The book balance is read-only:
 * it was snapshotted from Tryton when the session started.
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ApiError, api, type CashBoxResponse, type CountSessionResponse } from "@/lib/api-client";
import { formatIsoDateTime } from "@/lib/format-date";
import { formatMoney } from "@/lib/format-money";
import { faNumber } from "@/lib/utils";
import {
  AmountInput,
  Button,
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
import { isPermissionDenied, persianApiErrorMessage } from "@/features/shared/api-error";
import {
  denominationsTotal,
  sessionStatusLabel,
  sessionStatusTone,
  signRoleLabel,
  varianceDirectionLabel,
  varianceDirectionTone,
} from "./cash-box-data";

/** Common banknote/coin values in Toman for the denomination breakdown. */
const COMMON_DENOMINATIONS_TOMAN = [
  500_000, 200_000, 100_000, 50_000, 20_000, 10_000, 5_000, 2_000, 1_000, 500,
];

interface DenominationRow {
  key: number;
  denominationToman: number;
  count: string;
}

let nextRowKey = 1;

function newRow(): DenominationRow {
  return { key: nextRowKey++, denominationToman: 100_000, count: "" };
}

export function CashBoxSessionDialog({
  sessionId,
  boxes,
  open,
  onClose,
  onChanged,
}: {
  sessionId: string | null;
  boxes: CashBoxResponse[];
  open: boolean;
  onClose: () => void;
  onChanged: () => void;
}) {
  const router = useRouter();
  const [session, setSession] = React.useState<CountSessionResponse | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<unknown>(null);
  const [actionError, setActionError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  // Count entry (Toman in the UI, IRR on the wire).
  const [countedToman, setCountedToman] = React.useState<number | null>(null);
  const [denominations, setDenominations] = React.useState<DenominationRow[]>([]);
  // Signatures.
  const [minutesText, setMinutesText] = React.useState("");
  const [cashierActor, setCashierActor] = React.useState("");
  const [supervisorActor, setSupervisorActor] = React.useState("");
  const [managerActor, setManagerActor] = React.useState("");
  const [postActor, setPostActor] = React.useState("");
  const [recountActor, setRecountActor] = React.useState("");

  const load = React.useCallback(async (id: string) => {
    setLoading(true);
    setError(null);
    setActionError(null);
    try {
      const data = await api.getCountSession(id);
      setSession(data);
      setCountedToman(data.counted_amount_irr === null ? null : data.counted_amount_irr / 10);
      setDenominations(
        data.denominations.map((line) => ({
          key: nextRowKey++,
          denominationToman: line.denomination_irr / 10,
          count: String(line.count),
        })),
      );
      setCashierActor(data.cashier);
      setSupervisorActor(data.supervisor);
      setMinutesText(data.minutes_text ?? "");
    } catch (err: unknown) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  // Fetch inside the effect with promise callbacks (repo pattern):
  // state is set in the callbacks, never synchronously in the effect
  // body. Reset-on-close lives in handleClose, not here.
  React.useEffect(() => {
    if (!open || sessionId === null) return;
    // Already showing this session (e.g. after an action) — no refetch.
    if (session !== null && session.id === sessionId) return;
    let cancelled = false;
    api
      .getCountSession(sessionId)
      .then((data) => {
        if (cancelled) return;
        setSession(data);
        setCountedToman(data.counted_amount_irr === null ? null : data.counted_amount_irr / 10);
        setDenominations(
          data.denominations.map((line) => ({
            key: nextRowKey++,
            denominationToman: line.denomination_irr / 10,
            count: String(line.count),
          })),
        );
        setCashierActor(data.cashier);
        setSupervisorActor(data.supervisor);
        setMinutesText(data.minutes_text ?? "");
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.replace("/login");
          return;
        }
        setError(err);
      });
    return () => {
      cancelled = true;
    };
  }, [open, sessionId, session, router]);

  const boxName = session ? (boxes.find((b) => b.id === session.cash_box_id)?.name_fa ?? "صندوق") : "";
  // Skeleton while the effect's fetch for this session is in flight.
  const fetching =
    loading || (open && sessionId !== null && (session === null || session.id !== sessionId));

  const countLocked =
    session !== null &&
    (session.witnessed_by !== null ||
      session.minutes_cashier_signed_at !== null ||
      session.minutes_supervisor_signed_at !== null ||
      session.minutes_manager_signed_at !== null);
  const canEnterCount =
    session !== null &&
    (session.status === "in_progress" || session.status === "awaiting_supervisor") &&
    !countLocked;
  const canSign = session !== null && session.status === "awaiting_supervisor";

  const denominationRowsIrr = denominations
    .map((row) => ({
      denomination_irr: row.denominationToman * 10,
      count: Number(row.count) || 0,
    }))
    .filter((row) => row.count > 0);
  const breakdownTotalIrr = denominationsTotal(denominationRowsIrr);
  const countedIrr = countedToman === null ? null : countedToman * 10;
  const breakdownMismatch =
    denominationRowsIrr.length > 0 && countedIrr !== null && breakdownTotalIrr !== countedIrr;

  const refresh = async () => {
    onChanged();
    if (sessionId) await load(sessionId);
  };

  const runAction = async (fn: () => Promise<CountSessionResponse>, fallback: string) => {
    setBusy(true);
    setActionError(null);
    try {
      await fn();
      await refresh();
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        setActionError("نشست شما به پایان رسیده است؛ دوباره وارد شوید.");
      } else {
        setActionError(persianApiErrorMessage(err, fallback));
      }
      // The request may have timed out at the BFF after the server
      // already applied it (the manager sign completes the session
      // server-side): refresh — now and once more after a short
      // delay — so the dialog shows the true state instead of a
      // stale one.
      await refresh().catch(() => undefined);
      window.setTimeout(() => {
        void refresh().catch(() => undefined);
      }, 3000);
    } finally {
      setBusy(false);
    }
  };

  const handleEnterCount = () => {
    if (!session || countedIrr === null) return;
    if (breakdownMismatch) {
      setActionError("مجموع اسکناس‌ها و سکه‌ها با مبلغ شمارش‌شده برابر نیست.");
      return;
    }
    void runAction(
      () =>
        api.enterCount(session.id, {
          counted_amount_irr: countedIrr,
          denominations: denominationRowsIrr,
          actor: session.cashier,
        }),
      "ثبت مبلغ شمارش ناموفق بود.",
    );
  };

  const handleSign = (role: "cashier" | "supervisor" | "manager") => {
    if (!session) return;
    const actor = role === "cashier" ? cashierActor : role === "supervisor" ? supervisorActor : managerActor;
    void runAction(
      () =>
        api.signSession(session.id, {
          role,
          actor: actor.trim(),
          minutes_text: role === "cashier" ? minutesText.trim() || null : null,
        }),
      "ثبت امضا ناموفق بود.",
    );
  };

  const handlePost = () => {
    if (!session) return;
    void runAction(
      () => api.postVarianceJournal(session.id, { actor: postActor.trim() || session.cashier }),
      "ثبت سند در دفتر ناموفق بود.",
    );
  };

  const handleRecount = () => {
    if (!session) return;
    void runAction(
      () => api.recountSession(session.id, { actor: recountActor.trim() || session.cashier }),
      "شروع شمارش مجدد ناموفق بود.",
    );
  };

  const handleClose = () => {
    // Reset here (event handler), so the next open refetches cleanly.
    setSession(null);
    setError(null);
    setActionError(null);
    onClose();
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(isOpen) => {
        if (!isOpen) handleClose();
      }}
    >
      <DialogContent
        className="max-h-[90vh] overflow-y-auto"
        data-testid="cash-box-session-dialog"
        // No built-in X close: it duplicates the footer «بستن» button's
        // accessible name and its icon-sm hit target is below the 44px
        // minimum. The footer button is the dialog's single close
        // control; Escape still works.
        showCloseButton={false}
      >
        <DialogHeader>
          <DialogTitle>{session ? `شمارش ${boxName}` : "جلسهٔ شمارش"}</DialogTitle>
          <DialogDescription>
            {session
              ? `صندوقدار: ${session.cashier} · ناظر: ${session.supervisor} · ${formatIsoDateTime(session.created_at)}`
              : "در حال دریافت جزئیات…"}
          </DialogDescription>
        </DialogHeader>
        {fetching ? (
          <div className="space-y-4" aria-label="در حال بارگذاری">
            <Skeleton className="h-6 w-full" />
            <Skeleton className="h-6 w-2/3" />
            <Skeleton className="h-24 w-full" />
          </div>
        ) : error ? (
          <p className="text-sm text-destructive" role="alert">
            {isPermissionDenied(error)
              ? "کلید شما برای این بخش دسترسی ندارد."
              : persianApiErrorMessage(error, "دریافت جلسهٔ شمارش ناموفق بود.")}
          </p>
        ) : session ? (
          <div className="space-y-6">
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge
                status={session.status}
                label={sessionStatusLabel(session.status)}
                tone={sessionStatusTone(session.status)}
              />
              <StatusBadge
                status={session.variance_irr === null ? "unknown" : String(session.variance_irr)}
                label={varianceDirectionLabel(session.variance_irr)}
                tone={varianceDirectionTone(session.variance_irr)}
              />
            </div>

            <dl className="grid gap-2 text-sm sm:grid-cols-2">
              <div>
                <dt className="text-muted-foreground">ماندهٔ دفتری (فقط خواندنی)</dt>
                <dd className="mt-1 font-bold">
                  <MoneyDisplay amountIrr={session.book_balance_irr} />
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">مبلغ شمارش‌شده</dt>
                <dd className="mt-1 font-bold">
                  {session.counted_amount_irr === null ? (
                    "—"
                  ) : (
                    <MoneyDisplay amountIrr={session.counted_amount_irr} />
                  )}
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">مغایرت</dt>
                <dd className="mt-1 font-bold">
                  {session.variance_irr === null
                    ? "—"
                    : `${session.variance_irr < 0 ? "−" : session.variance_irr > 0 ? "+" : ""}${formatMoney(Math.abs(session.variance_irr))}`}
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">صورتجلسه</dt>
                <dd className="mt-1">
                  {session.minutes_required
                    ? session.minutes_signed
                      ? "امضاشده (صندوقدار، ناظر و مدیر)"
                      : "لازم است — هنوز کامل نشده"
                    : "لازم نیست (شمارش برابر است)"}
                </dd>
              </div>
            </dl>

            {session.minutes_text ? (
              <div>
                <h3 className="text-sm font-semibold">متن صورتجلسه</h3>
                <p className="mt-1 whitespace-pre-wrap rounded-md border p-3 text-sm">{session.minutes_text}</p>
                <ul className="mt-2 space-y-1 text-sm text-muted-foreground">
                  <li>
                    صندوقدار: {session.minutes_cashier_signed_by ?? "—"}
                    {session.minutes_cashier_signed_at ? ` · ${formatIsoDateTime(session.minutes_cashier_signed_at)}` : ""}
                  </li>
                  <li>
                    ناظر: {session.minutes_supervisor_signed_by ?? session.witnessed_by ?? "—"}
                    {session.minutes_supervisor_signed_at
                      ? ` · ${formatIsoDateTime(session.minutes_supervisor_signed_at)}`
                      : session.witnessed_at
                        ? ` · حضور: ${formatIsoDateTime(session.witnessed_at)}`
                        : ""}
                  </li>
                  <li>
                    مدیر: {session.minutes_manager_signed_by ?? "—"}
                    {session.minutes_manager_signed_at ? ` · ${formatIsoDateTime(session.minutes_manager_signed_at)}` : ""}
                  </li>
                </ul>
              </div>
            ) : null}

            {session.review_item_id ? (
              <p className="text-sm">
                آیتم صف بازبینی:{" "}
                <Link href="/review-queue" className="text-accent underline underline-offset-4">
                  مشاهده در صف بازبینی
                </Link>
                {session.draft_journal_id ? ` · سند پیش‌نویس ${faNumber(session.draft_journal_id)}` : ""}
                {session.posted_journal_id ? ` · سند ثبت‌شده ${faNumber(session.posted_journal_id)}` : ""}
              </p>
            ) : null}

            {canEnterCount ? (
              <section aria-label="ثبت مبلغ شمارش" className="space-y-4 rounded-md border p-4">
                <h3 className="text-sm font-semibold">مبلغ شمارش‌شده را وارد کنید</h3>
                <div>
                  <Label htmlFor="counted-amount">مبلغ شمارش‌شده</Label>
                  <AmountInput
                    id="counted-amount"
                    value={countedToman}
                    onChange={setCountedToman}
                    min={0}
                    quick={[100_000, 500_000, 1_000_000, 5_000_000]}
                  />
                </div>
                <div>
                  <div className="flex items-center justify-between">
                    <Label>تفکیک اسکناس و سکه (اختیاری)</Label>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => setDenominations((rows) => [...rows, newRow()])}
                    >
                      افزودن ردیف
                    </Button>
                  </div>
                  {denominations.length === 0 ? (
                    <p className="mt-1 text-sm text-muted-foreground">
                      اگر اسکناس‌ها را شمرده‌اید، اینجا وارد کنید تا جمع آن‌ها با مبلغ شمارش‌شده مقایسه شود.
                    </p>
                  ) : (
                    <ul className="mt-2 space-y-2">
                      {denominations.map((row, index) => (
                        <li key={row.key} className="flex items-end gap-2">
                          <div className="flex-1">
                            <Label htmlFor={`denomination-${row.key}`}>اسکناس / سکه</Label>
                            <Select
                              value={String(row.denominationToman)}
                              onValueChange={(value) => {
                                if (value === null) return;
                                setDenominations((rows) =>
                                  rows.map((r) =>
                                    r.key === row.key ? { ...r, denominationToman: Number(value) } : r,
                                  ),
                                );
                              }}
                            >
                              <SelectTrigger id={`denomination-${row.key}`}>
                                <SelectValue />
                              </SelectTrigger>
                              <SelectContent>
                                {COMMON_DENOMINATIONS_TOMAN.map((value) => (
                                  <SelectItem key={value} value={String(value)}>
                                    {faNumber(value)} تومان
                                  </SelectItem>
                                ))}
                              </SelectContent>
                            </Select>
                          </div>
                          <div className="w-24">
                            <Label htmlFor={`denomination-count-${row.key}`}>تعداد</Label>
                            <Input
                              id={`denomination-count-${row.key}`}
                              inputMode="numeric"
                              value={row.count}
                              onChange={(event) =>
                                setDenominations((rows) =>
                                  rows.map((r) =>
                                    r.key === row.key ? { ...r, count: event.target.value } : r,
                                  ),
                                )
                              }
                              aria-label={`تعداد ردیف ${index + 1}`}
                            />
                          </div>
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            onClick={() =>
                              setDenominations((rows) => rows.filter((r) => r.key !== row.key))
                            }
                            aria-label={`حذف ردیف ${index + 1}`}
                          >
                            حذف
                          </Button>
                        </li>
                      ))}
                    </ul>
                  )}
                  {denominationRowsIrr.length > 0 ? (
                    <p className="mt-2 text-sm text-muted-foreground">
                      جمع تفکیک: {formatMoney(breakdownTotalIrr)}
                      {breakdownMismatch ? (
                        <span className="text-destructive"> — با مبلغ شمارش‌شده برابر نیست.</span>
                      ) : (
                        " — با مبلغ شمارش‌شده برابر است."
                      )}
                    </p>
                  ) : null}
                </div>
                <Button type="button" onClick={handleEnterCount} disabled={busy || countedToman === null}>
                  ثبت مبلغ شمارش
                </Button>
              </section>
            ) : null}

            {canSign ? (
              <section aria-label="امضاها" className="space-y-4 rounded-md border p-4">
                <h3 className="text-sm font-semibold">امضاها</h3>
                {session.minutes_required ? (
                  <p className="text-sm text-muted-foreground">
                    چون شمارش با ماندهٔ دفتری مغایرت دارد، صورتجلسه باید با امضای صندوقدار، ناظر و مدیر تهیه شود.
                  </p>
                ) : (
                  <p className="text-sm text-muted-foreground">
                    شمارش با ماندهٔ دفتری برابر است؛ فقط حضور ناظر کافی است و صورتجلسه لازم نیست.
                  </p>
                )}
                {session.minutes_required ? (
                  <div className="space-y-2">
                    <div>
                      <Label htmlFor="minutes-text">متن صورتجلسه</Label>
                      <Textarea
                        id="minutes-text"
                        value={minutesText}
                        onChange={(event) => setMinutesText(event.target.value)}
                        disabled={session.minutes_cashier_signed_at !== null}
                        placeholder="شرح شمارش، مغایرت و علت احتمالی آن…"
                      />
                    </div>
                    <div className="flex items-end gap-2">
                      <div className="flex-1">
                        <Label htmlFor="cashier-actor">{signRoleLabel("cashier")}</Label>
                        <Input
                          id="cashier-actor"
                          value={cashierActor}
                          onChange={(event) => setCashierActor(event.target.value)}
                          disabled={session.minutes_cashier_signed_at !== null}
                        />
                      </div>
                      <Button
                        type="button"
                        variant="outline"
                        onClick={() => handleSign("cashier")}
                        disabled={busy || session.minutes_cashier_signed_at !== null}
                      >
                        {session.minutes_cashier_signed_at ? "امضاشده" : "امضای صندوقدار"}
                      </Button>
                    </div>
                  </div>
                ) : null}
                <div className="flex items-end gap-2">
                  <div className="flex-1">
                    <Label htmlFor="supervisor-actor">{signRoleLabel("supervisor")}</Label>
                    <Input
                      id="supervisor-actor"
                      value={supervisorActor}
                      onChange={(event) => setSupervisorActor(event.target.value)}
                      disabled={session.witnessed_by !== null}
                    />
                  </div>
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => handleSign("supervisor")}
                    disabled={busy || session.witnessed_by !== null}
                  >
                    {session.witnessed_by ? "حاضر شده" : "امضای ناظر"}
                  </Button>
                </div>
                {session.minutes_required ? (
                  <div className="flex items-end gap-2">
                    <div className="flex-1">
                      <Label htmlFor="manager-actor">{signRoleLabel("manager")}</Label>
                      <Input
                        id="manager-actor"
                        value={managerActor}
                        onChange={(event) => setManagerActor(event.target.value)}
                        disabled={session.minutes_manager_signed_at !== null}
                        placeholder="نام مدیر (متفاوت از صندوقدار و ناظر)"
                      />
                    </div>
                    <Button
                      type="button"
                      variant="outline"
                      onClick={() => handleSign("manager")}
                      disabled={busy || session.minutes_manager_signed_at !== null}
                    >
                      {session.minutes_manager_signed_at ? "امضاشده" : "امضای مدیر"}
                    </Button>
                  </div>
                ) : null}
              </section>
            ) : null}

            {session.status === "awaiting_manager" ? (
              <section aria-label="ثبت سند و شمارش مجدد" className="space-y-4 rounded-md border p-4">
                <h3 className="text-sm font-semibold">سند کسری / اضافه</h3>
                <p className="text-sm text-muted-foreground">
                  سند پیش‌نویس آماده است و در صف بازبینی منتظر تأیید مدیر است. تا وقتی مدیر آن را تأیید نکند،
                  سند در دفتر ثبت نمی‌شود.
                </p>
                <div className="flex flex-wrap items-end gap-2">
                  <div className="flex-1">
                    <Label htmlFor="post-actor">ثبت‌کنندهٔ سند</Label>
                    <Input
                      id="post-actor"
                      value={postActor}
                      onChange={(event) => setPostActor(event.target.value)}
                      placeholder={session.cashier}
                    />
                  </div>
                  <Button type="button" onClick={handlePost} disabled={busy}>
                    ثبت سند در دفتر
                  </Button>
                </div>
                <div className="flex flex-wrap items-end gap-2">
                  <div className="flex-1">
                    <Label htmlFor="recount-actor">شمارش مجدد (به درخواست مدیر)</Label>
                    <Input
                      id="recount-actor"
                      value={recountActor}
                      onChange={(event) => setRecountActor(event.target.value)}
                      placeholder={session.cashier}
                    />
                  </div>
                  <Button type="button" variant="outline" onClick={handleRecount} disabled={busy}>
                    شمارش مجدد
                  </Button>
                </div>
              </section>
            ) : null}

            {actionError ? (
              <p className="text-sm text-destructive" role="alert">
                {actionError}
              </p>
            ) : null}
          </div>
        ) : null}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={handleClose}>
            بستن
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
