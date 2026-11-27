/**
 * Cash-forecast page (US-553): resolves the company, then renders the
 * shared calendar section. Also reachable from the reports page's
 * «پیش‌بینی جریان نقد» tab, which passes its own company down.
 */

"use client";

import * as React from "react";
import { CompanyPicker } from "@/features/shared/company-picker";
import { useCompany } from "@/features/shared/use-company";
import { CashForecastSection } from "./cash-forecast-section";

export function CashForecastPage() {
  const { companies, companyId, setCompanyId } = useCompany();
  const companyName = companies.find((c) => c.id === companyId)?.name;

  return (
    <main className="mx-auto w-full max-w-6xl space-y-6 px-4 py-6 sm:px-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">پیش‌بینی جریان نقد</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            ورودی‌ها و خروجی‌های موردانتظار ماه، از روی تسویه‌ها، اقساط، چک‌ها و فاکتورهای
            ثبت‌شده
          </p>
        </div>
        <CompanyPicker
          companies={companies}
          companyId={companyId}
          onChange={setCompanyId}
        />
      </div>
      <CashForecastSection companyId={companyId} companyName={companyName} />
    </main>
  );
}
