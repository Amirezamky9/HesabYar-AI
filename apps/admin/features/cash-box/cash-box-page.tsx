/**
 * Cash boxes page (US-516, Phase 5-2 K2) — under module 5
 * (بانک و صندوق ← صندوق‌ها).
 *
 * Live from the backend via the BFF: the company's cash boxes
 * (each on one Tryton Statement Journal, with its live book
 * balance and counting state), the recent counting sessions, and
 * the cash-box settings. Registering a box, starting a session
 * and the counting flow itself live in dialogs; the session
 * dialog holds the count entry, signatures, minutes and the
 * variance-journal posting. Nothing here is fake: an empty
 * registry renders an honest empty state.
 */

"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { TriangleAlert } from "lucide-react";
import {
  ApiError,
  api,
  type CashBoxResponse,
  type CountSessionResponse,
  type CashBoxStatementJournalResponse,
} from "@/lib/api-client";
import { formatIsoDateTime } from "@/lib/format-date";
import {
  AmountInput,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  DataTable,
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
} from "@/components/ui";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import { CompanyPicker } from "@/features/shared/company-picker";
import { useCompany } from "@/features/shared/use-company";
import { isPermissionDenied, persianApiErrorMessage } from "@/features/shared/api-error";
import {
  CASH_BOX_KINDS,
  cashBoxKindLabel,
  daysSinceLabel,
  sessionStatusLabel,
  sessionStatusTone,
  varianceDirectionLabel,
  varianceDirectionTone,
} from "./cash-box-data";
import { CashBoxSessionDialog } from "./cash-box-session-dialog";
import { CashBoxSettings } from "./cash-box-settings";

const PAGE_SIZE = 20;

