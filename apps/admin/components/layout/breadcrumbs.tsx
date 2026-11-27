"use client";

/**
 * Breadcrumbs (US-302, spec §5.4) — derived from the pathname with the
 * Persian labels in navigation.ts. The current page is plain text with
 * aria-current; ancestors are links. Unknown segments fall back to the
 * raw segment (never crash on a route this story does not know).
 */

import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { ChevronLeft } from "lucide-react";
import { ROUTE_LABELS } from "./navigation";

interface Crumb {
  href: string;
  label: string;
}

export function buildCrumbs(pathname: string): Crumb[] {
  const crumbs: Crumb[] = [{ href: "/", label: "داشبورد" }];
  const segments = pathname.split("/").filter(Boolean);
  let acc = "";
  for (const segment of segments) {
    acc += `/${segment}`;
    crumbs.push({ href: acc, label: ROUTE_LABELS[segment] ?? segment });
  }
  return crumbs;
}

export function Breadcrumbs() {
  const pathname = usePathname();
  const crumbs = buildCrumbs(pathname);
  const current = crumbs[crumbs.length - 1];

  return (
    <nav aria-label="مسیر صفحه">
      <ol className="flex min-w-0 flex-wrap items-center gap-1 text-sm">
        {crumbs.slice(0, -1).map((crumb) => (
          <li key={crumb.href} className="flex items-center gap-1">
            <Link href={crumb.href} className="rounded-sm text-muted-foreground hover:text-foreground hover:underline">
              {crumb.label}
            </Link>
            <ChevronLeft className="size-4 text-muted-foreground" strokeWidth={2} aria-hidden />
          </li>
        ))}
        <li key={current.href} aria-current="page" className="truncate font-medium">
          {current.label}
        </li>
      </ol>
    </nav>
  );
}
