/**
 * Installments center — plan list (US-545, Phase 5-5 ۵ج).
 *
 * Every plan in the installation, newest first, with server filters
 * (kind, status, invoice) and a client-side customer filter — the
 * list endpoint has no customer filter, so that one runs over the
 * fetched page and says so. Tabs split internal / BNPL plans: the
 * two worlds are never summed together (§7.5 #11). Amounts are
 * integer IRR; dates are Jalali.
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Download } from "lucide-react";
import { ApiError, api, type InstallmentPlanResponse } from "@/lib/api-client";
import {
  Button,
  Card,
  CardContent,
  DataTable,
  Input,
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
import { applyPlanFilters, planStatusLabel, sumPlans } from "./installment-data";
import { planTableColumns } from "./installment-shared";

const PAGE_SIZE = 50;

type TabId = "all" | "internal" | "bnpl";

const TAB_PARAMS: Record<TabId, { kind?: string }> = {
  all: {},
  internal: { kind: "internal" },
  bnpl: { kind: "bnpl" },
};

const STATUS_OPTIONS = ["all", "active", "overdue", "paid"] as const;

function csvFilename(): string {
  return `installments-${new Date().toISOString().slice(0, 10)}.csv`;
}

export function InstallmentCenterPage() {
  const router = useRouter();
  const [tab, setTab] = React.useState<TabId>("all");
  const [statusFilter, setStatusFilter] = React.useState<string>("all");
  const [invoiceFilter, setInvoiceFilter] = React.useState("");
  const [invoiceQuery, setInvoiceQuery] = React.useState("");
  const [customerFilter, setCustomerFilter] = React.useState("");
  const [offset, setOffset] = React.useState(0);
  const [rows, setRows] = React.useState<InstallmentPlanResponse[]>([]);
  const [hasMore, setHasMore] = React.useState(false);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<unknown>(null);

  const load = React.useCallback(() => {
    let cancelled = false;
    setLoading(true);
    api
      .listInstallmentPlans({
        ...TAB_PARAMS[tab],
        status: statusFilter === "all" ? undefined : statusFilter,
        invoice_id: invoiceQuery || undefined,
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
  }, [tab, statusFilter, invoiceQuery, offset, router]);

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
    () => applyPlanFilters(rows, { customer: customerFilter || undefined }),
    [rows, customerFilter],
  );

  const summary = React.useMemo(() => sumPlans(rows), [rows]);
  const overdueCount = React.useMemo(
    () => rows.filter((r) => r.status === "overdue").length,
    [rows],
  );

  const resetPage = () => setOffset(0);

  const exportCsv = () => {
    downloadCsv(
      csvFilename(),
      buildCsv(
        [
          { header: "شناسهٔ برنامه", value: (r) => r.id },
          { header: "فاکتور", value: (r) => r.invoice_id },
          { header: "مرجع مشتری", value: (r) => r.customer_ref ?? "" },
          { header: "نوع", value: (r) => r.kind },
          { header: "درگاه", value: (r) => r.gateway_channel_key ?? "" },
          { header: "وضعیت", value: (r) => r.status },
          { header: "تعداد اقساط", value: (r) => r.leg_count },
          { header: "اصل کل (ریال)", value: (r) => r.total_principal_irr },
          { header: "دریافت‌شده (ریال)", value: (r) => r.total_received_irr },
          { header: "مانده (ریال)", value: (r) => r.outstanding_irr },
          { header: "در انتظار وصول چک (ریال)", value: (r) => r.pending_clearance_irr },
          { header: "نخستین سررسید (شمسی)", value: (r) => r.first_due_date ?? "" },
        ],
        visible,
      ),
    );
  };

  if (error && isPermissionDenied(error)) {
    return <PermissionDeniedState message="کلید شما دسترسی خواندن برنامه‌های قسط را ندارد." />;
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">مرکز اقساط</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            برنامه‌های قسط داخلی و BNPL — دو دنیا جدا نگه‌داشته می‌شوند و هرگز با هم جمع نمی‌شوند.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={exportCsv} disabled={visible.length === 0}>
            <Download className="size-4" aria-hidden />
            خروجی CSV
          </Button>
          <Link
            href="/installments/reports?report=overdue"
            className="inline-flex min-h-11 items-center rounded-md border px-4 text-sm font-medium"
          >
            اقساط معوق
          </Link>
          <Link
            href="/installments/reports"
            className="inline-flex min-h-11 items-center rounded-md border px-4 text-sm font-medium"
          >
            گزارش‌های اقساط
          </Link>
          <Link
            href="/installments/new"
            className="inline-flex min-h-11 items-center rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground"
          >
            برنامهٔ جدید
          </Link>
        </div>
      </div>

      <Tabs
        value={tab}
        onValueChange={(value) => {
          setTab(value as TabId);
          resetPage();
        }}
      >
        <TabsList aria-label="نوع برنامه">
          <TabsTrigger value="all">همه</TabsTrigger>
          <TabsTrigger value="internal">داخلی</TabsTrigger>
          <TabsTrigger value="bnpl">BNPL</TabsTrigger>
        </TabsList>
      </Tabs>

      <Card>
        <CardContent className="space-y-4 pt-6">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <div className="space-y-2">
              <Label htmlFor="installment-status-filter">وضعیت</Label>
              <Select
                value={statusFilter}
                onValueChange={(value) => {
                  setStatusFilter(value ?? "all");
                  resetPage();
                }}
              >
                <SelectTrigger id="installment-status-filter">
                  <SelectValue>
                    {statusFilter === "all" ? "همهٔ وضعیت‌ها" : planStatusLabel(statusFilter)}
                  </SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">همهٔ وضعیت‌ها</SelectItem>
                  {STATUS_OPTIONS.filter((s) => s !== "all").map((status) => (
                    <SelectItem key={status} value={status}>
                      {planStatusLabel(status)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="installment-invoice-filter">فاکتور</Label>
              <div className="flex gap-2">
                <Input
                  id="installment-invoice-filter"
                  value={invoiceFilter}
                  onChange={(event) => setInvoiceFilter(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      setInvoiceQuery(invoiceFilter.trim());
                      resetPage();
                    }
                  }}
                  placeholder="شناسهٔ فاکتور"
                  dir="ltr"
                />
                <Button
                  variant="outline"
                  onClick={() => {
                    setInvoiceQuery(invoiceFilter.trim());
                    resetPage();
                  }}
                >
                  جستجو
                </Button>
              </div>
            </div>
            <div className="space-y-2">
              <Label htmlFor="installment-customer-filter">مشتری</Label>
              <Input
                id="installment-customer-filter"
                value={customerFilter}
                onChange={(event) => setCustomerFilter(event.target.value)}
                placeholder="مرجع مشتری"
              />
            </div>
          </div>
          <p className="text-xs text-muted-foreground">
            فیلتر مشتری روی صفحهٔ دریافت‌شده اعمال می‌شود؛ نقطهٔ پایانی فهرست این فیلتر را ندارد. وضعیت «معوق» یعنی
            دست‌کم یک قسط سررسیدگذشته دارد؛ برنامه‌ای که فقط اقساط در انتظار وصول چک دارد معوق نیست.
          </p>
        </CardContent>
      </Card>

      {error ? (
        <ErrorState
          message={persianApiErrorMessage(error, "دریافت فهرست برنامه‌های قسط ناموفق بود.")}
          onRetry={load}
        />
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
                <p className="text-sm text-muted-foreground">تعداد برنامه‌ها (این صفحه)</p>
                <p className="mt-1 text-2xl font-bold tabular-nums">{faNumber(rows.length)}</p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-6">
                <p className="text-sm text-muted-foreground">اصل کل</p>
                <p className="mt-1">
                  <MoneyDisplay amountIrr={summary.principalIrr} size="md" />
                </p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-6">
                <p className="text-sm text-muted-foreground">ماندهٔ کل</p>
                <p className="mt-1">
                  <MoneyDisplay amountIrr={summary.outstandingIrr} size="md" />
                </p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-6">
                <p className="text-sm text-muted-foreground">برنامه‌های معوق</p>
                <p className="mt-1 text-2xl font-bold tabular-nums">{faNumber(overdueCount)}</p>
                <Link
                  href="/installments/reports?report=overdue"
                  className="text-sm font-medium text-primary hover:underline"
                >
                  گزارش اقساط معوق
                </Link>
              </CardContent>
            </Card>
          </div>
          {hasMore ? (
            <p
              className="rounded-lg border border-warning/40 bg-warning/10 px-4 py-3 text-sm text-warning-foreground"
              role="note"
            >
              بیش از {faNumber(PAGE_SIZE)} برنامه در این فیلتر هست؛ مجموع‌ها بر اساس {faNumber(PAGE_SIZE)} رکورد اول
              محاسبه شده‌اند.
            </p>
          ) : null}
          <DataTable
            columns={planTableColumns()}
            rows={visible}
            rowKey={(row) => row.id}
            emptyText={
              rows.length === 0
                ? "هنوز برنامهٔ قسطی ثبت نشده است؛ از «برنامهٔ جدید» اولین برنامه را بسازید."
                : "با این فیلترها برنامه‌ای پیدا نشد."
            }
            hasNextPage={hasMore}
            onNextPage={() => setOffset((o) => o + PAGE_SIZE)}
            onRowClick={(row) => router.push(`/installments/${row.id}`)}
            caption={`مرکز اقساط — ${formatMoney(summary.outstandingIrr)} مانده در ${faNumber(visible.length)} ردیف`}
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
