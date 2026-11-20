/**
 * Client-side statement filtering (US-304): name/journal search on the
 * loaded page, normalized (the API filters server-side by `state` only).
 */

import type { BankStatementResponse } from "@/lib/api-client";
import { matchesSearch } from "@/features/shared/search";

export function filterStatementsByQuery(
  statements: BankStatementResponse[],
  query: string,
): BankStatementResponse[] {
  if (!query.trim()) return statements;
  return statements.filter(
    (s) => matchesSearch(s.name, query) || matchesSearch(s.journal_name, query),
  );
}
