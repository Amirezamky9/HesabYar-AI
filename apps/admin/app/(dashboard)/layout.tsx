/**
 * Dashboard shell (US-302). Auth-gated: unauthenticated visitors are
 * redirected to /login; the client chrome (sidebar, top bar, company
 * context) lives in DashboardShell.
 */

import { connection } from "next/server";
import { redirect } from "next/navigation";
import { getSessionInfo } from "@/lib/auth";
import { DashboardShell } from "@/components/layout/dashboard-shell";

/**
 * Every dashboard route is authenticated (session cookie) and live — none
 * of them prerender. Opt the whole group out of instant-navigation
 * validation (Next 16 cacheComponents mode) so the auth check in this
 * layout may block rendering.
 */
export const instant = false;

export default async function DashboardLayout({ children }: { children: React.ReactNode }) {
  // Stop prerendering here: the session check below reads cookies and uses
  // Date.now() (session expiry), which is unstable during prerender. Without
  // this, `next start` prerenders "/" and serves a 200 error page instead of
  // redirecting unauthenticated visitors to /login (verified 2026-11-17).
  await connection();
  const session = await getSessionInfo();
  if (!session.authenticated) {
    redirect("/login");
  }

  return <DashboardShell keyLast4={session.keyLast4}>{children}</DashboardShell>;
}
