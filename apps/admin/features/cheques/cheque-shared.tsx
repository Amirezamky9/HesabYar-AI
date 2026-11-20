/**
 * Shared presentational bits for the cheque center (US-535).
 *
 * Small, unstyled-logic-free pieces every cheque page reuses: the
 * state badge, the cheque table columns (registry, report 9 and the
 * intake queue share the same row shape), and the client-side filter
 * the list page applies on top of the server's filters (bank / party
 * / due-range / Sayyad status / source are not server filters).
 */

import Link from "next/link";
import type { ChequeResponse } from "@/lib/api-client";
import { MoneyDisplay, StatusBadge, type DataTableColumn } from "@/components/ui";
import {
  chequeKindLabel,
  chequePurposeLabel,
  chequeStateLabel,
  chequeStateTone,
  daysToDue,
  formatJalaliShort,
  storageLocationLabel,
  todayJalaliString,
} from "./cheque-data";

export function ChequeStateBadge({ state }: { state: string }) {
  return <StatusBadge status={state} label={chequeStateLabel(state)} tone={chequeStateTone(state)} />;
}

/** «سایت» when the row carries a source event, «دستی» otherwise (derived, not stored). */
export function chequeSourceLabel(cheque: ChequeResponse): string {
  return cheque.source_event_id ? "سایت" : "دستی";
}

/** Shared columns for a cheque registry table (list page, report 9). */
export function chequeTableColumns(todayJalali: string = todayJalaliString()): DataTableColumn<ChequeResponse>[] {
  return [
    {
      key: "sayad",
      header: "شناسهٔ صیاد",
      render: (row) => (
        <Link href={`/cheques/${row.id}`} className="font-mono text-sm text-primary underline-offset-4 hover:underline" dir="ltr">
          {row.sayad_id}
        </Link>
      ),
    },
    {
      key: "party",
      header: "طرف حساب",
      render: (row) => (
        <span>
          {row.kind === "received" ? row.issuer_name : row.payee_name}
          <span className="block text-xs text-muted-foreground">{row.issuer_bank}</span>
        </span>
      ),
    },
    {
      key: "amount",
      header: "مبلغ",
      render: (row) => <MoneyDisplay amountIrr={row.amount_irr} size="sm" />,
    },
    {
      key: "due",
      header: "سررسید",
      render: (row) => {
        const days = daysToDue(row.due_date, todayJalali);
        return (
          <span>
            {formatJalaliShort(row.due_date)}
            {days < 0 ? (
              <span className="block text-xs font-medium text-destructive">سررسیدگذشته</span>
            ) : days <= 7 && (row.state === "held" || row.state === "registered") ? (
              <span className="block text-xs font-medium text-warning-foreground">سررسید نزدیک</span>
            ) : null}
          </span>
        );
      },
    },
    {
      key: "state",
      header: "وضعیت",
      render: (row) => <ChequeStateBadge state={row.state} />,
    },
    {
      key: "kind-purpose",
      header: "نوع",
      render: (row) => (
        <span>
          {chequeKindLabel(row.kind)} · {chequePurposeLabel(row.purpose)}
        </span>
      ),
    },
    {
      key: "storage",
      header: "محل نگهداری",
      render: (row) => storageLocationLabel(row.storage_location),
    },
  ];
}

export interface ChequeClientFilters {
  /** Substring of the issuer bank display string ("056 سامان"). */
  bank?: string;
  /** Substring of issuer or payee name. */
  party?: string;
  /** Jalali YYYY-MM-DD lower bound (inclusive). */
  dueFrom?: string;
  /** Sayyad status code. */
  sayadStatus?: string;
  /** "site" | "manual" (derived from source_event_id). */
  source?: string;
}

/** Client-side filters over one fetched page (the list endpoint has no bank/date-range filters). */
export function applyChequeFilters(rows: ChequeResponse[], filters: ChequeClientFilters): ChequeResponse[] {
  return rows.filter((row) => {
    if (filters.bank && !row.issuer_bank.includes(filters.bank)) return false;
    if (filters.party && !`${row.issuer_name} ${row.payee_name}`.includes(filters.party)) return false;
    if (filters.dueFrom && row.due_date < filters.dueFrom) return false;
    if (filters.sayadStatus && row.sayad_status !== filters.sayadStatus) return false;
    if (filters.source === "site" && !row.source_event_id) return false;
    if (filters.source === "manual" && row.source_event_id) return false;
    return true;
  });
}
