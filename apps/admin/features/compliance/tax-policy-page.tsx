/**
 * Tax-policy view (US-405).
 *
 * Live versions from GET /api/v1/tax-policy/versions: every 1405
 * parameter with its rule/performance/filing years, effective dates
 * and verification badge. All seeded 1405 parameters are
 * CORROBORATED_CURRENT — each row says so, with the Persian reason,
 * and no rate is ever shown as verified production truth.
 *
 * The decision resolver (POST /api/v1/tax-policy/decisions) resolves
 * the active version for a date + rule. A production resolution of a
 * gated version is refused by the backend (422
 * TAX_POLICY_SOURCE_BLOCKED) and the UI shows that refusal in Persian.
 */

"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { ApiError, api, type TaxDecisionResponse, type TaxPolicyVersionResponse, type TaxSourceResponse } from "@/lib/api-client";
import { fa } from "@/lib/utils";
import { formatIsoDate } from "@/lib/format-date";
import { formatInteger } from "@/lib/format-money";
import { todayIso } from "@/lib/jalali";
import {
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  DataTable,
  JalaliDatePicker,
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
import { distinctTaxRuleKeys, formatTaxParameterValue } from "./compliance-data";
import {
  taxRuleLabel,
  verificationLabel,
  verificationReason,
  verificationTone,
} from "./labels";

/** Percent values render as Persian digits + ٪; IRR values as grouped Rial with the تومان note. */
function ParameterValue({ value, unit }: { value: string; unit: string }) {
  const formatted = formatTaxParameterValue(value, unit);
  if (formatted.isPercent) {
    return <span className="tabular-nums">{fa(value)}٪</span>;
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return <span dir="ltr">{value}</span>;
  return (
    <span className="tabular-nums">
      {formatInteger(numeric)} ریال ({formatInteger(Math.round(numeric / 10))} تومان)
    </span>
  );
}

const VERSION_COLUMNS: DataTableColumn<TaxPolicyVersionResponse>[] = [
  {
    key: "rule",
    header: "قاعده",
    render: (row) => taxRuleLabel(row.rule_key, row.rule_name),
  },
  {
    key: "value",
    header: "مقدار",
    render: (row) => <ParameterValue value={row.parameter_value} unit={row.value_unit} />,
  },
  {
    key: "years",
    header: "سال قاعده / عملکرد / اظهار",
    render: (row) => fa(`${row.rule_year} / ${row.performance_year ?? "—"} / ${row.filing_year ?? "—"}`),
  },
  {
    key: "effective",
    header: "اعتبار",
    render: (row) =>
      row.effective_from || row.effective_to
        ? `${formatIsoDate(row.effective_from) || "—"} تا ${formatIsoDate(row.effective_to) || "—"}`
        : "—",
  },
  {
    key: "verification",
    header: "وضعیت تأیید",
    render: (row) => (
      <StatusBadge
        status={row.verification_status}
        label={verificationLabel(row.verification_status)}
        tone={verificationTone(row.verification_status)}
      />
    ),
  },
  { key: "status", header: "وضعیت", render: (row) => (row.status === "active" ? "فعال" : "جایگزین‌شده") },
];

export function TaxPolicyPage() {
  const router = useRouter();
  const [versions, setVersions] = React.useState<TaxPolicyVersionResponse[] | null>(null);
  const [sources, setSources] = React.useState<TaxSourceResponse[] | null>(null);
  const [error, setError] = React.useState<unknown>(null);
  const [reloadKey, setReloadKey] = React.useState(0);

  const [ruleKey, setRuleKey] = React.useState("vat_general_rate");
  const [decisionDate, setDecisionDate] = React.useState<string | null>(() => todayIso());
  const [usageContext, setUsageContext] = React.useState("reference");
  const [resolving, setResolving] = React.useState(false);
  const [decision, setDecision] = React.useState<TaxDecisionResponse | null>(null);
  const [decisionError, setDecisionError] = React.useState<string | null>(null);

  React.useEffect(() => {
    let cancelled = false;
    Promise.all([api.listTaxVersions({ limit: 100 }), api.listTaxSources({ limit: 100 })])
      .then(([v, s]) => {
        if (cancelled) return;
        setVersions(v.items);
        setSources(s.items);
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
  }, [reloadKey, router]);

  if (error && isPermissionDenied(error)) {
    return <PermissionDeniedState message={persianApiErrorMessage(error, "دریافت سیاست مالیاتی ناموفق بود.")} />;
  }

  if (error) {
    return (
      <ErrorState
        message={persianApiErrorMessage(error, "دریافت سیاست مالیاتی ناموفق بود.")}
        onRetry={() => setReloadKey((k) => k + 1)}
      />
    );
  }

  const ruleKeys = versions ? distinctTaxRuleKeys(versions) : [];
  const gatedCount = (versions ?? []).filter((v) => v.verification_status !== "VERIFIED_TEXT").length;

  const resolveDecision = async () => {
    if (!decisionDate) {
      setDecisionError("تاریخ تصمیم را انتخاب کنید.");
      return;
    }
    setResolving(true);
    setDecision(null);
    setDecisionError(null);
    try {
      const result = await api.resolveTaxDecision({
        rule_key: ruleKey,
        decision_date: decisionDate,
        usage_context: usageContext,
      });
      setDecision(result);
    } catch (err: unknown) {
      setDecisionError(persianApiErrorMessage(err, "تعیین تصمیم مالیاتی ناموفق بود."));
    } finally {
      setResolving(false);
    }
  };

  return (
    <div className="space-y-6" data-testid="tax-policy-page">
      <div>
        <h1 className="text-2xl font-bold">سیاست مالیاتی</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          پارامترهای سالانه با سال قاعده، سال عملکرد، سال اظهار و تاریخ اعتبار جداگانه؛ هیچ نرخی در کد ثابت
          نشده و همهٔ مقادیر از رجیستری می‌آیند.
        </p>
      </div>

      {versions !== null && gatedCount > 0 ? (
        <div className="rounded-lg border border-warning/40 bg-warning/10 px-4 py-3" role="alert" data-testid="tax-gated-notice">
          <p className="text-sm font-medium text-warning-foreground">
            {fa(gatedCount)} پارامتر از {fa(versions.length)} پارامتر فقط با منبع مکمل تأیید شده‌اند.
          </p>
          <p className="mt-1 text-sm text-muted-foreground">
            {verificationReason("CORROBORATED_CURRENT")} این مقادیر نرخ تأییدشدهٔ تولیدی نیستند.
          </p>
        </div>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>نسخه‌های سیاست مالیاتی</CardTitle>
        </CardHeader>
        <CardContent>
          <DataTable
            columns={VERSION_COLUMNS}
            rows={versions ?? []}
            rowKey={(row) => row.id}
            loading={versions === null}
            emptyText="هنوز نسخه‌ای از سیاست مالیاتی ثبت نشده است."
            caption="مقادیر ریالی به ریال ذخیره می‌شوند؛ معادل تومانی فقط برای خوانایی نمایش داده می‌شود."
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>تعیین تصمیم مالیاتی</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm text-muted-foreground">
            نسخهٔ فعال یک قاعده را برای یک تاریخ مشخص تعیین می‌کند. «تولیدی» برای پارامترهای تأییدنشده مسدود
            است و بک‌اند آن را با خطا رد می‌کند؛ «مرجع» فقط مشاهده است.
          </p>
          <div className="grid gap-4 md:grid-cols-3">
            <div className="space-y-2">
              <Label htmlFor="tax-rule">قاعده</Label>
              <Select value={ruleKey} onValueChange={(v) => v && setRuleKey(v)}>
                <SelectTrigger id="tax-rule">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {ruleKeys.map((key) => (
                    <SelectItem key={key} value={key}>
                      {taxRuleLabel(key, versions?.find((v) => v.rule_key === key)?.rule_name)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="decision-date">تاریخ تصمیم</Label>
              <JalaliDatePicker id="decision-date" value={decisionDate} onChange={setDecisionDate} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="usage-context">مورد استفاده</Label>
              <Select value={usageContext} onValueChange={(v) => v && setUsageContext(v)}>
                <SelectTrigger id="usage-context">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="reference">مرجع (فقط مشاهده)</SelectItem>
                  <SelectItem value="production">تولیدی</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <Button onClick={resolveDecision} disabled={resolving || versions === null} data-testid="resolve-tax-decision">
            {resolving ? "در حال تعیین…" : "تعیین تصمیم"}
          </Button>
          {decisionError ? (
            <p className="text-sm text-destructive" role="alert">
              {decisionError}
            </p>
          ) : null}
          {decision ? (
            <div className="space-y-2 rounded-lg border p-4" data-testid="tax-decision-result">
              <p className="text-sm">
                قاعده: {taxRuleLabel(decision.rule_key, versions?.find((v) => v.rule_key === decision.rule_key)?.rule_name)} ·{" "}
                تاریخ: {formatIsoDate(decision.decision_date)} · مورد استفاده:{" "}
                {decision.usage_context === "production" ? "تولیدی" : "مرجع"}
              </p>
              <p className="text-sm">
                مقدار: <ParameterValue value={decision.parameter_value} unit={decision.value_unit} /> · نسخه:{" "}
                <span dir="ltr">{decision.version_id}</span>
              </p>
              <p className="flex flex-wrap items-center gap-2 text-sm">
                وضعیت تأیید نسخه:
                <StatusBadge
                  status={decision.verification_status}
                  label={verificationLabel(decision.verification_status)}
                  tone={verificationTone(decision.verification_status)}
                />
              </p>
              {verificationReason(decision.verification_status) ? (
                <p className="text-sm text-muted-foreground">{verificationReason(decision.verification_status)}</p>
              ) : null}
              <p className="text-xs text-muted-foreground">
                منبع: <span dir="ltr">{decision.source_key}</span> · ثبت: {formatIsoDate(decision.created_at.slice(0, 10))}
              </p>
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>منابع</CardTitle>
        </CardHeader>
        <CardContent>
          <DataTable
            columns={[
              { key: "key", header: "کلید منبع", render: (row) => <span dir="ltr">{row.source_key}</span> },
              { key: "title", header: "عنوان", render: (row) => row.title },
              { key: "authority", header: "مرجع", render: (row) => row.authority ?? "—" },
              { key: "versions", header: "نسخه‌ها", render: (row) => fa(row.versions_count), className: "text-end", headerClassName: "text-end" },
            ]}
            rows={sources ?? []}
            rowKey={(row) => row.id}
            loading={sources === null}
            emptyText="هنوز منبعی ثبت نشده است."
            caption="این منابع تأیید مکمل‌اند، نه متن رسمی قانون؛ تا وقتی تصویر رسمی پین نشود، پارامترها تولیدی نیستند."
          />
        </CardContent>
      </Card>
    </div>
  );
}
