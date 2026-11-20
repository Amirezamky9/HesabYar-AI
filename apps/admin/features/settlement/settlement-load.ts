/**
 * Cursor-pagination helper for the settlement center (US-525).
 *
 * The settlement list endpoints cap pages at 100 rows; the report
 * aggregates whole lists, so it follows cursors until the list ends
 * (bounded: at most `maxPages` pages, so a huge installation cannot
 * hang the page — the report notes when it stopped early).
 */

import type { Paginated } from "@/lib/api-client";

export interface FetchAllResult<T> {
  items: T[];
  /** True when the list was cut off at `maxPages` (report is partial). */
  truncated: boolean;
}

export async function fetchAllPages<T>(
  fetchPage: (cursor?: string) => Promise<Paginated<T>>,
  maxPages = 10,
): Promise<FetchAllResult<T>> {
  const items: T[] = [];
  let cursor: string | undefined;
  let truncated = false;
  for (let page = 0; page < maxPages; page += 1) {
    const result = await fetchPage(cursor);
    items.push(...result.items);
    if (!result.next_cursor) return { items, truncated };
    cursor = result.next_cursor;
    if (page === maxPages - 1) truncated = true;
  }
  return { items, truncated };
}
