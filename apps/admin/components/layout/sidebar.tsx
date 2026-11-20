"use client";

/**
 * Dashboard sidebar (US-302) — collapsible RTL sidebar with the 13 §18
 * modules (spec §5.4). The sidebar sits on the start edge (right in RTL).
 * Collapsed mode shows icons only, with tooltips; coming-soon modules
 * are visible but disabled with a «به‌زودی» badge. The same nav content
 * is reused in the mobile sheet.
 */

import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { ChevronDown, PanelRightClose, PanelRightOpen } from "lucide-react";
import { cn } from "@/lib/utils";
import { Badge, Button, Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui";
import { isModuleActive, NAV_MODULES, type NavModule } from "./navigation";

const LINK_CLASSES =
  "flex min-h-11 w-full items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground focus-visible:outline-2 focus-visible:outline-ring";
const ACTIVE_CLASSES = "bg-sidebar-primary font-semibold text-sidebar-primary-foreground hover:bg-sidebar-primary hover:text-sidebar-primary-foreground";

/** Tooltip around an icon-only link/button (collapsed sidebar). The child
 * element itself is the trigger (Base UI `render` pattern). */
function NavTooltip({ label, children }: { label: string; children: React.ReactElement }) {
  return (
    <Tooltip>
      <TooltipTrigger render={children} />
      <TooltipContent side="inline-end">{label}</TooltipContent>
    </Tooltip>
  );
}

interface NavContentProps {
  collapsed: boolean;
  /** Called when a link is activated (mobile sheet closes itself). */
  onNavigate?: () => void;
  /** Collapsed mode: a group icon expands the sidebar instead of navigating. */
  onRequestExpand?: () => void;
}

export function SidebarNavContent({ collapsed, onNavigate, onRequestExpand }: NavContentProps) {
  const pathname = usePathname();
  // Group expansion is DERIVED: a group is open when its page is active,
  // unless the user explicitly toggled it (overrides win over the
  // pathname default). No effect syncs state after render.
  const [overrides, setOverrides] = React.useState<Map<string, boolean>>(new Map());

  const isGroupExpanded = (navModule: NavModule): boolean => {
    const override = overrides.get(navModule.id);
    if (override !== undefined) return override;
    return isModuleActive(navModule, pathname);
  };

  const toggleGroup = (navModule: NavModule) => {
    setOverrides((prev) => new Map(prev).set(navModule.id, !isGroupExpanded(navModule)));
  };

  return (
    <nav aria-label="ناوبری اصلی">
      <ul className="space-y-1 px-2">
        {NAV_MODULES.map((navModule) => (
          <li key={navModule.id}>
            <NavModuleRow
              navModule={navModule}
              pathname={pathname}
              collapsed={collapsed}
              groupExpanded={isGroupExpanded(navModule)}
              onToggleGroup={() => toggleGroup(navModule)}
              onNavigate={onNavigate}
              onRequestExpand={onRequestExpand}
            />
          </li>
        ))}
      </ul>
    </nav>
  );
}

function NavModuleRow({
  navModule,
  pathname,
  collapsed,
  groupExpanded,
  onToggleGroup,
  onNavigate,
  onRequestExpand,
}: NavContentProps & {
  navModule: NavModule;
  pathname: string;
  groupExpanded: boolean;
  onToggleGroup: () => void;
}) {
  const Icon = navModule.icon;
  const active = isModuleActive(navModule, pathname);

  // -- Coming-soon module: visible, disabled, badged. -------------------
  if (navModule.comingSoon) {
    if (collapsed) {
      return (
        <NavTooltip label={`${navModule.label} (به‌زودی)`}>
          <button
            type="button"
            disabled
            aria-label={`${navModule.label} — به‌زودی`}
            className={cn(LINK_CLASSES, "justify-center px-0 opacity-60")}
          >
            <Icon className="size-5 shrink-0" strokeWidth={1.5} aria-hidden />
          </button>
        </NavTooltip>
      );
    }
    return (
      <button
        type="button"
        disabled
        aria-disabled="true"
        className={cn(LINK_CLASSES, "cursor-not-allowed opacity-60")}
      >
        <Icon className="size-5 shrink-0" strokeWidth={1.5} aria-hidden />
        <span className="flex-1 text-start">{navModule.label}</span>
        <Badge variant="outline">به‌زودی</Badge>
      </button>
    );
  }

  // -- Group module (حسابداری): expandable children. --------------------
  if (navModule.children) {
    if (collapsed) {
      return (
        <NavTooltip label={navModule.label}>
          <button
            type="button"
            aria-label={navModule.label}
            onClick={onRequestExpand}
            className={cn(LINK_CLASSES, "justify-center px-0", active && ACTIVE_CLASSES)}
          >
            <Icon className="size-5 shrink-0" strokeWidth={1.5} aria-hidden />
          </button>
        </NavTooltip>
      );
    }
    return (
      <div>
        <button
          type="button"
          aria-expanded={groupExpanded}
          aria-controls={`nav-group-${navModule.id}`}
          onClick={onToggleGroup}
          className={cn(LINK_CLASSES, active && "text-sidebar-primary-foreground")}
        >
          <Icon className="size-5 shrink-0" strokeWidth={1.5} aria-hidden />
          <span className="flex-1 text-start">{navModule.label}</span>
          <ChevronDown
            className={cn("size-4 shrink-0 transition-transform duration-150", groupExpanded && "rotate-180")}
            strokeWidth={2}
            aria-hidden
          />
        </button>
        {groupExpanded ? (
          <ul id={`nav-group-${navModule.id}`} className="mt-1 space-y-1 border-s-2 border-sidebar-border ps-2 ms-4">
            {navModule.children.map((child) => {
              const ChildIcon = child.icon;
              const childActive = pathname === child.href || pathname.startsWith(`${child.href}/`);
              return (
                <li key={child.id}>
                  <Link
                    href={child.href}
                    aria-current={pathname === child.href ? "page" : undefined}
                    onClick={onNavigate}
                    className={cn(LINK_CLASSES, childActive && ACTIVE_CLASSES)}
                  >
                    <ChildIcon className="size-5 shrink-0" strokeWidth={1.5} aria-hidden />
                    <span className="flex-1 text-start">{child.label}</span>
                  </Link>
                </li>
              );
            })}
          </ul>
        ) : null}
      </div>
    );
  }

  // -- Plain link module. ------------------------------------------------
  const href = navModule.href ?? "/";
  if (collapsed) {
    return (
      <NavTooltip label={navModule.label}>
        <Link
          href={href}
          aria-label={navModule.label}
          aria-current={pathname === href ? "page" : undefined}
          onClick={onNavigate}
          className={cn(LINK_CLASSES, "justify-center px-0", active && ACTIVE_CLASSES)}
        >
          <Icon className="size-5 shrink-0" strokeWidth={1.5} aria-hidden />
        </Link>
      </NavTooltip>
    );
  }
  return (
    <Link
      href={href}
      aria-current={pathname === href ? "page" : undefined}
      onClick={onNavigate}
      className={cn(LINK_CLASSES, active && ACTIVE_CLASSES)}
    >
      <Icon className="size-5 shrink-0" strokeWidth={1.5} aria-hidden />
      <span className="flex-1 text-start">{navModule.label}</span>
    </Link>
  );
}

/** Desktop sidebar (hidden below lg; mobile uses a sheet in the shell). */
export function Sidebar({
  collapsed,
  onToggleCollapsed,
}: {
  collapsed: boolean;
  onToggleCollapsed: () => void;
}) {
  return (
    <aside
      className={cn(
        "sticky top-0 hidden h-dvh shrink-0 flex-col border-e bg-sidebar text-sidebar-foreground lg:flex print:hidden",
        collapsed ? "w-16" : "w-64",
      )}
    >
      <div className={cn("flex h-14 shrink-0 items-center gap-2 border-b border-sidebar-border px-4", collapsed && "justify-center px-2")}>
        {!collapsed ? (
          <span className="flex-1 text-lg font-bold">حسابیار</span>
        ) : null}
        <Button
          variant="ghost"
          size="icon"
          onClick={onToggleCollapsed}
          aria-label={collapsed ? "باز کردن نوار کناری" : "بستن نوار کناری"}
          aria-expanded={!collapsed}
        >
          {collapsed ? (
            <PanelRightOpen className="size-5" strokeWidth={1.5} aria-hidden />
          ) : (
            <PanelRightClose className="size-5" strokeWidth={1.5} aria-hidden />
          )}
        </Button>
      </div>
      <div className="flex-1 overflow-y-auto py-4">
        <SidebarNavContent collapsed={collapsed} onRequestExpand={onToggleCollapsed} />
      </div>
    </aside>
  );
}
