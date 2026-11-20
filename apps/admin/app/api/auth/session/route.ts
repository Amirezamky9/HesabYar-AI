/** GET /api/auth/session — public session info (never includes the key). */

import { NextResponse } from "next/server";
import { getSessionInfo } from "@/lib/auth";


export async function GET() {
  return NextResponse.json(await getSessionInfo());
}
