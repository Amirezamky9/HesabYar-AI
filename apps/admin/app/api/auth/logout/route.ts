/** POST /api/auth/logout — destroy the session and clear the cookie. */

import { NextResponse } from "next/server";
import { destroySession } from "@/lib/auth";


export async function POST() {
  await destroySession();
  return NextResponse.json({ ok: true });
}
