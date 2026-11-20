/**
 * Client-side journal list filtering (US-303).
 *
 * The list endpoint (`GET /accounting/journal-drafts`) takes no state,
 * period or search parameters — it returns the company's draft moves
 * only, newest first — so filtering happens over the loaded rows, using
 * normalized values, never localized display strings (spec §5.5 rule 10).
 * Search matches the description and the engine id.
 */

import { en } from "@/lib/utils";
import type { AccountResponse, JournalDraftResponse } from "@/lib/api-client";

export type JournalStateFilter = "all" | "draft" | "posted";

export interface JournalFilters {
  state: JournalStateFilter;
  /** Fiscal period name, or "all". */
  period: string;
  /** Free-text search over description / id. */
  query: string;
}

export const ALL_PERIODS = "all";

/**
 * Normalize text for comparison: trim, collapse whitespace, unify Arabic
 * ي/ك with Persian ی/ک, and fold Persian/Arabic-Indic digits to ASCII.
 */
export function normalizeSearchText(text: string): string {
  return en(text)
    .replace(/[ي]/g, "ی")
    .replace(/[ك]/g, "ک")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();
}

export function filterJournals(
  drafts: JournalDraftResponse[],
  filters: JournalFilters,
): JournalDraftResponse[] {
  const query = normalizeSearchText(filters.query);
  return drafts.filter((draft) => {
    if (filters.state !== "all" && draft.state !== filters.state) return false;
    if (filters.period !== ALL_PERIODS && draft.period.name !== filters.period) return false;
    if (query) {
      const haystack = `${normalizeSearchText(draft.description)} ${draft.id}`;
      if (!haystack.includes(query)) return false;
    }
    return true;
  });
}

/** Distinct period names present in the loaded drafts, in first-seen order. */
export function periodOptions(drafts: JournalDraftResponse[]): string[] {
  const seen = new Set<string>();
  const options: string[] = [];
  for (const draft of drafts) {
    if (!seen.has(draft.period.name)) {
      seen.add(draft.period.name);
      options.push(draft.period.name);
    }
  }
  return options;
}

/**
 * Account-picker search: code prefix or name substring, normalized.
 * Only postable, active, open accounts are offered for journal lines.
 */
export function filterAccounts(accounts: AccountResponse[], query: string): AccountResponse[] {
  const q = normalizeSearchText(query);
  const postable = accounts.filter((a) => a.postable && a.active && !a.closed);
  if (!q) return postable;
  return postable.filter((a) => {
    const haystack = `${normalizeSearchText(a.code)} ${normalizeSearchText(a.name)}`;
    return haystack.includes(q);
  });
}

/** «1001 — صندوق» style label for an account (code stays LTR-isolated in the UI). */
export function accountLabel(account: AccountResponse): string {
  return `${account.code} — ${account.name}`;
}
