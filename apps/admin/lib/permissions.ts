/**
 * API-key scopes (spec §14.2) — the 25-scope catalog the backend enforces.
 *
 * The backend is the authority: every BFF call can return 403 and the UI
 * must surface that as a readable Persian message. These helpers only
 * gate UI affordances (hide a button the key cannot use); they never
 * replace the server-side check.
 *
 * NOTE: the backend has no key-introspection endpoint yet, so the session
 * does not know the key's scopes at login. `SessionInfo.scopes` is empty
 * until the backend exposes it; until then, `hasScope` with an empty
 * granted set returns false and pages should rely on API errors instead
 * of pre-hiding actions. Do not invent scopes client-side.
 */

export const SCOPES = [
  "company:read",
  "account:read",
  "ledger:read",
  "ledger:draft:create",
  "ledger:draft:update",
  "ledger:post",
  "ledger:reverse",
  "invoice:read",
  "invoice:draft:create",
  "invoice:finalize",
  "payment:read",
  "banking:read",
  "banking:reconcile",
  "reports:read",
  "standards:read",
  "standards:validate",
  "compliance:read",
  "compliance:draft:create",
  "compliance:submit",
  "integrations:read",
  "integrations:manage",
  "api_keys:manage",
  "users:manage",
  "settings:manage",
  "mcp:connect",
] as const;

export type Scope = (typeof SCOPES)[number];

export function isScope(value: string): value is Scope {
  return (SCOPES as readonly string[]).includes(value);
}

/** True when every required scope is in the granted set. */
export function hasScope(granted: readonly string[], ...required: Scope[]): boolean {
  const set = new Set(granted);
  return required.every((s) => set.has(s));
}

/** True when at least one of the scopes is in the granted set. */
export function hasAnyScope(granted: readonly string[], ...candidates: Scope[]): boolean {
  const set = new Set(granted);
  return candidates.some((s) => set.has(s));
}
