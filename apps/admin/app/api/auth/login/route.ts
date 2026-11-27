/**
 * POST /api/auth/login — validate an operator API key and open a session.
 *
 * Body: { apiKey: "hya_live_..." }. The key is validated against the
 * FastAPI backend (a scoped probe) and then stored server-side only, in
 * the session store. The response carries only public session info —
 * never the key.
 *
 * Validation logic: 401 from the probe means the key is unknown/invalid.
 * Any other probe outcome (200, 403 for a key without company:read, …)
 * proves the key exists, so the session is created; the backend keeps
 * enforcing scopes per endpoint.
 */

import { NextResponse } from "next/server";
import { createSession } from "@/lib/auth";
import { backendFetch } from "@/lib/backend";


interface LoginBody {
  apiKey?: unknown;
}

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

export async function POST(request: Request) {
  let body: LoginBody;
  try {
    body = (await request.json()) as LoginBody;
  } catch {
    return problem(400, "BAD_REQUEST", "Invalid JSON body.");
  }

  const apiKey = typeof body.apiKey === "string" ? body.apiKey.trim() : "";
  if (!apiKey) {
    return problem(400, "VALIDATION_ERROR", "API key is required.");
  }
  if (!apiKey.startsWith("hya_live_") || apiKey.length > 256) {
    return problem(400, "VALIDATION_ERROR", "API key must start with hya_live_.");
  }

  // Probe: a cheap scoped read. 401 = unknown key; anything else = real key.
  let probe: Response;
  try {
    probe = await backendFetch("companies?limit=1", apiKey);
  } catch {
    return problem(502, "BACKEND_UNREACHABLE", "The HesabYar backend did not answer.");
  }

  if (probe.status === 401) {
    return problem(401, "UNAUTHORIZED", "This API key is not valid.");
  }

  const info = await createSession(apiKey);
  return NextResponse.json(info);
}
