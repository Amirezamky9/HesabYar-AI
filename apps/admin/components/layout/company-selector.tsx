"use client";

/**
 * Company selector (US-302) — the active company for every
 * company-scoped call. Lives in the top bar; the value is a normalized
 * company id, never a display string (§5.5 rule 10).
 */

import * as React from "react";
import { useCompany } from "@/features/dashboard/company-context";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Skeleton,
} from "@/components/ui";

export function CompanySelector() {
  const { companies, company, companyId, status, selectCompany } = useCompany();

  if (status === "loading") {
    // Same width as the loaded trigger at every breakpoint — a wider
    // skeleton overflows the top bar at 320px while companies load.
    return <Skeleton className="h-9 w-32 sm:w-44 lg:w-56" aria-label="در حال بارگذاری شرکت‌ها" />;
  }

  if (status !== "ready" || companies.length === 0) {
    return (
      <span className="text-sm text-muted-foreground" role="status">
        شرکتی در دسترس نیست
      </span>
    );
  }

  return (
    <Select
      value={companyId ?? undefined}
      onValueChange={(value) => {
        if (value !== null) selectCompany(value);
      }}
    >
      <SelectTrigger aria-label="شرکت فعال" className="w-32 sm:w-44 lg:w-56">
        {/*
          Base UI resolves the trigger label from the mounted list items;
          the popup is unmounted while closed, so without explicit
          children the trigger shows the raw company id. Pass the name.
        */}
        <SelectValue placeholder="انتخاب شرکت">{company?.name}</SelectValue>
      </SelectTrigger>
      <SelectContent>
        {companies.map((company) => (
          <SelectItem key={company.id} value={company.id}>
            {company.name}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
