/**
 * Invoices list page body (US-304).
 *
 * Data comes from GET /api/v1/invoices through the BFF. State and type
 * filter server-side; party search filters the loaded page client-side
 * on normalized values (the API has no party-search parameter). Export
 * downloads the visible (filtered) list as CSV with normalized values.
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Download, Plus, Search } from "lucide-react";
import { ApiError, api, type InvoiceResponse } from "@/lib/api-client";
import { formatIsoDate } from "@/lib/format-date";
import { formatMoney } from "@/lib/format-money";
import { todayIso } from "@/lib/jalali";
import { fa } from "@/lib/utils";
import {
  Button,
  buttonVariants,
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
import { filterInvoicesByQuery } from "./invoice-filters";
import { invoicesToCsv } from "./invoice-csv";
import { INVOICE_STATES, INVOICE_TYPE_LABELS, INVOICE_TYPES } from "./labels";

const PAGE_SIZE = 50;

const COLUMNS: DataTableColumn<InvoiceResponse>[] = [
  {
    key: "number",
    header: "شماره",
    render: (inv) => (
      <span dir="ltr" className="ltr-isolate tabular-nums">
        {inv.number ?? `#${fa(inv.id)}`}
      </span>
    ),
  },
  { key: "party", header: "طرف حساب", render: (inv) => inv.party_name },
  {
    key: "type",
    header: "نوع",
    render: (inv) => INVOICE_TYPE_LABELS[inv.invoice_type as keyof typeof INVOICE_TYPE_LABELS] ?? inv.invoice_type,
  },
  {
    key: "date",
    header: "تاریخ",
    render: (inv) => formatIsoDate(inv.invoice_date) || "—",
  },
  { key: "state", header: "وضعیت", render: (inv) => <StatusBadge status={inv.state} /> },
  {
    key: "total",
    header: "مبلغ کل",
    render: (inv) => <span className="tabular-nums">{formatMoney(inv.total_amount)}</span>,
    className: "text-end",
    headerClassName: "text-end",
  },
];

export function InvoiceList() {
  const router = useRouter();
  // Report summary rows deep-link here with ?state= / ?type= / ?q=
  // (US-305). Read through useSearchParams — the page wraps this
  // component in <Suspense> so the param read never blocks prerendering
  // (Next 16 instant-shell rule). Unknown values fall back to "all".
  const searchParams = useSearchParams();
  const { companies, companyId, setCompanyId, loading: companiesLoading, error: companiesError } = useCompany();
  // null = not loaded yet (skeleton); the load effect never sets state
  // synchronously (react-hooks/set-state-in-effect), so resets happen in
  // event handlers (filter change, retry, company switch) instead.
  const [invoices, setInvoices] = React.useState<InvoiceResponse[] | null>(null);
  const [nextCursor, setNextCursor] = React.useState<string | null>(null);
  const [loadingMore, setLoadingMore] = React.useState(false);
  const [error, setError] = React.useState<unknown>(null);
  const [stateFilter, setStateFilter] = React.useState<string>(() => {
    const s = searchParams.get("state");
    return s && (INVOICE_STATES as readonly string[]).includes(s) ? s : "all";
  });
  const [typeFilter, setTypeFilter] = React.useState<string>(() => {
    const t = searchParams.get("type");
    return t && (INVOICE_TYPES as readonly string[]).includes(t) ? t : "all";
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
      .listInvoices({
        company: companyId,
        limit: PAGE_SIZE,
        state: stateFilter === "all" ? undefined : stateFilter,
        type: typeFilter === "all" ? undefined : typeFilter,
      })
      .then((page) => {
        if (cancelled) return;
        setInvoices(page.items);
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
  }, [companyId, stateFilter, typeFilter, reloadKey, router]);

  const loading = invoices === null;
  const visible = React.useMemo(() => filterInvoicesByQuery(invoices ?? [], query), [invoices, query]);

  /** Reset list state (event handler — allowed to set state synchronously). */
  const resetList = React.useCallback(() => {
    setError(null);
    setInvoices(null);
    setNextCursor(null);
  }, []);

  const handleNextPage = async () => {
    if (!companyId || !nextCursor) return;
    setLoadingMore(true);
    try {
      const page = await api.listInvoices({
        company: companyId,
        limit: PAGE_SIZE,
        cursor: nextCursor,
        state: stateFilter === "all" ? undefined : stateFilter,
        type: typeFilter === "all" ? undefined : typeFilter,
      });
      setInvoices((prev) => [...(prev ?? []), ...page.items]);
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
    downloadCsv(csvFilename("invoices", todayIso()), invoicesToCsv(visible));
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
    return <PermissionDeniedState message={persianApiErrorMessage(error, "دریافت فاکتورها ناموفق بود.")} />;
  }

  if (error) {
    return (
      <ErrorState
        message={persianApiErrorMessage(error, "دریافت فاکتورها ناموفق بود.")}
        onRetry={() => {
          resetList();
          setReloadKey((k) => k + 1);
        }}
      />
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">فاکتورها</h1>
          <p className="mt-1 text-sm text-muted-foreground">فاکتورهای فروش و خرید شرکت، از موتور حسابداری.</p>
        </div>
        {/* Plain Link styled as a button: Base UI's Button with
            render={<Link>} logs a nativeButton console error (US-305). */}
        <Link href="/invoices/new" className={buttonVariants({ variant: "default" })}>
          <Plus className="size-4" aria-hidden />
          فاکتور جدید
        </Link>
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
          <Label htmlFor="invoice-state-filter">وضعیت</Label>
          <Select
            value={stateFilter}
            onValueChange={(v) => {
              setStateFilter(v ?? "all");
              resetList();
            }}
          >
            <SelectTrigger id="invoice-state-filter" className="w-40">
              <SelectValue placeholder="همه" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">همه</SelectItem>
              {INVOICE_STATES.map((s) => (
                <SelectItem key={s} value={s}>
                  <StatusBadge status={s} />
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="invoice-type-filter">نوع</Label>
          <Select
            value={typeFilter}
            onValueChange={(v) => {
              setTypeFilter(v ?? "all");
              resetList();
            }}
          >
            <SelectTrigger id="invoice-type-filter" className="w-40">
              <SelectValue placeholder="همه" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">همه</SelectItem>
              {INVOICE_TYPES.map((t) => (
                <SelectItem key={t} value={t}>
                  {INVOICE_TYPE_LABELS[t]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="min-w-56 flex-1 space-y-1.5">
          <Label htmlFor="invoice-search">جستجو</Label>
          <div className="relative">
            <Search className="pointer-events-none absolute top-1/2 start-3 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              id="invoice-search"
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
        rowKey={(inv) => inv.id}
        loading={loading}
        emptyText={
          query ? "فاکتوری با این جستجو پیدا نشد." : "هنوز فاکتوری ثبت نشده است."
        }
        hasNextPage={nextCursor !== null}
        onNextPage={handleNextPage}
        loadingMore={loadingMore}
        onRowClick={(inv) => router.push(`/invoices/${inv.id}`)}
        caption={
          visible.length > 0
            ? `${fa(visible.length)} فاکتور نمایش داده می‌شود.`
            : undefined
        }
      />
    </div>
  );
}
