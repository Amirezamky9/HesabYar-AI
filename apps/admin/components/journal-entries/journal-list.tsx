"use client";

/**
 * Journal list (US-303) — the company's journal drafts from
 * GET /accounting/journal-drafts, newest first.
 *
 * The endpoint returns drafts only (posted journals never appear) and
 * takes no state/period/search parameters, so the filters run
 * client-side over the loaded rows. Each row shows the Jalali date,
 * description, period, integer-IRR totals (Toman display, labelled)
 * and a state badge; clicking a row opens its detail page. The visible
 * (filtered) list exports as CSV (US-305).
 */

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Download, Plus } from "lucide-react";
import {
  api,
  ApiError,
  type CompanyResponse,
  type JournalDraftResponse,
} from "@/lib/api-client";
import { formatIsoDate } from "@/lib/format-date";
import { todayIso } from "@/lib/jalali";
import { apiErrorMessage } from "@/features/journal-entries/lib/error-messages";
import { journalsToCsv } from "@/features/journal-entries/lib/journal-csv";
import {
  ALL_PERIODS,
  filterJournals,
  periodOptions,
  type JournalStateFilter,
} from "@/features/journal-entries/lib/journal-filters";
import { fetchCompanies } from "@/features/journal-entries/lib/journal-api";
import { csvFilename, downloadCsv } from "@/features/shared/csv";
import {
  Button,
  buttonVariants,
  DataTable,
  Input,
  Label,
  MoneyDisplay,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  StatusBadge,
  type DataTableColumn,
} from "@/components/ui";

const PAGE_LIMIT = 50;

