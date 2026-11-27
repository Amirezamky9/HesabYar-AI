"use client";

/**
 * Chart of accounts page (US-302) — hierarchical tree from
 * GET /api/v1/accounting/accounts, search by code/name, type filter,
 * detail panel per account. Account codes are LTR-isolated identifiers
 * (§5.5 rule 8); filtering/sorting use normalized values, never display
 * strings (§5.5 rule 10). The whole chart is paged in (cursor
 * pagination, 135 accounts in the dev company) so the tree is complete.
 * Load state carries the company id it belongs to, so switching company
 * never shows another company's chart while the new one loads. The
 * visible (filtered) chart exports as CSV (US-305).
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import { ChevronDown, Download, Info, Search, TriangleAlert } from "lucide-react";
import { api, ApiError, type AccountResponse } from "@/lib/api-client";
import { todayIso } from "@/lib/jalali";
import { cn, faNumber } from "@/lib/utils";
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  DataTable,
  InputGroup,
  InputGroupAddon,
  InputGroupInput,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  Skeleton,
  StatusBadge,
} from "@/components/ui";
import { apiErrorMessage, useCompany } from "@/features/dashboard/company-context";
import { fetchAllPages } from "@/features/dashboard/dashboard-data";
import { csvFilename, downloadCsv } from "@/features/shared/csv";
import { accountsToCsv } from "./account-csv";
import { accountTypeLabel, hasAccountTypeLabel } from "./account-types";
import {
  accountTypeOptions,
  buildAccountTree,
  compareAccountCodes,
  filterAccounts,
  type AccountNode,
} from "./account-tree";

const TYPE_ALL = "all";
const TYPE_NONE = "__none__";

/** Type-filter select value -> the raw type to match (undefined = no filter). */
function selectValueToType(value: string): string | null | undefined {
  if (value === TYPE_ALL) return undefined;
  if (value === TYPE_NONE) return null;
  return value;
}

type AccountsState =
  | { kind: "loading"; companyId: string | null }
  | { kind: "ok"; companyId: string; accounts: AccountResponse[] }
  | { kind: "error"; companyId: string; message: string };

