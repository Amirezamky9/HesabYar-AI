/**
 * Client-side search normalization for the US-304 list pages.
 *
 * Filtering uses normalized values, never localized display strings
 * (spec §5.5 rule 10): Persian/Arabic-Indic digits become ASCII, Arabic
 * Yeh/Kaf become Persian ی/ک, and extra whitespace collapses — so a
 * search for «احمد» or «۱۲۳» matches regardless of how it was typed.
 */

import { en } from "@/lib/utils";

/** Normalize free text for comparison: ASCII digits, Persian ی/ک, single spaces. */
export function normalizeSearchText(value: string): string {
  return en(value)
    .replace(/[\u064A\u0649]/g, "ی") // Arabic Yeh U+064A + Alef Maksura U+0649 -> Persian Yeh U+06CC
    .replace(/[\u0643]/g, "ک") // Arabic Kaf U+0643 -> Persian Kaf U+06A9
    .replace(/‌/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();
}

/** True when `haystack` contains `query` after normalization. Empty query matches everything. */
export function matchesSearch(haystack: string, query: string): boolean {
  const q = normalizeSearchText(query);
  if (!q) return true;
  return normalizeSearchText(haystack).includes(q);
}
