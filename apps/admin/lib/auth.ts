/**
 * Server-side session for the BFF (spec §4 line 252, §19 rule 1).
 *
 * The operator's API key (`hya_live_...`) is kept ONLY in this server-side
 * session store. The browser receives an opaque, random session id in an
 * httpOnly cookie — it never holds the key itself, not even encrypted.
 *
 * Storage is an in-memory Map: correct for a single-instance dev/admin
 * deployment. A multi-instance production deployment must swap this for a
 * shared store (Redis/DB) — the module boundary is the swap point.
 *
 * Sessions expire after 12h of inactivity. The store is swept lazily on
 * every read plus a 15-minute interval timer.
 */

import "server-only";

import { cookies } from "next/headers";
import { randomBytes } from "node:crypto";

export const SESSION_COOKIE = "hesabyar_session";

/** 12 hours of inactivity ends a session. */
const SESSION_TTL_MS = 12 * 60 * 60 * 1000;

export interface Session {
  /** The operator API key. NEVER serialize this to the browser. */
  apiKey: string;
  /** Last 4 chars of the key, safe to show in the UI («کلید …a1b2»). */
  keyLast4: string;
  /** Scopes granted to the key. Empty until the backend exposes key introspection. */
  scopes: string[];
  createdAt: number;
  lastSeenAt: number;
}

export interface SessionInfo {
  authenticated: boolean;
  keyLast4: string | null;
  createdAt: string | null;
}

const globalStore = globalThis as unknown as {
  __hesabyar_sessions?: Map<string, Session>;
};

/**
 * The session map MUST live on globalThis: in dev, Turbopack bundles this
 * module separately for route handlers and RSC rendering, so a plain
 * module-level Map would be two different maps (login sets the session
 * in one, the dashboard layout reads the other and redirects to /login).
 * globalThis is shared by every bundle in the process.
 */
const store: Map<string, Session> = (globalStore.__hesabyar_sessions ??= new Map<string, Session>());

// Sweep expired sessions every 15 minutes (guarded: dev HMR re-runs modules).
const SWEEP_FLAG = "__hesabyar_session_sweep__";
if (!(globalThis as Record<string, unknown>)[SWEEP_FLAG]) {
  (globalThis as Record<string, unknown>)[SWEEP_FLAG] = true;
  const timer = setInterval(() => {
    const now = Date.now();
    for (const [id, session] of store) {
      if (now - session.lastSeenAt > SESSION_TTL_MS) store.delete(id);
    }
  }, 15 * 60 * 1000);
  timer.unref?.();
}

function newSessionId(): string {
  return randomBytes(32).toString("base64url");
}

/** Create a session for a validated API key and set the httpOnly cookie. */
export async function createSession(apiKey: string): Promise<SessionInfo> {
  const id = newSessionId();
  const now = Date.now();
  const session: Session = {
    apiKey,
    keyLast4: apiKey.slice(-4),
    scopes: [],
    createdAt: now,
    lastSeenAt: now,
  };
  store.set(id, session);

  const jar = await cookies();
  jar.set(SESSION_COOKIE, id, {
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    secure: process.env.NODE_ENV === "production",
    maxAge: SESSION_TTL_MS / 1000,
  });

  return { authenticated: true, keyLast4: session.keyLast4, createdAt: new Date(now).toISOString() };
}

/** Read the current session (touching lastSeenAt). Null when absent/expired. */
export async function getSession(): Promise<Session | null> {
  const jar = await cookies();
  const id = jar.get(SESSION_COOKIE)?.value;
  if (!id) return null;
  const session = store.get(id);
  if (!session) return null;
  if (Date.now() - session.lastSeenAt > SESSION_TTL_MS) {
    store.delete(id);
    return null;
  }
  session.lastSeenAt = Date.now();
  return session;
}

/** Public session info for the client — never includes the key. */
export async function getSessionInfo(): Promise<SessionInfo> {
  const session = await getSession();
  if (!session) return { authenticated: false, keyLast4: null, createdAt: null };
  return {
    authenticated: true,
    keyLast4: session.keyLast4,
    createdAt: new Date(session.createdAt).toISOString(),
  };
}

/** Destroy the current session and clear the cookie. */
export async function destroySession(): Promise<void> {
  const jar = await cookies();
  const id = jar.get(SESSION_COOKIE)?.value;
  if (id) store.delete(id);
  jar.delete(SESSION_COOKIE);
}

/** Test helper: number of live sessions (dev/diagnostics only). */
export function sessionCount(): number {
  return store.size;
}
