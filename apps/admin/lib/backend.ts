/**
 * Server-side access to the HesabYar FastAPI backend (spec §19 rule 1).
 *
 * Only BFF route handlers import this module. The browser never learns
 * the backend origin and never holds credentials.
 */

import "server-only";

import http, { type IncomingMessage } from "node:http";
import https from "node:https";

/** FastAPI base URL. Override with HESABYAR_API_BASE_URL (default: local dev server). */
export function getBackendBaseUrl(): string {
  const raw = process.env.HESABYAR_API_BASE_URL ?? "http://127.0.0.1:8001";
  return raw.replace(/\/$/, "");
}

/**
 * Upstream calls time out after 120s. Tryton round-trips over a remote
 * DB are slow — a single stock-move do measured 41s and a 14-line
 * inventory confirm 53s against Supabase (US-515, 2026-11-18) — and
 * the 30s value aborted writes the engine had already committed,
 * surfacing false errors (the same class of failure as the Phase 4
 * journal e2e timeouts). Matches the engine client timeout
 * (TrytonSettings.timeout_seconds).
 */
const UPSTREAM_TIMEOUT_MS = 120_000;

/**
 * Long-running operations (bank reconcile/import) make several Tryton
 * round-trips at ~7s each against the remote dev DB; a reconcile
 * measures ~31s end to end, past the default timeout.
 */
export const LONG_UPSTREAM_TIMEOUT_MS = 120_000;

/**
 * The longest operations: a settlement deposit with inline tie-out
 * chains the match decision, the tie-out computation, the policy
 * gate's fiscal-period check, the wiring resolution (chart fetch),
 * and — since the late-clearing fix — a per-tranche clearing
 * transfer (payment create/submit/process/succeed + clearing-move
 * post + reconcile) followed by the bank receipt (draft + post +
 * reconcile): eight Tryton moves for a 4-tranche batch, dozens of
 * round-trips, measured at ~13 min end to end against the remote
 * dev DB (2026-11-19; the old ~100s figure predated the
 * per-tranche clearing, which failed fast before the sale seed
 * existed). The explicit tie-out run is the same chain minus the
 * match. A timeout here aborts a write the engine may already have
 * committed, surfacing a false error — so these get 30 min: the
 * full deposit (Tryton chain ~13 min + a post-Tryton flush/commit
 * tail measured under 10 min in runs 9/12) stays under ~23 min.
 * In production (local DB) the chain takes seconds; the ceiling
 * only binds on the slow remote dev DB.
 */
export const XL_UPSTREAM_TIMEOUT_MS = 1_800_000;

/** Delay before a GET retry (below). */
const GET_RETRY_DELAY_MS = 1_000;

export interface BackendCallOptions {
  method?: "GET" | "POST" | "PATCH" | "PUT";
  body?: unknown;
  idempotencyKey?: string;
  /** Per-call upstream timeout override (ms). Defaults to 120s. */
  timeoutMs?: number;
}

/**
 * Call the backend with an operator API key. Returns a Response so
 * callers can proxy status/body (including problem-details) unchanged.
 *
 * This uses node:http directly, NOT fetch: Node's global fetch
 * (undici) enforces its own default 300s `headersTimeout` on the time
 * to the first response headers, and it fires before the abort signal
 * on long engine calls — every successful settlement deposit (which
 * answers after ~11 min of engine work) died at ~5 min with a 502
 * while the backend kept working and committed (runs 8/9/12/13,
 * 2026-11-19). Passing a dispatcher from the `undici` package does
 * not help either: its Agent is protocol-incompatible with Node 24's
 * built-in fetch ("invalid onRequestStart method") and breaks every
 * call. node:http has no hidden response timeout; the AbortSignal is
 * the only bound.
 *
 * GETs are retried once on a 5xx or a timeout: against a remote database
 * the backend's per-request connection can die mid-request (the Supabase
 * pooler drops it — "SSL error: unexpected eof") and the retry opens a
 * fresh one. Writes are never retried here: only some carry an
 * idempotency key, so a retried write could apply twice.
 */
export async function backendFetch(
  path: string,
  apiKey: string,
  options: BackendCallOptions = {},
): Promise<Response> {
  const timeoutMs = options.timeoutMs ?? UPSTREAM_TIMEOUT_MS;
  if ((options.method ?? "GET") !== "GET") {
    return callBackend(path, apiKey, options, timeoutMs);
  }
  try {
    const response = await callBackend(path, apiKey, options, timeoutMs);
    if (response.status < 500) return response;
  } catch {
    // Timeout or transport failure — fall through to the retry.
  }
  await new Promise((resolve) => setTimeout(resolve, GET_RETRY_DELAY_MS));
  return callBackend(path, apiKey, options, timeoutMs);
}

/** One raw backend call over node:http(s); the body is wrapped in a Response. */
function callBackend(
  path: string,
  apiKey: string,
  options: BackendCallOptions,
  timeoutMs: number,
): Promise<Response> {
  const headers: Record<string, string> = { Authorization: `Bearer ${apiKey}` };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
  const url = new URL(`${getBackendBaseUrl()}/api/v1/${path}`);
  const lib = url.protocol === "https:" ? https : http;
  const body = options.body === undefined ? undefined : JSON.stringify(options.body);
  return new Promise<Response>((resolve, reject) => {
    const req = lib.request(
      url,
      {
        method: options.method ?? "GET",
        headers,
        signal: AbortSignal.timeout(timeoutMs),
      },
      (res: IncomingMessage) => {
        const chunks: Buffer[] = [];
        res.on("data", (chunk: Buffer) => chunks.push(chunk));
        res.on("end", () => {
          const responseHeaders = new Headers();
          for (const [name, value] of Object.entries(res.headers)) {
            if (value === undefined) continue;
            responseHeaders.set(name, Array.isArray(value) ? value[0] : value);
          }
          resolve(
            new Response(Buffer.concat(chunks), {
              status: res.statusCode ?? 500,
              headers: responseHeaders,
            }),
          );
        });
        res.on("error", reject);
      },
    );
    req.on("error", reject);
    if (body !== undefined) req.write(body);
    req.end();
  });
}
