"use client";

/**
 * Compact top bar (US-302, spec §5.4): mobile nav button, breadcrumbs,
 * company selector, session key hint, logout. Sticky; the sidebar owns
 * navigation, this bar owns context.
 */

import * as React from "react";
import { Menu } from "lucide-react";
import { Button } from "@/components/ui";
import { Breadcrumbs } from "./breadcrumbs";
import { CompanySelector } from "./company-selector";
import { LogoutButton } from "./logout-button";

export function Topbar({
  keyLast4,
  onOpenMobileNav,
}: {
  keyLast4: string | null;
  onOpenMobileNav: () => void;
}) {
  return (
    <header className="sticky top-0 z-40 flex h-14 shrink-0 items-center gap-3 border-b bg-background px-4 print:hidden">
      <Button
        variant="ghost"
        size="icon"
        className="lg:hidden"
        onClick={onOpenMobileNav}
        aria-label="باز کردن ناوبری"
      >
        <Menu className="size-5" strokeWidth={1.5} aria-hidden />
      </Button>
      <span className="hidden text-lg font-bold sm:block lg:hidden">حسابیار</span>
      <div className="hidden min-w-0 flex-1 sm:block">
        <Breadcrumbs />
      </div>
      <div className="ms-auto flex items-center gap-2 sm:gap-3">
        <CompanySelector />
        {keyLast4 ? (
          <span className="hidden text-xs text-muted-foreground md:inline" dir="ltr">
            key …{keyLast4}
          </span>
        ) : null}
        <LogoutButton />
      </div>
    </header>
  );
}
