/**
 * Payments list page body (US-304). Read-only: payment create/approve
 * is not exposed by the API yet, so no action exists in this UI.
 *
 * Data comes from GET /api/v1/payments through the BFF. Kind and state
 * filter server-side; party search filters the loaded page client-side
 * on normalized values. Export downloads the visible list as CSV.
 */

"use client";

import * as React from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Download, Search } from "lucide-react";
import { ApiError, api, type PaymentResponse } from "@/lib/api-client";
import { formatIsoDate } from "@/lib/format-date";
import { formatMoney } from "@/lib/format-money";
import { todayIso } from "@/lib/jalali";
import { fa } from "@/lib/utils";
import {
  Button,
  DataTable,
  Input,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  StatusBadge,
  type DataTableColumn,
} from "@/components/ui";
import { CompanyPicker } from "@/features/shared/company-picker";
import { csvFilename, downloadCsv } from "@/features/shared/csv";
import { isPermissionDenied, persianApiErrorMessage } from "@/features/shared/api-error";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import { useCompany } from "@/features/shared/use-company";
import { filterPaymentsByQuery } from "./payment-filters";
import { paymentsToCsv } from "./payment-csv";
import { PAYMENT_KIND_LABELS, PAYMENT_KINDS, PAYMENT_STATE_LABELS, PAYMENT_STATES } from "./labels";

const PAGE_SIZE = 50;

const COLUMNS: DataTableColumn<PaymentResponse>[] = [
  {
    key: "number",
    header: "شماره",
    render: (p) => (
      <span dir="ltr" className="ltr-isolate tabular-nums">
        {p.number}
      </span>
    ),
  },
  { key: "party", header: "طرف حساب", render: (p) => p.party_name },
  { key: "kind", header: "نوع", render: (p) => PAYMENT_KIND_LABELS[p.kind] ?? p.kind },
  { key: "date", header: "تاریخ", render: (p) => formatIsoDate(p.date) },
  {
    key: "state",
    header: "وضعیت",
    render: (p) => {
      const info = PAYMENT_STATE_LABELS[p.state];
      return info ? <StatusBadge status={p.state} label={info.label} tone={info.tone} /> : <StatusBadge status={p.state} />;
    },
  },
  {
    key: "amount",
    header: "مبلغ",
    render: (p) => <span className="tabular-nums">{formatMoney(p.amount)}</span>,
    className: "text-end",
    headerClassName: "text-end",
  },
];

