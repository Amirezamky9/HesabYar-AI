/**
 * Standards registry view (US-405).
 *
 * Live list from GET /api/v1/standards: every standard with its latest
 * revision's verification badge. Standard 15 carries the
 * BLOCKED_SOURCE_GAP badge with its Persian reason; 43/44 carry
 * VERIFIED_TEXT. Rows link to the detail page (revisions + rules).
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ApiError, api, type StandardListItemResponse } from "@/lib/api-client";
import { fa } from "@/lib/utils";
import { formatIsoDate } from "@/lib/format-date";
import {
  Badge,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  DataTable,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  StatusBadge,
  type DataTableColumn,
} from "@/components/ui";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import { isPermissionDenied, persianApiErrorMessage } from "@/features/shared/api-error";
import { verificationLabel, verificationReason, verificationTone } from "./labels";

const STATUS_FILTERS = [
  { value: "all", label: "همهٔ وضعیت‌ها" },
  { value: "VERIFIED_TEXT", label: "متن تأییدشده" },
  { value: "CORROBORATED_CURRENT", label: "تأیید با منبع مکمل" },
  { value: "UNVERIFIED", label: "تأییدنشده" },
  { value: "BLOCKED_SOURCE_GAP", label: "مسدود — کمبود منبع" },
];

const COLUMNS: DataTableColumn<StandardListItemResponse>[] = [
  {
    key: "code",
    header: "شماره",
    render: (row) => (
      <Link href={`/compliance/standards/${row.standard_code}`} className="font-medium hover:underline">
        {fa(row.standard_code)}
      </Link>
    ),
  },
  {
    key: "title",
    header: "عنوان",
    render: (row) => (
      <Link href={`/compliance/standards/${row.standard_code}`} className="hover:underline">
        {row.title}
      </Link>
    ),
  },
  {
    key: "verification",
    header: "وضعیت تأیید",
    render: (row) => (
      <StatusBadge
        status={row.latest_verification_status ?? ""}
        label={verificationLabel(row.latest_verification_status)}
        tone={verificationTone(row.latest_verification_status)}
      />
    ),
  },
  {
    key: "effective",
    header: "اعتبار بازبینی",
    render: (row) =>
      row.latest_effective_from || row.latest_effective_to
        ? `${formatIsoDate(row.latest_effective_from) || "—"} تا ${formatIsoDate(row.latest_effective_to) || "—"}`
        : "—",
  },
  { key: "rules", header: "قاعده‌ها", render: (row) => fa(row.rules_count), className: "text-end", headerClassName: "text-end" },
];

export function StandardsPage() {
  const router = useRouter();
  const [filter, setFilter] = React.useState("all");
  const [standards, setStandards] = React.useState<StandardListItemResponse[] | null>(null);
  const [error, setError] = React.useState<unknown>(null);
  const [reloadKey, setReloadKey] = React.useState(0);

  React.useEffect(() => {
    let cancelled = false;
    api
      .listStandards({ limit: 100, ...(filter === "all" ? {} : { verification_status: filter }) })
      .then((page) => {
        if (cancelled) return;
        setStandards(page.items);
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
  }, [filter, reloadKey, router]);

  if (error && isPermissionDenied(error)) {
    return <PermissionDeniedState message={persianApiErrorMessage(error, "دریافت استانداردها ناموفق بود.")} />;
  }

  if (error) {
    return (
      <ErrorState
        message={persianApiErrorMessage(error, "دریافت استانداردها ناموفق بود.")}
        onRetry={() => setReloadKey((k) => k + 1)}
      />
    );
  }

  const blocked = (standards ?? []).filter((s) => s.latest_verification_status === "BLOCKED_SOURCE_GAP");

  return (
    <div className="space-y-6" data-testid="standards-page">
      <div>
        <h1 className="text-2xl font-bold">استانداردهای حسابداری</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          رجیستری استانداردهای ایران با وضعیت تأیید هر بازبینی؛ برای جزئیات هر استاندارد، روی ردیف آن کلیک کنید.
        </p>
      </div>

      {blocked.length > 0 ? (
        <div
          className="space-y-2 rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3"
          role="alert"
          data-testid="standards-blocked-notice"
        >
          <p className="text-sm font-medium text-destructive">
            {fa(blocked.length)} استاندارد مسدود است:{" "}
            {blocked.map((s) => `استاندارد ${fa(s.standard_code)}`).join("، ")}
          </p>
          {blocked.map((s) => (
            <p key={s.standard_code} className="text-sm text-muted-foreground">
              استاندارد {fa(s.standard_code)} — {verificationReason(s.latest_verification_status)}
            </p>
          ))}
        </div>
      ) : null}

      <Card>
        <CardHeader className="flex flex-wrap items-center justify-between gap-4">
          <CardTitle>فهرست استانداردها</CardTitle>
          <div className="flex items-center gap-2">
            <Label htmlFor="verification-filter">وضعیت تأیید</Label>
            <Select value={filter} onValueChange={(v) => v && setFilter(v)}>
              <SelectTrigger id="verification-filter" className="w-56">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {STATUS_FILTERS.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </CardHeader>
        <CardContent>
          <DataTable
            columns={COLUMNS}
            rows={standards ?? []}
            rowKey={(row) => row.standard_code}
            loading={standards === null}
            emptyText="استانداردی با این وضعیت ثبت نشده است."
            onRowClick={(row) => router.push(`/compliance/standards/${row.standard_code}`)}
            caption="وضعیت «مسدود» یعنی متن رسمی بازبینی‌شده هنوز وارد نشده و استفاده برای دوره‌های جاری ممنوع است."
          />
        </CardContent>
      </Card>

      <p className="text-sm text-muted-foreground">
        <Badge variant="outline">منبع: رجیستری استانداردها</Badge> تنها استاندارد ۱ دارای قواعد ماشین‌خوان
        (۳۶ قاعده) است؛ بقیهٔ استانداردها فعلاً متن مرجع‌اند.
      </p>
    </div>
  );
}
