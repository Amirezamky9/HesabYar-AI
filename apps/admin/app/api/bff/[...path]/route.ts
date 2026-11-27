/**
 * BFF catch-all proxy — the ONLY way the browser reaches the backend
 * (spec §19 rule 1, §4 line 252).
 *
 *   /api/bff/companies            -> {backend}/api/v1/companies
 *   /api/bff/accounting/accounts  -> {backend}/api/v1/accounting/accounts
 *
 * The route requires a session, attaches the operator API key server-side,
 * forwards the query string, JSON body and Idempotency-Key, and proxies
 * the backend's status/body (including RFC 9457 problem-details)
 * unchanged. The browser never learns the backend origin.
 */

import { NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import {
  LONG_UPSTREAM_TIMEOUT_MS,
  XL_UPSTREAM_TIMEOUT_MS,
  backendFetch,
} from "@/lib/backend";

/**
 * Paths whose backend work makes several Tryton round-trips and
 * legitimately exceeds the default 120s upstream timeout (US-514:
 * a bank reconcile measures ~31s against the remote dev DB;
 * the chart-of-accounts list measures ~33s — the same slowness
 * behind the known journal e2e failures).
 */
const LONG_TIMEOUT_PATTERNS = [
  /\/reconcile$/,
  /\/statements\/imports$/,
  /\/confirm$/,
  // US-525: an import approval re-matches every row, and a file
  // upload parses up to 2,000 rows — both legitimately slow.
  /\/settlements\/imports\/[^/]+\/approve$/,
  /^settlements\/imports(\?|$)/,
  // NB: the target has no leading slash ("accounting/accounts?..."),
  // so this one is start-anchored instead of slash-prefixed.
  /^accounting\/accounts(\?|$)/,
];

/**
 * Paths whose backend work chains dozens of Tryton round-trips
 * and measures ~13 min end to end against the remote dev DB
 * (2026-11-19; the old ~100s figure predated the per-tranche
 * late-clearing fix): a settlement deposit with inline tie-out
 * (match + tie-out + gate + wiring + 4 clearings + 4 bank
 * receipts) and the explicit tie-out run (the same chain minus
 * the match). These get the 30 min ceiling — the full deposit
 * measures under ~23 min (Tryton chain ~13 min + post-Tryton
 * flush/commit tail under 10 min, runs 9/12) — a timeout here
 * would abort a write the engine may already have committed.
 */
const XL_TIMEOUT_PATTERNS = [
  /^settlements\/deposits(\?|$)/,
  /\/tie-out$/,
];


type RouteParams = { params: Promise<{ path: string[] }> };

function problem(status: number, code: string, detail: string) {
  return NextResponse.json(
    {
      type: `https://hesabyar.local/problems/${code.toLowerCase()}`,
      title: code,
      status,
      detail,
      code,
    },
    { status },
  );
}

async function handle(request: Request, { params }: RouteParams, method: "GET" | "POST" | "PATCH" | "PUT" | "DELETE") {
  const session = await getSession();
  if (!session) {
    return problem(401, "UNAUTHORIZED", "Not signed in.");
  }

  const { path } = await params;
  const url = new URL(request.url);
  const target = [...path].map(encodeURIComponent).join("/") + url.search;

  let body: unknown;
  if (method !== "GET") {
    const text = await request.text();
    if (text) {
      try {
        body = JSON.parse(text) as unknown;
      } catch {
        return problem(400, "BAD_REQUEST", "Invalid JSON body.");
      }
    }
  }

  let upstream: Response;
  try {
    const timeoutMs = XL_TIMEOUT_PATTERNS.some((pattern) => pattern.test(target))
      ? XL_UPSTREAM_TIMEOUT_MS
      : LONG_TIMEOUT_PATTERNS.some((pattern) => pattern.test(target))
        ? LONG_UPSTREAM_TIMEOUT_MS
        : undefined;
    upstream = await backendFetch(target, session.apiKey, {
      method,
      body,
      idempotencyKey: request.headers.get("Idempotency-Key") ?? undefined,
      ...(timeoutMs !== undefined ? { timeoutMs } : {}),
    });
  } catch {
    return problem(502, "BACKEND_UNREACHABLE", "The HesabYar backend did not answer.");
  }

  // Proxy status + body + content type unchanged (problem-details included).
  const payload = await upstream.arrayBuffer();
  const headers = new Headers();
  const contentType = upstream.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  const requestId = upstream.headers.get("x-request-id");
  if (requestId) headers.set("x-request-id", requestId);
  return new NextResponse(payload, { status: upstream.status, headers });
}

export async function GET(request: Request, ctx: RouteParams) {
  return handle(request, ctx, "GET");
}

export async function POST(request: Request, ctx: RouteParams) {
  return handle(request, ctx, "POST");
}

export async function PATCH(request: Request, ctx: RouteParams) {
  return handle(request, ctx, "PATCH");
}

export async function PUT(request: Request, ctx: RouteParams) {
  return handle(request, ctx, "PUT");
}

export async function DELETE(request: Request, ctx: RouteParams) {
  return handle(request, ctx, "DELETE");
}