export function PaymentList() {
  const router = useRouter();
  // Report summary rows deep-link here with ?state= / ?kind= / ?q=
  // (US-305). Read through useSearchParams — the page wraps this
  // component in <Suspense> so the param read never blocks prerendering
  // (Next 16 instant-shell rule). Unknown values fall back to "all".
  const searchParams = useSearchParams();
  const { companies, companyId, setCompanyId, loading: companiesLoading, error: companiesError } = useCompany();
  // null = not loaded yet (skeleton); the load effect never sets state
  // synchronously (react-hooks/set-state-in-effect), so resets happen in
  // event handlers (filter change, retry, company switch) instead.
  const [payments, setPayments] = React.useState<PaymentResponse[] | null>(null);
  const [nextCursor, setNextCursor] = React.useState<string | null>(null);
  const [loadingMore, setLoadingMore] = React.useState(false);
  const [error, setError] = React.useState<unknown>(null);
  const [stateFilter, setStateFilter] = React.useState<string>(() => {
    const s = searchParams.get("state");
    return s && (PAYMENT_STATES as readonly string[]).includes(s) ? s : "all";
  });
  const [kindFilter, setKindFilter] = React.useState<string>(() => {
    const k = searchParams.get("kind");
    return k && (PAYMENT_KINDS as readonly string[]).includes(k) ? k : "all";
  });
  const [query, setQuery] = React.useState(() => searchParams.get("q") ?? "");
  const [reloadKey, setReloadKey] = React.useState(0);

  // First page loads in the effect (setState only in promise callbacks,
  // like ConnectionProof — the set-state-in-effect rule forbids calling a
  // state-setting helper from an effect). "Load more" is an event handler.
  React.useEffect(() => {
    if (!companyId) return;
    let cancelled = false;
    api
      .listPayments({
        company: companyId,
        limit: PAGE_SIZE,
        state: stateFilter === "all" ? undefined : stateFilter,
        kind: kindFilter === "all" ? undefined : kindFilter,
      })
      .then((page) => {
        if (cancelled) return;
        setPayments(page.items);
        setNextCursor(page.next_cursor);
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
  }, [companyId, stateFilter, kindFilter, reloadKey, router]);

  const loading = payments === null;
  const visible = React.useMemo(() => filterPaymentsByQuery(payments ?? [], query), [payments, query]);

  /** Reset list state (event handler — allowed to set state synchronously). */
  const resetList = React.useCallback(() => {
    setError(null);
    setPayments(null);
    setNextCursor(null);
  }, []);

  const handleNextPage = async () => {
    if (!companyId || !nextCursor) return;
    setLoadingMore(true);
    try {
      const page = await api.listPayments({
        company: companyId,
        limit: PAGE_SIZE,
        cursor: nextCursor,
        state: stateFilter === "all" ? undefined : stateFilter,
        kind: kindFilter === "all" ? undefined : kindFilter,
      });
      setPayments((prev) => [...(prev ?? []), ...page.items]);
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

  const handleExport = () => {
    downloadCsv(csvFilename("payments", todayIso()), paymentsToCsv(visible));
  };

  if (companiesLoading) {
    return (
      <div className="space-y-6" aria-label="در حال بارگذاری">
        <div className="h-8 w-40 animate-pulse rounded-lg bg-muted" />
        <div className="h-64 animate-pulse rounded-lg bg-muted" />
      </div>
    );
  }

  if (companiesError) {
    return (
      <ErrorState
        message={persianApiErrorMessage(companiesError, "دریافت شرکت‌ها ناموفق بود.")}
        onRetry={() => setReloadKey((k) => k + 1)}
      />
    );
  }

  if (companies.length === 0) {
    return <p className="text-sm text-muted-foreground">هیچ شرکتی در دسترس نیست.</p>;
  }

  if (error && isPermissionDenied(error)) {
    return <PermissionDeniedState message={persianApiErrorMessage(error, "دریافت پرداخت‌ها ناموفق بود.")} />;
  }

  if (error) {
    return (
      <ErrorState
        message={persianApiErrorMessage(error, "دریافت پرداخت‌ها ناموفق بود.")}
        onRetry={() => {
          resetList();
          setReloadKey((k) => k + 1);
        }}
      />
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">پرداخت‌ها</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          دریافت‌ها و پرداخت‌های شرکت، از موتور حسابداری. ثبت پرداخت جدید هنوز در دسترس نیست.
        </p>
      </div>

      <CompanyPicker
        companies={companies}
        companyId={companyId}
        onChange={(id) => {
          setCompanyId(id);
          resetList();
        }}
      />

      <div className="flex flex-wrap items-end gap-4">
        <div className="space-y-1.5">
          <Label htmlFor="payment-state-filter">وضعیت</Label>
          <Select
            value={stateFilter}
            onValueChange={(v) => {
              setStateFilter(v ?? "all");
              resetList();
            }}
          >
            <SelectTrigger id="payment-state-filter" className="w-40">
              <SelectValue placeholder="همه" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">همه</SelectItem>
              {PAYMENT_STATES.map((s) => {
                const info = PAYMENT_STATE_LABELS[s];
                return (
                  <SelectItem key={s} value={s}>
                    <StatusBadge status={s} label={info.label} tone={info.tone} />
                  </SelectItem>
                );
              })}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="payment-kind-filter">نوع</Label>
          <Select
            value={kindFilter}
            onValueChange={(v) => {
              setKindFilter(v ?? "all");
              resetList();
            }}
          >
            <SelectTrigger id="payment-kind-filter" className="w-40">
              <SelectValue placeholder="همه" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">همه</SelectItem>
              {PAYMENT_KINDS.map((k) => (
                <SelectItem key={k} value={k}>
                  {PAYMENT_KIND_LABELS[k]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="min-w-56 flex-1 space-y-1.5">
          <Label htmlFor="payment-search">جستجو</Label>
          <div className="relative">
            <Search className="pointer-events-none absolute top-1/2 start-3 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              id="payment-search"
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="نام طرف حساب، شماره یا مرجع…"
              className="ps-10"
            />
          </div>
        </div>
        <Button variant="outline" onClick={handleExport} disabled={visible.length === 0}>
          <Download className="size-4" />
          خروجی CSV
        </Button>
      </div>

      <DataTable
        columns={COLUMNS}
        rows={visible}
        rowKey={(p) => p.id}
        loading={loading}
        emptyText={query ? "پرداختی با این جستجو پیدا نشد." : "هنوز پرداختی ثبت نشده است."}
        hasNextPage={nextCursor !== null}
        onNextPage={handleNextPage}
        loadingMore={loadingMore}
        onRowClick={(p) => router.push(`/payments/${p.id}`)}
        caption={visible.length > 0 ? `${fa(visible.length)} پرداخت نمایش داده می‌شود.` : undefined}
      />
    </div>
  );
}