export function AccountsPage() {
  const router = useRouter();
  const { company, companyId, status: companyStatus, errorMessage: companyError, refresh } = useCompany();
  const [state, setState] = React.useState<AccountsState>({ kind: "loading", companyId: null });
  const [nonce, setNonce] = React.useState(0);
  const [query, setQuery] = React.useState("");
  const [typeValue, setTypeValue] = React.useState(TYPE_ALL);
  const [expanded, setExpanded] = React.useState<Set<number>>(new Set());
  const [selectedId, setSelectedId] = React.useState<number | null>(null);
  const [detailOpen, setDetailOpen] = React.useState(false);

  React.useEffect(() => {
    if (companyStatus !== "ready" || companyId === null) return;
    let cancelled = false;
    fetchAllPages((cursor) => api.listAccounts({ company: companyId, limit: 100, cursor }), {
      maxPages: 20,
      maxItems: 2000,
    })
      .then(({ items }) => {
        if (cancelled) return;
        setState({ kind: "ok", companyId, accounts: items });
        // Expand the root + the six top-level classes by default.
        const tree = buildAccountTree(items);
        const initial = new Set<number>();
        const collect = (node: AccountNode) => {
          if (node.depth <= 1) initial.add(node.account.id);
          for (const child of node.children) collect(child);
        };
        for (const root of tree) collect(root);
        setExpanded(initial);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.push("/login");
          return;
        }
        setState({
          kind: "error",
          companyId,
          message: apiErrorMessage(err, "دریافت سر فصل حساب‌ها ناموفق بود."),
        });
      });
    return () => {
      cancelled = true;
    };
  }, [companyStatus, companyId, nonce, router]);

  const retry = () => {
    setState({ kind: "loading", companyId });
    setNonce((n) => n + 1);
  };

  // Data for another company is stale: never render it.
  const stale = state.companyId !== companyId;
  const accounts = state.kind === "ok" && !stale ? state.accounts : null;

  const tree = React.useMemo(() => (accounts ? buildAccountTree(accounts) : []), [accounts]);
  const typeOptions = React.useMemo(() => (accounts ? accountTypeOptions(accounts) : []), [accounts]);
  const filteringActive = query.trim() !== "" || typeValue !== TYPE_ALL;
  const filtered = React.useMemo(
    () =>
      accounts
        ? filterAccounts(accounts, { query, type: selectValueToType(typeValue) }).sort((a, b) =>
            compareAccountCodes(a.code, b.code),
          )
        : [],
    [accounts, query, typeValue],
  );
  const byId = React.useMemo(() => new Map((accounts ?? []).map((a) => [a.id, a])), [accounts]);
  // Selection resolves against the CURRENT company's accounts, so a
  // company switch never shows another company's detail.
  const selected = selectedId !== null ? (byId.get(selectedId) ?? null) : null;
  const selectedParent = selected?.parent_id != null ? (byId.get(selected.parent_id) ?? null) : null;

  // Trigger label for the type filter (Base UI shows the raw value
  // while the popup is closed — see company-selector.tsx).
  const typeTriggerLabel =
    typeValue === TYPE_ALL
      ? "همهٔ انواع"
      : (() => {
          const option = typeOptions.find((o) => (o.value ?? TYPE_NONE) === typeValue);
          return option
            ? `${accountTypeLabel(option.value)} (${faNumber(option.count)})`
            : "همهٔ انواع";
        })();

  const toggleNode = (id: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  const selectAccount = (account: AccountResponse) => {
    setSelectedId(account.id);
    // Desktop shows the detail in the side panel; only small screens use
    // the sheet (lg breakpoint matches the aside's `hidden lg:block`).
    if (typeof window !== "undefined" && !window.matchMedia("(min-width: 1024px)").matches) {
      setDetailOpen(true);
    }
  };

  const clearFilters = () => {
    setQuery("");
    setTypeValue(TYPE_ALL);
  };

  /** Export the visible (filtered) chart as CSV with normalized values. */
  const handleExport = () => {
    downloadCsv(csvFilename("accounts", todayIso()), accountsToCsv(filtered));
  };

  if (companyStatus === "loading") {
    return (
      <div className="space-y-6" aria-label="در حال بارگذاری سر فصل حساب‌ها">
        <div className="space-y-2">
          <h1 className="text-2xl font-bold">سر فصل حساب‌ها</h1>
          <Skeleton className="h-4 w-72" />
        </div>
        <div className="space-y-2">
          {Array.from({ length: 8 }).map((_, i) => (
            <Skeleton key={i} className="h-11 w-full" />
          ))}
        </div>
      </div>
    );
  }

  if (companyStatus === "error") {
    return (
      <div className="space-y-4" role="alert">
        <h1 className="text-2xl font-bold">سر فصل حساب‌ها</h1>
        <p className="flex items-start gap-2 text-sm text-destructive">
          <TriangleAlert className="mt-1 size-4 shrink-0" aria-hidden />
          {companyError ?? "دریافت فهرست شرکت‌ها ناموفق بود."}
        </p>
        <Button variant="outline" onClick={refresh}>
          تلاش دوباره
        </Button>
      </div>
    );
  }

  if (companyStatus === "empty" || company === null) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-bold">سر فصل حساب‌ها</h1>
        <p className="text-sm text-muted-foreground">هیچ شرکتی در دسترس این کلید نیست.</p>
      </div>
    );
  }

  // Company is ready; the chart itself is still loading (or stale).
  if (state.kind === "loading" || stale) {
    return (
      <div className="space-y-6" aria-label="در حال بارگذاری سر فصل حساب‌ها">
        <div className="space-y-2">
          <h1 className="text-2xl font-bold">سر فصل حساب‌ها</h1>
          <Skeleton className="h-4 w-72" />
        </div>
        <div className="space-y-2">
          {Array.from({ length: 8 }).map((_, i) => (
            <Skeleton key={i} className="h-11 w-full" />
          ))}
        </div>
      </div>
    );
  }

  if (state.kind === "error") {
    return (
      <div className="space-y-4" role="alert">
        <h1 className="text-2xl font-bold">سر فصل حساب‌ها</h1>
        <p className="flex items-start gap-2 text-sm text-destructive">
          <TriangleAlert className="mt-1 size-4 shrink-0" aria-hidden />
          {state.message}
        </p>
        <Button variant="outline" onClick={retry}>
          تلاش دوباره
        </Button>
      </div>
    );
  }

  const total = accounts?.length ?? 0;
  const postableCount = accounts?.filter((a) => a.postable).length ?? 0;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">سر فصل حساب‌ها</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {company.name} · {faNumber(total)} حساب · {faNumber(postableCount)} حساب قابل‌ثبت
        </p>
      </div>

      <div className="flex flex-col gap-4 sm:flex-row">
        <div className="flex-1">
          <Label htmlFor="account-search" className="sr-only">
            جستجو در حساب‌ها
          </Label>
          <InputGroup>
            <InputGroupAddon>
              <Search className="size-4" aria-hidden />
            </InputGroupAddon>
            <InputGroupInput
              id="account-search"
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="جستجو با کد یا نام…"
              autoComplete="off"
            />
          </InputGroup>
        </div>
        <div className="sm:w-72">
          <Label htmlFor="account-type" className="sr-only">
            نوع حساب
          </Label>
          <Select
            value={typeValue}
            onValueChange={(value) => {
              if (value !== null) setTypeValue(value);
            }}
          >
            <SelectTrigger id="account-type" aria-label="نوع حساب" className="w-full">
              <SelectValue placeholder="همهٔ انواع">{typeTriggerLabel}</SelectValue>
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={TYPE_ALL}>همهٔ انواع</SelectItem>
              {typeOptions.map((option) => (
                <SelectItem key={option.value ?? TYPE_NONE} value={option.value ?? TYPE_NONE}>
                  {accountTypeLabel(option.value)} ({faNumber(option.count)})
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        {filteringActive ? (
          <Button variant="outline" onClick={clearFilters}>
            پاک کردن فیلترها
          </Button>
        ) : null}
        <Button variant="outline" onClick={handleExport} disabled={filtered.length === 0}>
          <Download className="size-4" aria-hidden />
          خروجی CSV
        </Button>
      </div>

      {total === 0 ? (
        <p className="rounded-lg border px-4 py-12 text-center text-sm text-muted-foreground">
          حسابی برای این شرکت ثبت نشده است؛ ابتدا نمودار حساب‌ها را نصب کنید.
        </p>
      ) : filteringActive ? (
        <div className="space-y-4">
          <p className="text-sm text-muted-foreground" role="status">
            {filtered.length === 0
              ? "حسابی با این مشخصات پیدا نشد."
              : `${faNumber(filtered.length)} حساب از ${faNumber(total)} حساب`}
          </p>
          {filtered.length > 0 ? (
            <DataTable<AccountResponse>
              columns={[
                {
                  key: "code",
                  header: "کد",
                  render: (row) =>
                    row.code ? (
                      <span className="ltr-isolate tabular-nums">{row.code}</span>
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    ),
                },
                { key: "name", header: "نام", render: (row) => row.name },
                {
                  key: "type",
                  header: "نوع",
                  render: (row) =>
                    hasAccountTypeLabel(row.type) ? (
                      accountTypeLabel(row.type)
                    ) : (
                      <span className="ltr-isolate text-sm">{row.type}</span>
                    ),
                },
                {
                  key: "postable",
                  header: "وضعیت",
                  render: (row) => (
                    <StatusBadge status={row.postable ? "active" : "inactive"} label={row.postable ? "قابل‌ثبت" : "غیرقابل‌ثبت"} />
                  ),
                },
                {
                  key: "actions",
                  header: "",
                  render: (row) => (
                    <Button variant="ghost" size="sm" onClick={() => selectAccount(row)}>
                      جزئیات
                    </Button>
                  ),
                },
              ]}
              rows={filtered}
              rowKey={(row) => row.id}
              onRowClick={selectAccount}
              emptyText="حسابی با این مشخصات پیدا نشد."
            />
          ) : (
            <Button variant="outline" onClick={clearFilters}>
              پاک کردن فیلترها
            </Button>
          )}
        </div>
      ) : (
        <div className="flex flex-col gap-6 lg:flex-row">
          <Card className="min-w-0 flex-1">
            <CardContent className="px-2 py-2">
              <ul className="space-y-1">
                {tree.map((node) => (
                  <AccountTreeNode
                    key={node.account.id}
                    node={node}
                    expanded={expanded}
                    selectedId={selectedId}
                    onToggle={toggleNode}
                    onSelect={selectAccount}
                  />
                ))}
              </ul>
            </CardContent>
          </Card>
          <aside className="hidden w-80 shrink-0 lg:block">
            <div className="sticky top-20">
              {selected ? (
                <AccountDetailCard account={selected} parent={selectedParent} />
              ) : (
                <Card>
                  <CardContent className="flex items-start gap-2 py-6 text-sm text-muted-foreground">
                    <Info className="mt-1 size-4 shrink-0" aria-hidden />
                    برای مشاهدهٔ جزئیات یک حساب، روی آن کلیک کنید.
                  </CardContent>
                </Card>
              )}
            </div>
          </aside>
        </div>
      )}

      {/* Mobile detail sheet (the desktop aside above is hidden on small screens). */}
      <Sheet open={detailOpen && selected !== null} onOpenChange={setDetailOpen}>
        <SheetContent side="right" className="w-80 p-0 sm:max-w-80">
          <SheetHeader className="border-b px-4 py-3">
            <SheetTitle className="text-start">جزئیات حساب</SheetTitle>
          </SheetHeader>
          {selected ? (
            <div className="overflow-y-auto px-4 py-4">
              <AccountDetailBody account={selected} parent={selectedParent} />
            </div>
          ) : null}
        </SheetContent>
      </Sheet>
    </div>
  );
}

function AccountTreeNode({
  node,
  expanded,
  selectedId,
  onToggle,
  onSelect,
}: {
  node: AccountNode;
  expanded: Set<number>;
  selectedId: number | null;
  onToggle: (id: number) => void;
  onSelect: (account: AccountResponse) => void;
}) {
  const { account } = node;
  const hasChildren = node.children.length > 0;
  const isExpanded = expanded.has(account.id);
  const isSelected = selectedId === account.id;

  return (
    <li>
      <div
        className={cn(
          "flex min-h-11 items-center gap-1 rounded-lg",
          isSelected && "bg-accent",
        )}
      >
        {hasChildren ? (
          <button
            type="button"
            aria-expanded={isExpanded}
            aria-label={isExpanded ? `بستن زیرمجموعه‌های ${account.name}` : `باز کردن زیرمجموعه‌های ${account.name}`}
            onClick={() => onToggle(account.id)}
            className="flex size-11 shrink-0 items-center justify-center rounded-lg hover:bg-accent"
          >
            <ChevronDown
              className={cn("size-4 transition-transform duration-150", !isExpanded && "rotate-90")}
              strokeWidth={2}
              aria-hidden
            />
          </button>
        ) : (
          <span className="size-11 shrink-0" aria-hidden />
        )}
        <button
          type="button"
          aria-current={isSelected ? "true" : undefined}
          onClick={() => onSelect(account)}
          className="flex min-w-0 flex-1 items-center gap-3 rounded-lg px-2 py-2 text-start"
        >
          <span className="ltr-isolate w-16 shrink-0 text-sm text-muted-foreground tabular-nums">
            {account.code || "—"}
          </span>
          <span className="min-w-0 flex-1 truncate text-sm font-medium">{account.name}</span>
          {!account.postable ? (
            <Badge variant="outline" className="shrink-0">
              غیرقابل‌ثبت
            </Badge>
          ) : null}
        </button>
      </div>
      {hasChildren && isExpanded ? (
        <ul className="ms-6 space-y-1 border-s ps-2">
          {node.children.map((child) => (
            <AccountTreeNode
              key={child.account.id}
              node={child}
              expanded={expanded}
              selectedId={selectedId}
              onToggle={onToggle}
              onSelect={onSelect}
            />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

function AccountDetailBody({
  account,
  parent,
}: {
  account: AccountResponse;
  parent: AccountResponse | null;
}) {
  return (
    <dl className="space-y-4 text-sm">
      <div>
        <dt className="text-muted-foreground">کد</dt>
        <dd className="ltr-isolate mt-1 font-medium tabular-nums">{account.code || "—"}</dd>
      </div>
      <div>
        <dt className="text-muted-foreground">نام</dt>
        <dd className="mt-1 font-medium">{account.name}</dd>
      </div>
      <div>
        <dt className="text-muted-foreground">نوع</dt>
        <dd className="mt-1 font-medium">
          {hasAccountTypeLabel(account.type) ? (
            accountTypeLabel(account.type)
          ) : (
            <span className="ltr-isolate">{account.type}</span>
          )}
        </dd>
      </div>
      <div>
        <dt className="text-muted-foreground">حساب والد</dt>
        <dd className="mt-1 font-medium">
          {parent ? (
            <>
              {parent.name}{" "}
              {parent.code ? <span className="ltr-isolate text-muted-foreground">({parent.code})</span> : null}
            </>
          ) : (
            "حساب ریشه"
          )}
        </dd>
      </div>
      <div>
        <dt className="text-muted-foreground">وضعیت</dt>
        <dd className="mt-2 flex flex-wrap gap-2">
          <StatusBadge status={account.postable ? "active" : "inactive"} label={account.postable ? "قابل‌ثبت" : "غیرقابل‌ثبت"} />
          <StatusBadge status={account.active ? "active" : "inactive"} label={account.active ? "فعال" : "غیرفعال"} />
          <StatusBadge status={account.closed ? "closed" : "open"} label={account.closed ? "بسته" : "باز"} />
          {account.party_required ? (
            <StatusBadge status="pending" label="نیازمند طرف حساب" />
          ) : null}
        </dd>
      </div>
    </dl>
  );
}

function AccountDetailCard({
  account,
  parent,
}: {
  account: AccountResponse;
  parent: AccountResponse | null;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{account.name}</CardTitle>
      </CardHeader>
      <CardContent>
        <AccountDetailBody account={account} parent={parent} />
      </CardContent>
    </Card>
  );
}
