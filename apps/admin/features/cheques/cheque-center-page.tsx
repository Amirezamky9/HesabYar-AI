/**
 * Cheque center — registry (US-535, Phase 5-4; report 9 / دفتر چک).
 *
 * The registry lists every cheque in the installation, newest first,
 * with server filters (state, kind, purpose, due-before) and
 * client-side filters (bank, party, due-from, Sayyad status, source)
 * — the list endpoint has no bank/date-range filter, so those run
 * over the fetched page and say so. Amounts are integer IRR; dates
 * are Jalali. Tabs split received / issued / collateral; the intake
 * queue lives on its own page (/cheques/intake).
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Download } from "lucide-react";
import { ApiError, api, type ChequeResponse } from "@/lib/api-client";
import {
  Button,
  Card,
  CardContent,
  DataTable,
  Input,
  JalaliDatePicker,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Skeleton,
  Tabs,
  TabsList,
  TabsTrigger,
} from "@/components/ui";
import { MoneyDisplay } from "@/components/ui";
import { faNumber } from "@/lib/utils";
import { formatMoney } from "@/lib/format-money";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import { isPermissionDenied, persianApiErrorMessage } from "@/features/shared/api-error";
import { buildCsv, downloadCsv } from "@/features/shared/csv";
import {
  CHEQUE_STATES,
  chequeStateLabel,
  isoToJalaliString,
  sumIrr,
  todayJalaliString,
} from "./cheque-data";
import { applyChequeFilters, chequeTableColumns } from "./cheque-shared";

const PAGE_SIZE = 50;

type TabId = "all" | "received" | "issued" | "collateral";

const TAB_PARAMS: Record<TabId, { kind?: string; purpose?: string }> = {
  all: {},
  received: { kind: "received" },
  issued: { kind: "issued" },
  collateral: { purpose: "collateral" },
};

function csvFilename(): string {
  return `cheques-${new Date().toISOString().slice(0, 10)}.csv`;
}

export function ChequeCenterPage() {
  const router = useRouter();
  const [tab, setTab] = React.useState<TabId>("all");
  const [stateFilter, setStateFilter] = React.useState<string>("all");
  const [bankFilter, setBankFilter] = React.useState("");
  const [partyFilter, setPartyFilter] = React.useState("");
  const [dueFromIso, setDueFromIso] = React.useState<string | null>(null);
  const [dueBeforeIso, setDueBeforeIso] = React.useState<string | null>(null);
  const [sayadFilter, setSayadFilter] = React.useState<string>("all");
  const [sourceFilter, setSourceFilter] = React.useState<string>("all");
  const [offset, setOffset] = React.useState(0);
  const [rows, setRows] = React.useState<ChequeResponse[]>([]);
  const [hasMore, setHasMore] = React.useState(false);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<unknown>(null);
  const todayJalali = React.useMemo(() => todayJalaliString(), []);

  const dueBeforeJalali = dueBeforeIso ? isoToJalaliString(dueBeforeIso) : undefined;
  const dueFromJalali = dueFromIso ? isoToJalaliString(dueFromIso) : undefined;

  const load = React.useCallback(() => {
    let cancelled = false;
    setLoading(true);
    api
      .listCheques({
        ...TAB_PARAMS[tab],
        state: stateFilter === "all" ? undefined : stateFilter,
        due_before: dueBeforeJalali,
        limit: PAGE_SIZE,
        offset,
      })
      .then((page) => {
        if (cancelled) return;
        setRows(page);
        setHasMore(page.length === PAGE_SIZE);
        setError(null);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.push("/login");
          return;
        }
        setError(err);
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [tab, stateFilter, dueBeforeJalali, offset, router]);

  // Loading state is set by load() from a timer callback, never
  // synchronously in the effect body (react-hooks/set-state-in-effect).
  React.useEffect(() => {
    let cleanup: (() => void) | undefined;
    const timer = setTimeout(() => {
      cleanup = load();
    }, 0);
    return () => {
      clearTimeout(timer);
      cleanup?.();
    };
  }, [load]);

  const visible = React.useMemo(
    () =>
      applyChequeFilters(rows, {
        bank: bankFilter || undefined,
        party: partyFilter || undefined,
        dueFrom: dueFromJalali,
        sayadStatus: sayadFilter === "all" ? undefined : sayadFilter,
        source: sourceFilter === "all" ? undefined : sourceFilter,
      }),
    [rows, bankFilter, partyFilter, dueFromJalali, sayadFilter, sourceFilter],
  );

  const summary = React.useMemo(
    () => ({
      count: rows.length,
      total: sumIrr(rows),
      held: sumIrr(rows.filter((r) => r.state === "held")),
      inClearing: sumIrr(rows.filter((r) => r.state === "in_clearing")),
      bouncedCount: rows.filter((r) => r.state === "bounced").length,
    }),
    [rows],
  );

  const resetPage = () => setOffset(0);

  const exportCsv = () => {
    downloadCsv(
      csvFilename(),
      buildCsv(
        [
          { header: "شناسهٔ صیاد", value: (r) => r.sayad_id },
          { header: "وضعیت", value: (r) => r.state },
          { header: "نوع", value: (r) => r.kind },
          { header: "هدف", value: (r) => r.purpose },
          { header: "صادرکننده", value: (r) => r.issuer_name },
          { header: "بانک صادرکننده", value: (r) => r.issuer_bank },
          { header: "مبلغ (ریال)", value: (r) => r.amount_irr },
          { header: "تاریخ صدور (شمسی)", value: (r) => r.issue_date },
          { header: "سررسید (شمسی)", value: (r) => r.due_date },
          { header: "وضعیت صیاد", value: (r) => r.sayad_status },
          { header: "محل نگهداری", value: (r) => r.storage_location ?? "" },
        ],
        visible,
      ),
    );
  };

  if (error && isPermissionDenied(error)) {
    return <PermissionDeniedState message="کلید شما دسترسی خواندن چک‌ها را ندارد." />;
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">مرکز چک</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            دفتر چک (گزارش ۹): همهٔ چک‌های دریافتی و پرداختی، تجاری و پشتوانه — پشتوانه هرگز با تجاری قاطی نمی‌شود.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={exportCsv} disabled={visible.length === 0}>
            <Download className="size-4" aria-hidden />
            خروجی CSV
          </Button>
          <Link href="/cheques/reports" className="inline-flex min-h-11 items-center rounded-md border px-4 text-sm font-medium">
            گزارش‌های چک
          </Link>
          <Link href="/cheques/new" className="inline-flex min-h-11 items-center rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground">
            ثبت چک جدید
          </Link>
        </div>
      </div>

      <Tabs value={tab} onValueChange={(value) => { setTab(value as TabId); resetPage(); }}>
        <TabsList aria-label="دستهٔ چک">
          <TabsTrigger value="all">همه</TabsTrigger>
          <TabsTrigger value="received">دریافتی</TabsTrigger>
          <TabsTrigger value="issued">پرداختی</TabsTrigger>
          <TabsTrigger value="collateral">پشتوانه</TabsTrigger>
        </TabsList>
      </Tabs>

      <Card>
        <CardContent className="space-y-4 pt-6">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <div className="space-y-2">
              <Label htmlFor="cheque-state-filter">وضعیت</Label>
              <Select
                value={stateFilter}
                onValueChange={(value) => { setStateFilter(value ?? "all"); resetPage(); }}
              >
                <SelectTrigger id="cheque-state-filter">
                  <SelectValue>{stateFilter === "all" ? "همهٔ وضعیت‌ها" : chequeStateLabel(stateFilter)}</SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">همهٔ وضعیت‌ها</SelectItem>
                  {CHEQUE_STATES.map((state) => (
                    <SelectItem key={state} value={state}>
                      {chequeStateLabel(state)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="cheque-bank-filter">بانک صادرکننده</Label>
              <Input
                id="cheque-bank-filter"
                value={bankFilter}
                onChange={(event) => setBankFilter(event.target.value)}
                placeholder="مثلاً سامان"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="cheque-party-filter">طرف حساب</Label>
              <Input
                id="cheque-party-filter"
                value={partyFilter}
                onChange={(event) => setPartyFilter(event.target.value)}
                placeholder="نام صادرکننده یا ذی‌نفع"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="cheque-due-from">سررسید از</Label>
              <JalaliDatePicker id="cheque-due-from" value={dueFromIso} onChange={setDueFromIso} placeholder="انتخاب تاریخ" />
            </div>
            <div className="space-y-2">
              <Label htmlFor="cheque-due-before">سررسید تا</Label>
              <JalaliDatePicker id="cheque-due-before" value={dueBeforeIso} onChange={setDueBeforeIso} placeholder="انتخاب تاریخ" />
            </div>
            <div className="space-y-2">
              <Label htmlFor="cheque-sayad-filter">وضعیت صیاد</Label>
              <Select value={sayadFilter} onValueChange={(v) => setSayadFilter(v ?? "all")}>
                <SelectTrigger id="cheque-sayad-filter">
                  <SelectValue>{sayadFilter === "all" ? "همه" : sayadFilter}</SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">همه</SelectItem>
                  <SelectItem value="unregistered">ثبت‌نشده در صیاد</SelectItem>
                  <SelectItem value="registered">ثبت‌شده در صیاد</SelectItem>
                  <SelectItem value="confirmed">تأییدشده</SelectItem>
                  <SelectItem value="transferred">منتقل‌شده</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="cheque-source-filter">مبدأ ثبت</Label>
              <Select value={sourceFilter} onValueChange={(v) => setSourceFilter(v ?? "all")}>
                <SelectTrigger id="cheque-source-filter">
                  <SelectValue>{sourceFilter === "all" ? "همه" : sourceFilter === "site" ? "سایت" : "دستی"}</SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">همه</SelectItem>
                  <SelectItem value="site">سایت</SelectItem>
                  <SelectItem value="manual">دستی</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <p className="text-xs text-muted-foreground">
            فیلترهای بانک، طرف حساب، «سررسید از»، وضعیت صیاد و مبدأ روی صفحهٔ دریافت‌شده اعمال می‌شوند؛ نقطهٔ پایانی فهرست این فیلترها را ندارد.
          </p>
        </CardContent>
      </Card>

      {error ? (
        <ErrorState message={persianApiErrorMessage(error, "دریافت فهرست چک‌ها ناموفق بود.")} onRetry={load} />
      ) : loading ? (
        <div className="space-y-2" aria-label="در حال بارگذاری">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      ) : (
        <>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Card>
              <CardContent className="pt-6">
                <p className="text-sm text-muted-foreground">تعداد چک‌ها (این صفحه)</p>
                <p className="mt-1 text-2xl font-bold tabular-nums">{faNumber(summary.count)}</p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-6">
                <p className="text-sm text-muted-foreground">مجموع مبالغ</p>
                <p className="mt-1"><MoneyDisplay amountIrr={summary.total} size="md" /></p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-6">
                <p className="text-sm text-muted-foreground">نزد ما + در وصول</p>
                <p className="mt-1"><MoneyDisplay amountIrr={summary.held + summary.inClearing} size="md" /></p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-6">
                <p className="text-sm text-muted-foreground">چک‌های برگشتی باز</p>
                <p className="mt-1 text-2xl font-bold tabular-nums">{faNumber(summary.bouncedCount)}</p>
                <Link href="/cheques/reports?report=bounced" className="text-sm font-medium text-primary hover:underline">
                  گزارش برگشتی‌ها
                </Link>
              </CardContent>
            </Card>
          </div>
          {hasMore ? (
            <p className="rounded-lg border border-warning/40 bg-warning/10 px-4 py-3 text-sm text-warning-foreground" role="note">
              بیش از {faNumber(PAGE_SIZE)} چک در این فیلتر هست؛ مجموع‌ها بر اساس {faNumber(PAGE_SIZE)} رکورد اول محاسبه شده‌اند.
            </p>
          ) : null}
          <DataTable
            columns={chequeTableColumns(todayJalali)}
            rows={visible}
            rowKey={(row) => row.id}
            emptyText={rows.length === 0 ? "هنوز چکی ثبت نشده است؛ از «ثبت چک جدید» اولین چک را ثبت کنید." : "با این فیلترها چکی پیدا نشد."}
            hasNextPage={hasMore}
            onNextPage={() => setOffset((o) => o + PAGE_SIZE)}
            onRowClick={(row) => router.push(`/cheques/${row.id}`)}
            caption={`دفتر چک — ${formatMoney(summary.total)} در ${faNumber(visible.length)} ردیف`}
          />
          {offset > 0 ? (
            <div className="flex justify-start">
              <Button variant="outline" onClick={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))}>
                صفحهٔ قبل
              </Button>
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}
