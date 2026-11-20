"use client";

/**
 * Debit/credit line grid (US-303) — the journal-line editor.
 *
 * Each row is one-sided: typing a debit clears the credit and vice
 * versa. Amounts are integer IRR (AmountInput with unit «ریال»); the
 * live balance bar shows totals in labelled Toman. The grid itself
 * never saves — the editor validates and submits.
 */

import * as React from "react";
import { CircleAlert, CircleCheck, Plus, Trash2 } from "lucide-react";
import type { AccountResponse } from "@/lib/api-client";
import { fa } from "@/lib/utils";
import {
  emptyLine,
  lineTotals,
  setLineAmount,
  type JournalLineDraft,
} from "@/features/journal-entries/lib/balance";
import {
  AmountInput,
  Button,
  Input,
  Label,
  MoneyDisplay,
} from "@/components/ui";
import { AccountPicker } from "./account-picker";

export interface JournalLineGridProps {
  lines: JournalLineDraft[];
  onChange: (lines: JournalLineDraft[]) => void;
  accounts: AccountResponse[];
  accountsLoading?: boolean;
  disabled?: boolean;
}

export function JournalLineGrid({
  lines,
  onChange,
  accounts,
  accountsLoading = false,
  disabled = false,
}: JournalLineGridProps) {
  const totals = lineTotals(lines);

  const updateLine = (key: string, patch: Partial<JournalLineDraft>) => {
    onChange(lines.map((line) => (line.key === key ? { ...line, ...patch } : line)));
  };

  const updateAmount = (key: string, side: "debit" | "credit", amount: number | null) => {
    onChange(
      lines.map((line) => (line.key === key ? setLineAmount(line, side, amount) : line)),
    );
  };

  const removeLine = (key: string) => {
    if (lines.length <= 2) return;
    onChange(lines.filter((line) => line.key !== key));
  };

  return (
    <div className="space-y-4">
      <div className="space-y-3">
        {lines.map((line, index) => {
          const rowLabel = `ردیف ${fa(index + 1)}`;
          return (
            <div
              key={line.key}
              data-testid={`journal-line-${index}`}
              className="rounded-lg border bg-card p-4"
            >
              <div className="mb-3 flex items-center justify-between gap-2">
                <span className="text-sm font-medium">{rowLabel}</span>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  aria-label={`حذف ${rowLabel}`}
                  disabled={disabled || lines.length <= 2}
                  onClick={() => removeLine(line.key)}
                >
                  <Trash2 className="size-4" aria-hidden />
                </Button>
              </div>
              <div className="grid grid-cols-2 gap-3 md:grid-cols-12">
                <div className="col-span-2 space-y-2 md:col-span-4">
                  <Label htmlFor={`line-${line.key}-account`}>حساب</Label>
                  <AccountPicker
                    id={`line-${line.key}-account`}
                    ariaLabel={`حساب ${rowLabel}`}
                    accounts={accounts}
                    value={line.accountId}
                    loading={accountsLoading}
                    disabled={disabled}
                    onChange={(accountId) => updateLine(line.key, { accountId })}
                  />
                </div>
                <div className="space-y-2 md:col-span-2">
                  <Label htmlFor={`line-${line.key}-debit`}>بدهکار</Label>
                  <AmountInput
                    id={`line-${line.key}-debit`}
                    unit="ریال"
                    words={false}
                    disabled={disabled || line.credit > 0}
                    value={line.debit || null}
                    onChange={(amount) => updateAmount(line.key, "debit", amount)}
                  />
                </div>
                <div className="space-y-2 md:col-span-2">
                  <Label htmlFor={`line-${line.key}-credit`}>بستانکار</Label>
                  <AmountInput
                    id={`line-${line.key}-credit`}
                    unit="ریال"
                    words={false}
                    disabled={disabled || line.debit > 0}
                    value={line.credit || null}
                    onChange={(amount) => updateAmount(line.key, "credit", amount)}
                  />
                </div>
                <div className="col-span-2 space-y-2 md:col-span-4">
                  <Label htmlFor={`line-${line.key}-description`}>شرح ردیف</Label>
                  <Input
                    id={`line-${line.key}-description`}
                    value={line.description}
                    disabled={disabled}
                    placeholder="شرح این ردیف (اختیاری)"
                    onChange={(e) => updateLine(line.key, { description: e.target.value })}
                  />
                </div>
              </div>
            </div>
          );
        })}
      </div>

      <Button
        type="button"
        variant="outline"
        disabled={disabled}
        onClick={() => onChange([...lines, emptyLine()])}
      >
        <Plus className="size-4" aria-hidden />
        افزودن ردیف
      </Button>

      {/* Live balance bar: totals in labelled Toman, balance state. */}
      <div
        className="flex flex-wrap items-center gap-x-6 gap-y-2 rounded-lg border bg-muted/50 px-4 py-3"
        aria-live="polite"
        data-testid="balance-bar"
      >
        <div className="flex items-baseline gap-2">
          <span className="text-sm text-muted-foreground">جمع بدهکار</span>
          <MoneyDisplay amountIrr={totals.totalDebit} size="sm" />
        </div>
        <div className="flex items-baseline gap-2">
          <span className="text-sm text-muted-foreground">جمع بستانکار</span>
          <MoneyDisplay amountIrr={totals.totalCredit} size="sm" />
        </div>
        <div className="flex items-baseline gap-2">
          <span className="text-sm text-muted-foreground">اختلاف</span>
          <MoneyDisplay amountIrr={Math.abs(totals.difference)} size="sm" />
        </div>
        {totals.balanced ? (
          <span className="ms-auto flex items-center gap-1.5 text-sm font-medium text-success">
            <CircleCheck className="size-4" aria-hidden />
            تراز است
          </span>
        ) : (
          <span className="ms-auto flex items-center gap-1.5 text-sm font-medium text-warning-foreground">
            <CircleAlert className="size-4" aria-hidden />
            نامتوازن
          </span>
        )}
      </div>
    </div>
  );
}
