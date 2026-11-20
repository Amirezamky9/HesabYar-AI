/**
 * Client-side invoice filtering (US-304).
 *
 * The API filters server-side by `type` and `state`; party search has
 * no API parameter, so it filters the loaded page client-side on the
 * normalized party name / number / reference (spec §5.5 rule 10:
 * filtering uses normalized values, never display strings).
 */

import type { InvoiceResponse } from "@/lib/api-client";
import { matchesSearch } from "@/features/shared/search";

export function filterInvoicesByQuery(invoices: InvoiceResponse[], query: string): InvoiceResponse[] {
  if (!query.trim()) return invoices;
  return invoices.filter(
    (inv) =>
      matchesSearch(inv.party_name, query) ||
      (inv.number !== null && matchesSearch(inv.number, query)) ||
      matchesSearch(inv.reference, query) ||
      matchesSearch(inv.description, query),
  );
}
