"use client";

/**
 * Command menu (facade) — FarsiUI Command + CommandDialog underneath.
 *
 * A ⌘K / Ctrl+K palette. Feature pages render <CommandMenu> with their
 * groups/items; the palette opens with the keyboard shortcut and closes
 * on selection or Escape. All labels must be Persian.
 */

import * as React from "react";
import {
  Command as CommandRoot,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
  CommandShortcut,
} from "./command";

export interface CommandMenuProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Placeholder for the search input. Default: «جست‌وجو…». */
  placeholder?: string;
  /** Shown when no item matches. Default: «نتیجه‌ای پیدا نشد.» */
  emptyText?: string;
  children: React.ReactNode;
}

export function CommandMenu({
  open,
  onOpenChange,
  placeholder = "جست‌وجو…",
  emptyText = "نتیجه‌ای پیدا نشد.",
  children,
}: CommandMenuProps) {
  return (
    <CommandDialog open={open} onOpenChange={onOpenChange}>
      <CommandRoot>
        <CommandInput placeholder={placeholder} />
        <CommandList>
          <CommandEmpty>{emptyText}</CommandEmpty>
          {children}
        </CommandList>
      </CommandRoot>
    </CommandDialog>
  );
}

/** Global ⌘K / Ctrl+K shortcut: returns [open, setOpen]. */
export function useCommandMenuShortcut() {
  const [open, setOpen] = React.useState(false);
  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((o) => !o);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);
  return [open, setOpen] as const;
}

export {
  CommandRoot as Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
  CommandShortcut,
};
