/**
 * Chart-of-accounts tree + filtering (US-302) — pure functions.
 *
 * The API returns a flat, cursor-paginated list (id, parent_id, code,
 * name, type). The page builds a tree from parent_id, sorts siblings by
 * normalized code, and filters by code/name search + type. All
 * comparisons use normalized values (ASCII digits, raw type strings) —
 * never localized display strings (§5.5 rule 10).
 */

import type { AccountResponse } from "@/lib/api-client";
import { en } from "@/lib/utils";

export interface AccountNode {
  account: AccountResponse;
  children: AccountNode[];
  depth: number;
}

/**
 * Compare account codes segment by segment, numerically («1.2.10» after
 * «1.2.9»). Empty codes (the chart root) sort first. Codes are
 * identifiers: normalized ASCII, never Persian digits.
 */
export function compareAccountCodes(a: string, b: string): number {
  if (a === b) return 0;
  if (a === "") return -1;
  if (b === "") return 1;
  const aParts = a.split(".");
  const bParts = b.split(".");
  const len = Math.max(aParts.length, bParts.length);
  for (let i = 0; i < len; i += 1) {
    const aPart = aParts[i];
    const bPart = bParts[i];
    if (aPart === undefined) return -1;
    if (bPart === undefined) return 1;
    const aNum = Number(aPart);
    const bNum = Number(bPart);
    if (Number.isInteger(aNum) && Number.isInteger(bNum) && aNum !== bNum) {
      return aNum - bNum;
    }
    const cmp = aPart.localeCompare(bPart);
    if (cmp !== 0) return cmp;
  }
  return 0;
}

/**
 * Build a forest from the flat list. Children are sorted by code.
 * Accounts whose parent is missing (should not happen) become roots so
 * they stay visible instead of vanishing.
 */
export function buildAccountTree(accounts: AccountResponse[]): AccountNode[] {
  const byId = new Map<number, AccountNode>();
  for (const account of accounts) {
    byId.set(account.id, { account, children: [], depth: 0 });
  }

  const roots: AccountNode[] = [];
  for (const node of byId.values()) {
    const parentId = node.account.parent_id;
    if (parentId === null) {
      roots.push(node);
      continue;
    }
    const parent = byId.get(parentId);
    if (!parent) {
      roots.push(node);
      continue;
    }
    parent.children.push(node);
  }

  // Depths in a second pass so the result never depends on list order.
  const assignDepth = (node: AccountNode, depth: number): void => {
    node.depth = depth;
    for (const child of node.children) assignDepth(child, depth + 1);
  };
  const sortNode = (node: AccountNode): void => {
    node.children.sort((a, b) => compareAccountCodes(a.account.code, b.account.code));
    for (const child of node.children) sortNode(child);
  };
  roots.sort((a, b) => compareAccountCodes(a.account.code, b.account.code));
  for (const root of roots) {
    assignDepth(root, 0);
    sortNode(root);
  }
  return roots;
}

export interface AccountFilter {
  /** Raw search input (digits normalized inside). */
  query: string;
  /** Raw type string to match; null matches untyped accounts; undefined = no filter. */
  type?: string | null;
}

/** Normalize a search string: Persian/Arabic-Indic digits -> ASCII, the
 * Persian decimal separator «٫» -> ".", trimmed. */
export function normalizeSearchQuery(query: string): string {
  return en(query).replace(/٫/g, ".").trim().toLowerCase();
}

/**
 * Filter accounts by code/name search + type. The query matches when it
 * appears in the normalized code or the (lowercased) name. Type matching
 * compares the raw type string.
 */
export function filterAccounts(accounts: AccountResponse[], filter: AccountFilter): AccountResponse[] {
  const query = normalizeSearchQuery(filter.query);
  const hasTypeFilter = filter.type !== undefined;
  return accounts.filter((account) => {
    if (hasTypeFilter && account.type !== filter.type) return false;
    if (query === "") return true;
    if (account.code.toLowerCase().includes(query)) return true;
    if (account.name.toLowerCase().includes(query)) return true;
    return false;
  });
}

export interface AccountTypeOption {
  /** Raw type string, or null for untyped accounts. */
  value: string | null;
  count: number;
}

/** Distinct types present in the loaded accounts, most common first. */
export function accountTypeOptions(accounts: AccountResponse[]): AccountTypeOption[] {
  const counts = new Map<string | null, number>();
  for (const account of accounts) {
    counts.set(account.type, (counts.get(account.type) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([value, count]) => ({ value, count }))
    .sort((a, b) => b.count - a.count);
}
