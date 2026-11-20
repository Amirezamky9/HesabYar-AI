/**
 * Company picker for the US-304 list pages.
 *
 * One company: a quiet caption naming it (the list is company-scoped,
 * so the operator should always see which company they are looking at).
 * Several companies: a Select to switch.
 */

"use client";

import type { CompanyResponse } from "@/lib/api-client";
import { Label, Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui";

export function CompanyPicker({
  companies,
  companyId,
  onChange,
}: {
  companies: CompanyResponse[];
  companyId: string | null;
  onChange: (id: string) => void;
}) {
  if (companies.length === 0) return null;
  if (companies.length === 1) {
    return <p className="text-sm text-muted-foreground">شرکت: {companies[0].name}</p>;
  }
  return (
    <div className="flex items-center gap-2">
      <Label htmlFor="company-picker">شرکت</Label>
      <Select value={companyId ?? undefined} onValueChange={(v) => v && onChange(v)}>
        <SelectTrigger id="company-picker" className="w-64">
          {/* Explicit label: Base UI resolves the trigger label from
          popup items (mounted on first open), so a controlled value
          would otherwise display the raw company id. */}
          <SelectValue>
            {companies.find((c) => c.id === companyId)?.name ?? "انتخاب شرکت"}
          </SelectValue>
        </SelectTrigger>
        <SelectContent>
          {companies.map((c) => (
            <SelectItem key={c.id} value={c.id}>
              {c.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}