export function CashBoxPage() {
  const router = useRouter();
  const { companies, companyId, setCompanyId, loading: companiesLoading, error: companiesError } =
    useCompany();
  const [boxes, setBoxes] = React.useState<CashBoxResponse[] | null>(null);
  const [sessions, setSessions] = React.useState<CountSessionResponse[] | null>(null);
  const [nextCursor, setNextCursor] = React.useState<string | null>(null);
  const [loadingMore, setLoadingMore] = React.useState(false);
  const [error, setError] = React.useState<unknown>(null);
  const [reloadKey, setReloadKey] = React.useState(0);

  const [registerOpen, setRegisterOpen] = React.useState(false);
  const [startOpen, setStartOpen] = React.useState(false);
  const [startBox, setStartBox] = React.useState<CashBoxResponse | null>(null);
  const [selectedSessionId, setSelectedSessionId] = React.useState<string | null>(null);

  // Register-box form.
  const [journals, setJournals] = React.useState<CashBoxStatementJournalResponse[]>([]);
  const [journalsLoading, setJournalsLoading] = React.useState(false);
  const [boxName, setBoxName] = React.useState("");
  const [boxKind, setBoxKind] = React.useState<string>("store");
  const [journalId, setJournalId] = React.useState("");
  const [ceilingToman, setCeilingToman] = React.useState<number | null>(null);
  const [registerError, setRegisterError] = React.useState<string | null>(null);
  const [registering, setRegistering] = React.useState(false);

  // Start-session form.
  const [cashier, setCashier] = React.useState("");
  const [supervisor, setSupervisor] = React.useState("");
  const [startError, setStartError] = React.useState<string | null>(null);
  const [starting, setStarting] = React.useState(false);

  // Fetch inside the effect with promise callbacks (repo pattern):
  // state is set in the callbacks, never synchronously in the effect
  // body. Loading state is derived (boxes/sessions null), and retries
  // bump reloadKey.
  React.useEffect(() => {
    if (!companyId) return;
    let cancelled = false;
    Promise.all([
      api.listCashBoxes({ company: companyId, limit: 50 }),
      api.listCountSessions({ company: companyId, limit: PAGE_SIZE }),
    ])
      .then(([boxPage, sessionPage]) => {
        if (cancelled) return;
        setBoxes(boxPage.items);
        setSessions(sessionPage.items);
        setNextCursor(sessionPage.next_cursor);
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
  }, [companyId, reloadKey, router]);

  const refreshAfterMutation = () => {
    setBoxes(null);
    setSessions(null);
    setError(null);
    setReloadKey((k) => k + 1);
  };

  const retry = () => {
    setBoxes(null);
    setSessions(null);
    setError(null);
    setReloadKey((k) => k + 1);
  };

  const loadMoreSessions = async () => {
    if (!nextCursor || !companyId) return;
    setLoadingMore(true);
    try {
      const page = await api.listCountSessions({ company: companyId, limit: PAGE_SIZE, cursor: nextCursor });
      setSessions((prev) => [...(prev ?? []), ...page.items]);
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

  const openRegister = async () => {
    if (!companyId) return;
    setRegisterOpen(true);
    setRegisterError(null);
    setBoxName("");
    setBoxKind("store");
    setJournalId("");
    setCeilingToman(null);
    setJournalsLoading(true);
    try {
      const data = await api.listCashBoxStatementJournals({ company: companyId });
      setJournals(data);
    } catch (err: unknown) {
      setRegisterError(persianApiErrorMessage(err, "دریافت دفترهای روزانه ناموفق بود."));
    } finally {
      setJournalsLoading(false);
    }
  };

  const handleRegister = async () => {
    if (!companyId || !journalId) return;
    setRegistering(true);
    setRegisterError(null);
    try {
      await api.createCashBox({
        company_id: companyId,
        name_fa: boxName.trim(),
        kind: boxKind,
        statement_journal_id: Number(journalId),
        ceiling_irr: boxKind === "petty_cash" && ceilingToman !== null ? ceilingToman * 10 : null,
      });
      setRegisterOpen(false);
      refreshAfterMutation();
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setRegisterError(persianApiErrorMessage(err, "ثبت صندوق ناموفق بود."));
    } finally {
      setRegistering(false);
    }
  };

  const openStart = (box: CashBoxResponse) => {
    setStartBox(box);
    setCashier("");
    setSupervisor("");
    setStartError(null);
    setStartOpen(true);
  };

  const handleStart = async () => {
    if (!companyId || !startBox) return;
    setStarting(true);
    setStartError(null);
    try {
      const session = await api.startCountSession(startBox.id, {
        company_id: companyId,
        cashier: cashier.trim(),
        supervisor: supervisor.trim(),
      });
      setStartOpen(false);
      refreshAfterMutation();
      setSelectedSessionId(session.id);
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setStartError(persianApiErrorMessage(err, "شروع جلسهٔ شمارش ناموفق بود."));
    } finally {
      setStarting(false);
    }
  };

  const loading = boxes === null || sessions === null || companiesLoading;
  const combinedError = error ?? companiesError;

  if (combinedError && isPermissionDenied(combinedError)) {
    return (
      <PermissionDeniedState message={persianApiErrorMessage(combinedError, "دریافت صندوق‌ها ناموفق بود.")} />
    );
  }

  if (combinedError) {
    return (
      <ErrorState message={persianApiErrorMessage(combinedError, "دریافت صندوق‌ها ناموفق بود.")} onRetry={retry} />
    );
  }

  return (
    <div className="space-y-6" data-testid="cash-box-page">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">صندوق‌ها</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            هر صندوق روی یک دفتر روزانهٔ ترایتن است؛ شمارش با حضور ناظر انجام می‌شود و کسری یا اضافه فقط با
            صورتجلسهٔ امضاشده و تأیید مدیر در دفتر ثبت می‌شود.
          </p>
        </div>
        <Button type="button" onClick={() => void openRegister()} disabled={!companyId}>
          ثبت صندوق جدید
        </Button>
      </div>

      <CompanyPicker companies={companies} companyId={companyId} onChange={setCompanyId} />

      {loading ? (
        <div className="space-y-4" aria-label="در حال بارگذاری">
          <Skeleton className="h-32 w-full" />
          <Skeleton className="h-32 w-full" />
        </div>
      ) : (
        <>
          {boxes !== null && boxes.length === 0 ? (
            <Card>
              <CardContent className="py-8 text-center text-sm text-muted-foreground">
                هنوز صندوقی برای این شرکت ثبت نشده است. با «ثبت صندوق جدید» یک صندوق روی یک دفتر روزانه
                بسازید.
              </CardContent>
            </Card>
          ) : (
            <div className="grid gap-4 md:grid-cols-2">
              {(boxes ?? []).map((box) => (
                <Card key={box.id} data-testid={`cash-box-card-${box.id}`}>
                  <CardHeader>
                    <div className="flex flex-wrap items-start justify-between gap-2">
                      <CardTitle>{box.name_fa}</CardTitle>
                      <StatusBadge status={box.kind} label={cashBoxKindLabel(box.kind)} tone="outline" />
                    </div>
                  </CardHeader>
                  <CardContent className="space-y-3">
                    <dl className="grid gap-2 text-sm sm:grid-cols-2">
                      <div>
                        <dt className="text-muted-foreground">ماندهٔ دفتری</dt>
                        <dd className="mt-1 font-bold">
                          {box.book_balance_irr === null ? (
                            "نامشخص — حساب در ترایتن پیدا نشد"
                          ) : (
                            <MoneyDisplay amountIrr={box.book_balance_irr} />
                          )}
                        </dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground">آخرین شمارش</dt>
                        <dd className="mt-1">{daysSinceLabel(box.days_since_last_count)}</dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground">حساب صندوق</dt>
                        <dd className="mt-1" dir="ltr">
                          {box.cash_account_code}
                        </dd>
                      </div>
                      {box.ceiling_irr !== null ? (
                        <div>
                          <dt className="text-muted-foreground">سقف تنخواه</dt>
                          <dd className="mt-1">
                            <MoneyDisplay amountIrr={box.ceiling_irr} />
                          </dd>
                        </div>
                      ) : null}
                    </dl>
                    {box.uncounted_alert ? (
                      <p className="flex items-center gap-2 text-sm text-warning-foreground" role="alert">
                        <TriangleAlert className="size-4 shrink-0" strokeWidth={1.5} aria-hidden />
                        این صندوق مدتی است شمارش نشده است.
                      </p>
                    ) : null}
                    {!box.active ? (
                      <p className="text-sm text-muted-foreground">این صندوق غیرفعال است و شمارش نمی‌شود.</p>
                    ) : null}
                    <Button type="button" onClick={() => openStart(box)} disabled={!box.active}>
                      شروع شمارش
                    </Button>
                  </CardContent>
                </Card>
              ))}
            </div>
          )}

          <section aria-label="جلسات شمارش" className="space-y-4">
            <h2 className="text-lg font-bold">جلسات شمارش</h2>
            <DataTable
              columns={[
                {
                  key: "box",
                  header: "صندوق",
                  render: (row) => boxes?.find((b) => b.id === row.cash_box_id)?.name_fa ?? "—",
                },
                {
                  key: "created",
                  header: "شروع",
                  render: (row) => formatIsoDateTime(row.created_at),
                },
                { key: "cashier", header: "صندوقدار", render: (row) => row.cashier },
                { key: "supervisor", header: "ناظر", render: (row) => row.supervisor },
                {
                  key: "book",
                  header: "ماندهٔ دفتری",
                  render: (row) => <MoneyDisplay amountIrr={row.book_balance_irr} />,
                },
                {
                  key: "variance",
                  header: "مغایرت",
                  render: (row) => (
                    <StatusBadge
                      status={row.variance_irr === null ? "unknown" : String(row.variance_irr)}
                      label={varianceDirectionLabel(row.variance_irr)}
                      tone={varianceDirectionTone(row.variance_irr)}
                    />
                  ),
                },
                {
                  key: "status",
                  header: "وضعیت",
                  render: (row) => (
                    <StatusBadge
                      status={row.status}
                      label={sessionStatusLabel(row.status)}
                      tone={sessionStatusTone(row.status)}
                    />
                  ),
                },
              ]}
              rows={sessions ?? []}
              rowKey={(row) => row.id}
              loading={sessions === null}
              emptyText="هنوز جلسهٔ شمارشی ثبت نشده است."
              hasNextPage={nextCursor !== null}
              onNextPage={() => void loadMoreSessions()}
              loadingMore={loadingMore}
              onRowClick={(row) => setSelectedSessionId(row.id)}
            />
          </section>

          <CashBoxSettings />
        </>
      )}

      <Dialog
        open={registerOpen}
        onOpenChange={(isOpen) => {
          if (!isOpen) setRegisterOpen(false);
        }}
      >
        <DialogContent
          className="max-h-[90vh] overflow-y-auto"
          data-testid="cash-box-register-dialog"
          showCloseButton={false}
        >
          <DialogHeader>
            <DialogTitle>ثبت صندوق جدید</DialogTitle>
            <DialogDescription>
              صندوق روی یک دفتر روزانهٔ ترایتن ساخته می‌شود؛ حساب صندوق همان حساب دفتر روزانه است.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div>
              <Label htmlFor="box-name">نام صندوق</Label>
              <Input
                id="box-name"
                value={boxName}
                onChange={(event) => setBoxName(event.target.value)}
                placeholder="مثلاً صندوق فروشگاه"
              />
            </div>
            <div>
              <Label htmlFor="box-kind">نوع صندوق</Label>
              <Select value={boxKind} onValueChange={(v) => v && setBoxKind(v)}>
                <SelectTrigger id="box-kind">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {CASH_BOX_KINDS.map((kind) => (
                    <SelectItem key={kind} value={kind}>
                      {cashBoxKindLabel(kind)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label htmlFor="box-journal">دفتر روزانه</Label>
              {journalsLoading ? (
                <Skeleton className="h-10 w-full" />
              ) : (
                <Select value={journalId} onValueChange={(v) => v && setJournalId(v)}>
                  <SelectTrigger id="box-journal">
                    <SelectValue placeholder="یک دفتر روزانه انتخاب کنید" />
                  </SelectTrigger>
                  <SelectContent>
                    {journals.map((journal) => (
                      <SelectItem key={journal.id} value={String(journal.id)} disabled={!journal.active}>
                        {journal.name} — حساب {journal.account_code}
                        {journal.active ? "" : " (غیرفعال)"}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
              {journals.length === 0 && !journalsLoading ? (
                <p className="mt-1 text-sm text-muted-foreground">
                  برای این شرکت دفتر روزانه‌ای در ترایتن پیدا نشد؛ اول یک دفتر روزانه بسازید.
                </p>
              ) : null}
            </div>
            {boxKind === "petty_cash" ? (
              <div>
                <Label htmlFor="box-ceiling">سقف تنخواه</Label>
                <AmountInput id="box-ceiling" value={ceilingToman} onChange={setCeilingToman} min={1} />
              </div>
            ) : null}
            {registerError ? (
              <p className="text-sm text-destructive" role="alert">
                {registerError}
              </p>
            ) : null}
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setRegisterOpen(false)}>
              انصراف
            </Button>
            <Button
              type="button"
              onClick={() => void handleRegister()}
              disabled={registering || !boxName.trim() || !journalId}
            >
              ثبت صندوق
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={startOpen}
        onOpenChange={(isOpen) => {
          if (!isOpen) setStartOpen(false);
        }}
      >
        <DialogContent
          className="max-h-[90vh] overflow-y-auto"
          data-testid="cash-box-start-dialog"
          showCloseButton={false}
        >
          <DialogHeader>
            <DialogTitle>شروع شمارش {startBox ? `— ${startBox.name_fa}` : ""}</DialogTitle>
            <DialogDescription>
              شمارش در حضور ناظر انجام می‌شود؛ ناظر باید با صندوقدار متفاوت باشد. ماندهٔ دفتری در لحظهٔ
              شروع از ترایتن خوانده می‌شود و قابل تغییر نیست.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div>
              <Label htmlFor="start-cashier">صندوقدار</Label>
              <Input
                id="start-cashier"
                value={cashier}
                onChange={(event) => setCashier(event.target.value)}
                placeholder="نام صندوقدار"
              />
            </div>
            <div>
              <Label htmlFor="start-supervisor">ناظر</Label>
              <Input
                id="start-supervisor"
                value={supervisor}
                onChange={(event) => setSupervisor(event.target.value)}
                placeholder="نام ناظر (متفاوت از صندوقدار)"
              />
            </div>
            {startError ? (
              <p className="text-sm text-destructive" role="alert">
                {startError}
              </p>
            ) : null}
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setStartOpen(false)}>
              انصراف
            </Button>
            <Button
              type="button"
              onClick={() => void handleStart()}
              disabled={starting || !cashier.trim() || !supervisor.trim()}
            >
              شروع شمارش
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <CashBoxSessionDialog
        sessionId={selectedSessionId}
        boxes={boxes ?? []}
        open={selectedSessionId !== null}
        onClose={() => setSelectedSessionId(null)}
        onChanged={refreshAfterMutation}
      />
    </div>
  );
}
