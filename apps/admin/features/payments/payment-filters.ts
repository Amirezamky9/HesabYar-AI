/**
 * Client-side payment filtering (US-304): party search on the loaded
 * page, normalized (the API filters server-side by `kind`/`state` only).
 */

import type { PaymentResponse } from "@/lib/api-client";
import { matchesSearch } from "@/features/shared/search";

export function filterPaymentsByQuery(payments: PaymentResponse[], query: string): PaymentResponse[] {
  if (!query.trim()) return payments;
  return payments.filter(
    (p) =>
      matchesSearch(p.party_name, query) ||
      matchesSearch(p.number, query) ||
      matchesSearch(p.reference, query) ||
      matchesSearch(p.journal_name, query),
  );
}
