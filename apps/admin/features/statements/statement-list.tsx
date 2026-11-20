/**
 * Bank statements list page body (US-304).
 *
 * Data comes from GET /api/v1/bank/statements through the BFF, plus the
 * read-only import-status summary (GET /bank/statements/import-status):
 * per-state counts, the newest statement, matched/unmatched lines.
 * State filters server-side; name search filters the loaded page
 * client-side on normalized values. Export downloads the visible list
 * as CSV. The import action (US-514) links to the import wizard, which
 * uploads a bank-export file and registers the statement as a draft.
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Download, FileUp, Search } from "lucide-react";
import {
  ApiError,
  api,
  type BankStatementResponse,
  type StatementImportStatusResponse,
} from "@/lib/api-client";
import { formatIsoDate } from "@/lib/format-date";
import { formatMoney } from "@/lib/format-money";
import { todayIso } from "@/lib/jalali";
import { fa } from "@/lib/utils";
import {
  Button,
  buttonVariants,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
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
import { filterStatementsByQuery } from "./statement-filters";
import { statementsToCsv } from "./statement-csv";
import { STATEMENT_STATES } from "./labels";

const PAGE_SIZE = 50;

const COLUMNS: DataTableColumn<BankStatementResponse>[] = [
  { key: "name", header: "نام", render: (s) => s.name },
  { key: "journal", header: "دفتر صورت‌حساب", render: (s) => s.journal_name },
  { key: "date", header: "تاریخ", render: (s) => formatIsoDate(s.date) },
  { key: "state", header: "وضعیت", render: (s) => <StatusBadge status={s.state} /> },
  {
    key: "lines",
    header: "ردیف‌ها",
    render: (s) => <span className="tabular-nums">{fa(s.lines.length)}</span>,
  },
  {
    key: "balance",
    header: "مانده",
    render: (s) =>
      s.balance !== null ? <span className="tabular-nums">{formatMoney(s.balance)}</span> : "—",
    className: "text-end",
    headerClassName: "text-end",
  },
];

function ImportStatusSummary({ status }: { status: StatementImportStatusResponse }) {
  const counts = [
    { label: "همهٔ صورت‌حساب‌ها", value: status.total_statements },
    { label: "پیش‌نویس", value: status.draft_count },
    { label: "تأییدشده", value: status.validated_count },
    { label: "ثبت‌شده", value: status.posted_count },
    { label: "لغوشده", value: status.cancelled_count },
    { label: "ردیف‌های تطبیق‌یافته", value: status.matched_lines },
    { label: "ردیف‌های تطبیق‌نیافته", value: status.unmatched_lines },
  ];
  return (
    <Card>
      <CardHeader>
        <CardTitle>وضعیت ورود صورت‌حساب‌ها</CardTitle>
      </CardHeader>
      <CardContent>
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          {counts.map((c) => (
            <div key={c.label}>
              <dt className="text-sm text-muted-foreground">{c.label}</dt>
              <dd className="mt-1 text-xl font-bold tabular-nums">{fa(c.value)}</dd>
            </div>
          ))}
          <div>
            <dt className="text-sm text-muted-foreground">آخرین صورت‌حساب</dt>
            <dd className="mt-1 text-xl font-bold">
              {status.latest_statement_date ? formatIsoDate(status.latest_statement_date) : "—"}
            </dd>
          </div>
        </dl>
      </CardContent>
    </Card>
  );
}

export function StatementList() {
  const router = useRouter();
  const { companies, companyId, setCompanyId, loading: companiesLoading, error: companiesError } = useCompany();
  // null = not loaded yet (skeleton); the load effect never sets state
  // synchronously (react-hooks/set-state-in-effect), so resets happen in
  // event handlers (filter change, retry, company switch) instead.
  const [statements, setStatements] = React.useState<BankStatementResponse[] | null>(null);
  const [importStatus, setImportStatus] = React.useState<StatementImportStatusResponse | null>(null);
  const [nextCursor, setNextCursor] = React.useState<string | null>(null);
  const [loadingMore, setLoadingMore] = React.useState(false);
  const [error, setError] = React.useState<unknown>(null);
  const [stateFilter, setStateFilter] = React.useState<string>("all");
  const [query, setQuery] = React.useState("");
  const [reloadKey, setReloadKey] = React.useState(0);

  // First page + import-status summary load in the effect (setState only
  // in promise callbacks, like ConnectionProof — the set-state-in-effect
  // rule forbids calling a state-setting helper from an effect).
  // "Load more" is an event handler and keeps the existing summary.
  React.useEffect(() => {
    if (!companyId) return;
    let cancelled = false;
    Promise.all([
      api.listStatements({
        company: companyId,
        limit: PAGE_SIZE,
        state: stateFilter === "all" ? undefined : stateFilter,
      }),
      api.getStatementImportStatus(companyId),
    ])
      .then(([page, status]) => {
        if (cancelled) return;
        setStatements(page.items);
        setNextCursor(page.next_cursor);
        setImportStatus(status);
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
  }, [companyId, stateFilter, reloadKey, router]);

  const loading = statements === null;
  const visible = React.useMemo(() => filterStatementsByQuery(statements ?? [], query), [statements, query]);

  /** Reset list state (event handler — allowed to set state synchronously). */
  const resetList = React.useCallback(() => {
    setError(null);
    setStatements(null);
    setNextCursor(null);
  }, []);

  const handleNextPage = async () => {
    if (!companyId || !nextCursor) return;
    setLoadingMore(true);
    try {
      const page = await api.listStatements({
        company: companyId,
        limit: PAGE_SIZE,
        cursor: nextCursor,
        state: stateFilter === "all" ? undefined : stateFilter,
      });
      setStatements((prev) => [...(prev ?? []), ...page.items]);
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
    downloadCsv(csvFilename("statements", todayIso()), statementsToCsv(visible));
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
    return <PermissionDeniedState message={persianApiErrorMessage(error, "دریافت صورت‌حساب‌ها ناموفق بود.")} />;
  }

  if (error) {
    return (
      <ErrorState
        message={persianApiErrorMessage(error, "دریافت صورت‌حساب‌ها ناموفق بود.")}
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
        <h1 className="text-2xl font-bold">صورت‌حساب‌های بانکی</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          صورت‌حساب‌های بانک و صندوق شرکت، از موتور حسابداری.
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

      {importStatus ? <ImportStatusSummary status={importStatus} /> : null}

      <div className="flex flex-wrap items-end gap-4">
        <div className="space-y-1.5">
          <Label htmlFor="statement-state-filter">وضعیت</Label>
          <Select
            value={stateFilter}
            onValueChange={(v) => {
              setStateFilter(v ?? "all");
              resetList();
            }}
          >
            <SelectTrigger id="statement-state-filter" className="w-40">
              <SelectValue placeholder="همه" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">همه</SelectItem>
              {STATEMENT_STATES.map((s) => (
                <SelectItem key={s} value={s}>
                  <StatusBadge status={s} />
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="min-w-56 flex-1 space-y-1.5">
          <Label htmlFor="statement-search">جستجو</Label>
          <div className="relative">
            <Search className="pointer-events-none absolute top-1/2 start-3 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              id="statement-search"
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="نام صورت‌حساب یا دفتر…"
              className="ps-10"
            />
          </div>
        </div>
        <Button variant="outline" onClick={handleExport} disabled={visible.length === 0}>
          <Download className="size-4" />
          خروجی CSV
        </Button>
        <Link href="/banking/statements/import" className={buttonVariants({ variant: "default" })}>
          <FileUp className="size-4" aria-hidden />
          ایمپورت صورت‌حساب
        </Link>
      </div>

      <DataTable
        columns={COLUMNS}
        rows={visible}
        rowKey={(s) => s.id}
        loading={loading}
        emptyText={query ? "صورت‌حسابی با این جستجو پیدا نشد." : "هنوز صورت‌حساب بانکی ثبت نشده است."}
        hasNextPage={nextCursor !== null}
        onNextPage={handleNextPage}
        loadingMore={loadingMore}
        onRowClick={(s) => router.push(`/banking/statements/${s.id}`)}
        caption={visible.length > 0 ? `${fa(visible.length)} صورت‌حساب نمایش داده می‌شود.` : undefined}
      />
    </div>
  );
}
