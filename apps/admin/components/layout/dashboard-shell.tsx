"use client";

/**
 * Dashboard shell (US-302) — client composition around the server-side
 * auth layout: CompanyProvider, collapsible desktop sidebar, mobile nav
 * sheet, compact top bar, content column. The auth check stays in the
 * server layout (app/(dashboard)/layout.tsx); this component only
 * renders chrome for an authenticated session.
 */

import * as React from "react";
import { CompanyProvider } from "@/features/dashboard/company-context";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui";
import { Sidebar, SidebarNavContent } from "./sidebar";
import { Topbar } from "./topbar";

export function DashboardShell({
  keyLast4,
  children,
}: {
  keyLast4: string | null;
  children: React.ReactNode;
}) {
  // Collapse state is session-only: restoring it from localStorage after
  // mount would need a setState-in-effect (lint error) and reading it
  // during render would break hydration. The sidebar defaults to expanded.
  const [collapsed, setCollapsed] = React.useState(false);
  const [mobileOpen, setMobileOpen] = React.useState(false);

  const toggleCollapsed = React.useCallback(() => {
    setCollapsed((prev) => !prev);
  }, []);

  return (
    <CompanyProvider>
      <div className="flex min-h-dvh">
        <Sidebar collapsed={collapsed} onToggleCollapsed={toggleCollapsed} />
        <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
          <SheetContent side="right" className="w-72 p-0 sm:max-w-72">
            <SheetHeader className="border-b px-4 py-3">
              <SheetTitle className="text-start text-lg font-bold">حسابیار</SheetTitle>
            </SheetHeader>
            <div className="overflow-y-auto py-4">
              <SidebarNavContent collapsed={false} onNavigate={() => setMobileOpen(false)} />
            </div>
          </SheetContent>
        </Sheet>
        <div className="flex min-w-0 flex-1 flex-col">
          <Topbar keyLast4={keyLast4} onOpenMobileNav={() => setMobileOpen(true)} />
          <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-6 sm:px-6">{children}</main>
        </div>
      </div>
    </CompanyProvider>
  );
}
