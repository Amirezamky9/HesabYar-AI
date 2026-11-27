/**
 * Shared presentational bits for the installments center (US-545).
 *
 * Small pieces every installments page reuses: the plan/kind/leg
 * badges and the plan table columns (center list and the reports
 * share the same row shape). No data fetching here.
 */

import Link from "next/link";
import type {
  InstallmentLegResponse,
  InstallmentPlanResponse,
} from "@/lib/api-client";
import { MoneyDisplay, StatusBadge, type DataTableColumn } from "@/components/ui";
import { faNumber } from "@/lib/utils";
import { formatJalaliShort } from "../cheques/cheque-data";
import {
  legStatusLabel,
  legStatusTone,
  planKindLabel,
  planStatusLabel,
  planStatusTone,
} from "./installment-data";

export function PlanStatusBadge({ status }: { status: string }) {
  return <StatusBadge status={status} label={planStatusLabel(status)} tone={planStatusTone(status)} />;
}

export function PlanKindBadge({ kind }: { kind: string }) {
  return (
    <StatusBadge
      status={kind}
      label={planKindLabel(kind)}
      tone={kind === "bnpl" ? "warning" : "outline"}
    />
  );
}

export function LegStatusBadge({ status }: { status: string }) {
  return <StatusBadge status={status} label={legStatusLabel(status)} tone={legStatusTone(status)} />;
}

/** Shared columns for a plan list table (center + reports). */
export function planTableColumns(): DataTableColumn<InstallmentPlanResponse>[] {
  return [
    {
      key: "invoice",
      header: "فاکتور",
      render: (row) => (
        <Link
          href={`/installments/${row.id}`}
          className="font-medium text-primary underline-offset-4 hover:underline"
        >
          <span dir="ltr">{row.invoice_id}</span>
        </Link>
      ),
    },
    {
      key: "customer",
      header: "مشتری",
      render: (row) => row.customer_ref ?? "—",
    },
    {
      key: "kind",
      header: "نوع",
      render: (row) => <PlanKindBadge kind={row.kind} />,
    },
    {
      key: "legs",
      header: "اقساط",
      render: (row) => (
        <span>
          {faNumber(row.leg_count)} قسط
          {row.first_due_date ? (
            <span className="block text-xs text-muted-foreground">
              نخستین سررسید {formatJalaliShort(row.first_due_date)}
            </span>
          ) : null}
        </span>
      ),
    },
    {
      key: "principal",
      header: "اصل کل",
      render: (row) => <MoneyDisplay amountIrr={row.total_principal_irr} size="sm" />,
    },
    {
      key: "received",
      header: "دریافت‌شده",
      render: (row) => <MoneyDisplay amountIrr={row.total_received_irr} size="sm" />,
    },
    {
      key: "outstanding",
      header: "مانده",
      render: (row) => <MoneyDisplay amountIrr={row.outstanding_irr} size="sm" />,
    },
    {
      key: "status",
      header: "وضعیت",
      render: (row) => (
        <span>
          <PlanStatusBadge status={row.status} />
          {row.pending_clearance_irr > 0 ? (
            <span className="block text-xs font-medium text-warning-foreground">
              {formatPendingClearance(row.pending_clearance_irr)} در انتظار وصول چک
            </span>
          ) : null}
        </span>
      ),
    },
  ];
}

function formatPendingClearance(amountIrr: number): string {
  // Short money for the sub-line; the full figure is on the detail page.
  return `${faNumber(Math.round(amountIrr / 10))} تومان`;
}

/** Shared columns for a plan schedule table (detail + report 7). */
export function legTableColumns(options: {
  /** Show the linked cheque column (internal plans only). */
  showCheque?: boolean;
  /** Per-leg finance income, when the plan's contract rate is known. */
  incomeIrrOf?: (leg: InstallmentLegResponse) => number | null;
} = {}): DataTableColumn<InstallmentLegResponse>[] {
  const columns: DataTableColumn<InstallmentLegResponse>[] = [
    {
      key: "sequence",
      header: "قسط",
      render: (row) => faNumber(row.sequence),
    },
    {
      key: "due",
      header: "سررسید",
      render: (row) => formatJalaliShort(row.due_date),
    },
    {
      key: "principal",
      header: "اصل",
      render: (row) => <MoneyDisplay amountIrr={row.principal_irr} size="sm" />,
    },
    {
      key: "received",
      header: "دریافت‌شده",
      render: (row) => <MoneyDisplay amountIrr={row.received_irr} size="sm" />,
    },
    {
      key: "balance",
      header: "مانده",
      render: (row) => <MoneyDisplay amountIrr={row.balance_irr} size="sm" />,
    },
  ];
  if (options.incomeIrrOf) {
    const incomeIrrOf = options.incomeIrrOf;
    columns.push({
      key: "income",
      header: "سود",
      render: (row) => {
        const income = incomeIrrOf(row);
        return income === null ? "—" : <MoneyDisplay amountIrr={income} size="sm" />;
      },
    });
  }
  if (options.showCheque) {
    columns.push({
      key: "cheque",
      header: "چک مرتبط",
      render: (row) =>
        row.linked_cheque ? (
          <Link
            href={`/cheques/${row.linked_cheque.cheque_id}`}
            className="text-primary underline-offset-4 hover:underline"
            dir="ltr"
          >
            {row.linked_cheque.sayad_id}
            <span className="block text-xs text-muted-foreground" dir="rtl">
              سررسید {formatJalaliShort(row.linked_cheque.due_date)}
            </span>
          </Link>
        ) : (
          "—"
        ),
    });
  }
  columns.push({
    key: "status",
    header: "وضعیت",
    render: (row) => (
      <span>
        <LegStatusBadge status={row.status} />
        {row.pending_clearance_irr > 0 ? (
          <span className="block text-xs font-medium text-warning-foreground">
            {formatPendingClearance(row.pending_clearance_irr)} معلق
          </span>
        ) : null}
      </span>
    ),
  });
  return columns;
}
