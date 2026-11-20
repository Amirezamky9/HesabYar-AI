/**
 * Money display (facade) — VibeFarsi Price underneath, HesabYar money
 * rules on top (spec §5.5 rule 5, §8.3).
 *
 * Input is always integer IRR. Display defaults to Toman, explicitly
 * labelled («تومان»); pass unit="irr" for an explicit «ریال» display.
 * The amount is LTR-isolated so digits never reorder inside RTL text.
 */

import { cn } from "@/lib/utils";
import { formatIrr, formatToman, type MoneyUnit } from "@/lib/format-money";
import { Price } from "./price";

export interface MoneyDisplayProps {
  /** Integer IRR. */
  amountIrr: number;
  /** Display unit — Toman is display-only and always labelled. Default: toman. */
  unit?: MoneyUnit;
  size?: "sm" | "md" | "lg";
  className?: string;
}

export function MoneyDisplay({ amountIrr, unit = "toman", size = "md", className }: MoneyDisplayProps) {
  if (!Number.isInteger(amountIrr)) {
    throw new RangeError(`MoneyDisplay: amountIrr must be an integer, got ${amountIrr}`);
  }
  // Whole Toman values render through the VibeFarsi Price visual;
  // fractional Toman (IRR not divisible by 10) renders exactly.
  if (unit === "toman" && amountIrr % 10 === 0) {
    return (
      <span className={cn("ltr-isolate", className)}>
        <Price amount={amountIrr / 10} unit="تومان" size={size} />
      </span>
    );
  }
  const text = unit === "toman" ? `${formatToman(amountIrr)} تومان` : `${formatIrr(amountIrr)} ریال`;
  const sizes = { sm: "text-base", md: "text-2xl", lg: "text-4xl" };
  return (
    <span className={cn("ltr-isolate inline-flex items-baseline gap-1.5", className)}>
      <span className={cn("font-bold tabular-nums", sizes[size])}>
        {unit === "toman" ? formatToman(amountIrr) : formatIrr(amountIrr)}
      </span>
      <span className="text-sm text-muted-foreground">{unit === "toman" ? "تومان" : "ریال"}</span>
      <span className="sr-only">{text}</span>
    </span>
  );
}