export function JournalList() {
  const router = useRouter();
  const [companies, setCompanies] = React.useState<CompanyResponse[] | null>(null);
  const [companyId, setCompanyId] = React.useState<string | null>(null);
  const [drafts, setDrafts] = React.useState<JournalDraftResponse[]>([]);
  const [nextCursor, setNextCursor] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [loadingMore, setLoadingMore] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const [stateFilter, setStateFilter] = React.useState<JournalStateFilter>("all");
  const [periodFilter, setPeriodFilter] = React.useState<string>(ALL_PERIODS);
  const [query, setQuery] = React.useState("");

  React.useEffect(() => {
    let cancelled = false;
    fetchCompanies()
      .then((items) => {
        if (cancelled) return;
        setCompanies(items);
        if (items.length > 0) setCompanyId(items[0].id);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.replace("/login");
          return;
        }
        setError(apiErrorMessage(err));
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

  // State changes only inside the promise chain (never synchronously in
  // an effect body) — see react-hooks/set-state-in-effect. Loading flags
  // are set by the event handlers that trigger a fetch.
  const fetchPage = React.useCallback(
    (company: string, cursor?: string) => {
      api
        .listJournalDrafts({ company, limit: PAGE_LIMIT, cursor })
        .then((page) => {
          setDrafts((current) => (cursor ? [...current, ...page.items] : page.items));
          setNextCursor(page.next_cursor);
        })
        .catch((err: unknown) => {
          if (err instanceof ApiError && err.isUnauthorized) {
            router.replace("/login");
            return;
          }
          setError(apiErrorMessage(err));
        })
        .finally(() => {
          setLoading(false);
          setLoadingMore(false);
        });
    },
    [router],
  );

  React.useEffect(() => {
    if (companyId) fetchPage(companyId);
  }, [companyId, fetchPage]);

  const handleCompanyChange = (value: string | null) => {
    if (!value) return;
    setCompanyId(value);
    setDrafts([]);
    setNextCursor(null);
    setError(null);
    setLoading(true);
  };

  const handleRetry = () => {
    if (!companyId) return;
    setError(null);
    setLoading(true);
    fetchPage(companyId);
  };

  const handleNextPage = () => {
    if (!companyId || !nextCursor) return;
    setLoadingMore(true);
    fetchPage(companyId, nextCursor);
  };

  const periods = React.useMemo(() => periodOptions(drafts), [drafts]);
  const visible = React.useMemo(
    () => filterJournals(drafts, { state: stateFilter, period: periodFilter, query }),
    [drafts, stateFilter, periodFilter, query],
  );

  /** Export the visible (filtered) drafts as CSV with normalized values. */
  const handleExport = () => {
    downloadCsv(csvFilename("journals", todayIso()), journalsToCsv(visible));
  };

  const columns: DataTableColumn<JournalDraftResponse>[] = [
    {
      key: "date",
      header: "تاریخ",
      render: (row) => formatIsoDate(row.date),
    },
    {
      key: "description",
      header: "شرح",
      render: (row) => row.description || "—",
    },
    {
      key: "period",
      header: "دوره",
      render: (row) => row.period.name,
    },
    {
      key: "total_debit",
      header: "بدهکار",
      render: (row) => <MoneyDisplay amountIrr={row.total_debit} size="sm" />,
    },
    {
      key: "total_credit",
      header: "بستانکار",
      render: (row) => <MoneyDisplay amountIrr={row.total_credit} size="sm" />,
    },
    {
      key: "state",
      header: "وضعیت",
      render: (row) => <StatusBadge status={row.state} />,
    },
    {
      key: "actions",
      header: "عملیات",
      render: (row) => (
        <Link
          href={`/accounting/journals/${row.id}`}
          className={buttonVariants({ variant: "ghost", size: "sm" })}
          onClick={(e) => e.stopPropagation()}
        >
          مشاهده
        </Link>
      ),
    },
  ];

  const emptyText =
    stateFilter === "posted"
      ? "سند ثبت‌شده‌ای در این فهرست نیست؛ فهرست پیش‌نویس‌ها فقط سندهای ثبت‌نشده را نشان می‌دهد."
      : drafts.length === 0
        ? "هنوز سندی ثبت نشده است."
        : "سندی با این فیلترها پیدا نشد.";

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">دفتر روزنامه</h1>
          <p className="mt-1 text-muted-foreground">
            پیش‌نویس‌های سند را بسازید، بررسی کنید و ثبت کنید.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" onClick={handleExport} disabled={visible.length === 0}>
            <Download className="size-4" aria-hidden />
            خروجی CSV
          </Button>
          <Link href="/accounting/journals/new" className={buttonVariants({ variant: "default" })}>
            <Plus className="size-4" aria-hidden />
            سند جدید
          </Link>
        </div>
      </div>

      <div className="grid gap-4 rounded-lg border bg-card p-4 sm:grid-cols-3">
        <div className="space-y-2">
          <Label htmlFor="journal-search">جست‌وجو</Label>
          <Input
            id="journal-search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="جست‌وجو در شرح یا شمارهٔ سند…"
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="journal-state-filter">وضعیت</Label>
          <Select
            value={stateFilter}
            onValueChange={(value) => setStateFilter(value as JournalStateFilter)}
          >
            <SelectTrigger id="journal-state-filter">
              <SelectValue placeholder="همهٔ وضعیت‌ها" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">همهٔ وضعیت‌ها</SelectItem>
              <SelectItem value="draft">پیش‌نویس</SelectItem>
              <SelectItem value="posted">ثبت‌شده</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-2">
          <Label htmlFor="journal-period-filter">دورهٔ مالی</Label>
          <Select value={periodFilter} onValueChange={(value) => setPeriodFilter(value ?? ALL_PERIODS)}>
            <SelectTrigger id="journal-period-filter">
              <SelectValue placeholder="همهٔ دوره‌ها" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL_PERIODS}>همهٔ دوره‌ها</SelectItem>
              {periods.map((period) => (
                <SelectItem key={period} value={period}>
                  {period}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      {companies !== null && companies.length > 1 ? (
        <div className="max-w-sm space-y-2">
          <Label htmlFor="journal-company-filter">شرکت</Label>
          <Select value={companyId ?? undefined} onValueChange={handleCompanyChange}>
            <SelectTrigger id="journal-company-filter">
              <SelectValue placeholder="انتخاب شرکت" />
            </SelectTrigger>
            <SelectContent>
              {companies.map((company) => (
                <SelectItem key={company.id} value={company.id}>
                  {company.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      ) : null}

      {error ? (
        <div className="space-y-4">
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
          <Button type="button" variant="outline" onClick={handleRetry}>
            تلاش دوباره
          </Button>
        </div>
      ) : (
        <DataTable
          columns={columns}
          rows={visible}
          rowKey={(row) => row.id}
          loading={loading}
          emptyText={emptyText}
          hasNextPage={nextCursor !== null}
          loadingMore={loadingMore}
          onNextPage={handleNextPage}
          onRowClick={(row) => router.push(`/accounting/journals/${row.id}`)}
          caption="سندها از جدید به قدیم مرتب شده‌اند."
        />
      )}
    </div>
  );
}
