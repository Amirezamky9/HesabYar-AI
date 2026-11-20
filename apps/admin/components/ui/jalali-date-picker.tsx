"use client";

/**
 * Jalali date picker (facade) — VibeFarsi calendar/date-picker underneath.
 *
 * API contract (spec §5.5 rules 6, 9): the value is a strict Gregorian
 * ISO date string (`YYYY-MM-DD`) or null. The UI shows Jalali; the
 * conversion is deterministic (lib/jalali) and never inferred from an
 * ambiguous string. Pass `null` (not "") for "no date".
 */

import * as React from "react";
import { isValidIsoDate, parseIsoDate } from "@/lib/jalali";
import { DatePicker, type DatePickerProps } from "./date-picker";

export interface JalaliDatePickerProps {
  /** Strict Gregorian ISO date (`YYYY-MM-DD`) or null. */
  value?: string | null;
  onChange?: (isoDate: string | null) => void;
  placeholder?: string;
  clearable?: boolean;
  weekday?: boolean;
  disabled?: boolean;
  className?: string;
  id?: string;
}

function toIso(y: number, m: number, d: number): string {
  return `${String(y).padStart(4, "0")}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}

export function JalaliDatePicker({
  value,
  onChange,
  placeholder = "انتخاب تاریخ",
  clearable = true,
  weekday = true,
  disabled,
  className,
  id,
}: JalaliDatePickerProps) {
  // ISO -> Date for the underlying calendar, at local noon: the calendar
  // day is stable in the viewer's timezone (no UTC-midnight shift).
  const dateValue = React.useMemo<Date | null>(() => {
    if (!value) return null;
    const { gy, gm, gd } = parseIsoDate(value);
    return new Date(gy, gm - 1, gd, 12, 0, 0);
  }, [value]);

  const handleChange = React.useCallback(
    (date: Date | null) => {
      if (!date) {
        onChange?.(null);
        return;
      }
      // The picked day's local calendar components ARE the Gregorian date.
      const iso = toIso(date.getFullYear(), date.getMonth() + 1, date.getDate());
      if (!isValidIsoDate(iso)) {
        throw new RangeError(`JalaliDatePicker: picked an invalid date: ${iso}`);
      }
      onChange?.(iso);
    },
    [onChange],
  );

  const pickerProps: DatePickerProps = {
    value: dateValue,
    onChange: handleChange,
    placeholder,
    clearable,
    weekday,
    className,
  };

  return (
    <div id={id} aria-disabled={disabled || undefined} className={disabled ? "pointer-events-none opacity-50" : undefined}>
      <DatePicker {...pickerProps} />
    </div>
  );
}
