"use client";

/**
 * Account picker (US-303) — searchable combobox over the chart of
 * accounts, built only from facade primitives (Popover + Command +
 * Button). Only postable, active, open accounts are offered; search
 * matches code or name with normalized digits and ي/ك unification.
 */

import * as React from "react";
import { ChevronsUpDown } from "lucide-react";
import type { AccountResponse } from "@/lib/api-client";
import {
  accountLabel,
  filterAccounts,
} from "@/features/journal-entries/lib/journal-filters";
import {
  Button,
  Command,
  CommandEmpty,
  CommandInput,
  CommandItem,
  CommandList,
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui";

export interface AccountPickerProps {
  accounts: AccountResponse[];
  /** Selected engine account id, or null. */
  value: number | null;
  onChange: (accountId: number | null) => void;
  /** True while the chart is still loading. */
  loading?: boolean;
  disabled?: boolean;
  /** Accessible label for the trigger button. */
  ariaLabel: string;
  id?: string;
}

export function AccountPicker({
  accounts,
  value,
  onChange,
  loading = false,
  disabled = false,
  ariaLabel,
  id,
}: AccountPickerProps) {
  const [open, setOpen] = React.useState(false);
  const [query, setQuery] = React.useState("");

  const selected = accounts.find((a) => a.id === value) ?? null;
  // Filtering is done here (not by cmdk) so Persian digits and ي/ك
  // normalization apply; cmdk's own filter is switched off.
  const visible = React.useMemo(() => filterAccounts(accounts, query), [accounts, query]);

  return (
    <Popover open={open} onOpenChange={(next) => {
      setOpen(next);
      if (!next) setQuery("");
    }}>
      <PopoverTrigger
        id={id}
        aria-label={ariaLabel}
        disabled={disabled || loading}
        render={
          <Button
            type="button"
            variant="outline"
            role="combobox"
            aria-expanded={open}
            className="w-full justify-between font-normal"
          >
            {loading ? (
              <span className="text-muted-foreground">در حال بارگذاری حساب‌ها…</span>
            ) : selected ? (
              <span className="flex min-w-0 items-center gap-2">
                <span className="ltr-isolate shrink-0 tabular-nums text-muted-foreground">
                  {selected.code}
                </span>
                <span className="truncate">{selected.name}</span>
              </span>
            ) : (
              <span className="text-muted-foreground">انتخاب حساب</span>
            )}
            <ChevronsUpDown className="size-4 shrink-0 opacity-50" aria-hidden />
          </Button>
        }
      />
      <PopoverContent align="start" sideOffset={4} className="w-80 p-0">
        <Command shouldFilter={false} label={ariaLabel}>
          <CommandInput
            placeholder="جست‌وجوی حساب با کد یا نام…"
            value={query}
            onValueChange={setQuery}
          />
          <CommandList>
            <CommandEmpty>حسابی پیدا نشد.</CommandEmpty>
            {visible.map((account) => {
              const isSelected = account.id === value;
              return (
                <CommandItem
                  key={account.id}
                  value={accountLabel(account)}
                  data-checked={isSelected}
                  onSelect={() => {
                    onChange(account.id);
                    setOpen(false);
                  }}
                >
                  <span className="ltr-isolate shrink-0 tabular-nums text-muted-foreground">
                    {account.code}
                  </span>
                  <span className="truncate">{account.name}</span>
                </CommandItem>
              );
            })}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
