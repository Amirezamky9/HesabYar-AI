/**
 * Small data-loading helpers for the journal pages (US-303).
 *
 * All calls go through the typed BFF client (lib/api-client); the browser
 * never talks to FastAPI or Tryton directly (spec §19 rule 1).
 */

import {
  api,
  type AccountResponse,
  type AuditEventResponse,
  type CompanyResponse,
} from "@/lib/api-client";

const PAGE_SIZE = 100;

/** Every company the operator key can see (usually one). */
export async function fetchCompanies(): Promise<CompanyResponse[]> {
  const page = await api.listCompanies({ limit: PAGE_SIZE });
  return page.items;
}

/**
 * The whole chart of accounts for one company, following cursors.
 * The picker needs the full chart (137 accounts in the dev company);
 * two pages at most.
 */
export async function fetchAllAccounts(companyId: string): Promise<AccountResponse[]> {
  const all: AccountResponse[] = [];
  let cursor: string | undefined;
  for (let page = 0; page < 10; page += 1) {
    const result = await api.listAccounts({ company: companyId, limit: PAGE_SIZE, cursor });
    all.push(...result.items);
    if (!result.next_cursor) break;
    cursor = result.next_cursor;
  }
  return all;
}

const JOURNAL_AUDIT_ACTIONS = ["ledger.draft.create", "ledger.post", "ledger.reverse"];

/**
 * Audit events for one journal, newest first. The audit list has no
 * resource filter, so each journal action is listed (limit 20) and the
 * events are matched client-side on `resource_id`. A key without the
 * `settings:manage` scope gets 403 — callers treat that as "no audit
 * panel", not an error.
 */
export async function fetchJournalAuditEvents(
  journalId: number,
  companyId: string,
): Promise<AuditEventResponse[]> {
  const id = String(journalId);
  const pages = await Promise.all(
    JOURNAL_AUDIT_ACTIONS.map((action) =>
      api.listAuditEvents({ action, company: companyId, limit: 20 }),
    ),
  );
  const events = pages.flatMap((page) => page.items).filter((event) => event.resource_id === id);
  events.sort((a, b) => (a.occurred_at < b.occurred_at ? 1 : -1));
  return events;
}
