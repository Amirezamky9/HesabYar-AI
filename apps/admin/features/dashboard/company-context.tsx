"use client";

/**
 * Company context (US-302).
 *
 * Every company-scoped backend endpoint requires a `company` query
 * parameter (the dev operator key is not restricted to one company — the
 * API answers 400 COMPANY_REQUIRED without it). The dashboard therefore
 * picks one active company: the stored selection when it is still in the
 * accessible list, otherwise the first accessible company. The selection
 * is a normalized id stored in localStorage; the UI shows the company
 * name and every widget states which company its figures belong to
 * (spec §11.3: no KPI without period, company and unit).
 */

import * as React from "react";
import { api, ApiError, type CompanyResponse } from "@/lib/api-client";

const STORAGE_KEY = "hesabyar.company-id";

export type CompanyStatus = "loading" | "ready" | "empty" | "error";

export interface CompanyContextValue {
  companies: CompanyResponse[];
  /** The active company (null while loading / when none is accessible). */
  company: CompanyResponse | null;
  /** Normalized id of the active company (null while loading). */
  companyId: string | null;
  status: CompanyStatus;
  /** Persian error message when status is "error". */
  errorMessage: string | null;
  selectCompany: (companyId: string) => void;
  refresh: () => void;
}

const CompanyContext = React.createContext<CompanyContextValue | null>(null);

function readStoredCompanyId(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function storeCompanyId(companyId: string): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, companyId);
  } catch {
    // Storage unavailable (private mode) — selection still works in memory.
  }
}

export function CompanyProvider({ children }: { children: React.ReactNode }) {
  const [companies, setCompanies] = React.useState<CompanyResponse[]>([]);
  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const [status, setStatus] = React.useState<CompanyStatus>("loading");
  const [errorMessage, setErrorMessage] = React.useState<string | null>(null);
  const [nonce, setNonce] = React.useState(0);

  React.useEffect(() => {
    let cancelled = false;
    api
      .listCompanies({ limit: 50 })
      .then((page) => {
        if (cancelled) return;
        setCompanies(page.items);
        if (page.items.length === 0) {
          setSelectedId(null);
          setStatus("empty");
          return;
        }
        const stored = readStoredCompanyId();
        const storedStillValid = stored !== null && page.items.some((c) => c.id === stored);
        setSelectedId(storedStillValid ? stored : page.items[0].id);
        setStatus("ready");
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const message =
          err instanceof ApiError
            ? err.isUnauthorized
              ? "نشست به پایان رسیده است؛ دوباره وارد شوید."
              : err.isForbidden
                ? "کلید شما دسترسی خواندن شرکت‌ها را ندارد."
                : "دریافت فهرست شرکت‌ها ناموفق بود."
            : "ارتباط با سرور برقرار نشد.";
        setErrorMessage(message);
        setStatus("error");
      });
    return () => {
      cancelled = true;
    };
  }, [nonce]);

  const value = React.useMemo<CompanyContextValue>(() => {
    const company = companies.find((c) => c.id === selectedId) ?? null;
    return {
      companies,
      company,
      companyId: company?.id ?? null,
      status,
      errorMessage,
      selectCompany: (companyId: string) => {
        if (!companies.some((c) => c.id === companyId)) return;
        storeCompanyId(companyId);
        setSelectedId(companyId);
      },
      // Reset to loading here (an event handler), not in the effect:
      // synchronous setState inside an effect body is a lint error.
      refresh: () => {
        setStatus("loading");
        setErrorMessage(null);
        setNonce((n) => n + 1);
      },
    };
  }, [companies, selectedId, status, errorMessage]);

  return <CompanyContext.Provider value={value}>{children}</CompanyContext.Provider>;
}

export function useCompany(): CompanyContextValue {
  const ctx = React.useContext(CompanyContext);
  if (!ctx) {
    throw new Error("useCompany must be used inside <CompanyProvider>");
  }
  return ctx;
}

/** Persian message for a failed list/detail call (never raw JSON). */
export function apiErrorMessage(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    if (err.isUnauthorized) return "نشست به پایان رسیده است؛ دوباره وارد شوید.";
    if (err.isForbidden) return "کلید شما به این داده دسترسی ندارد.";
    if (err.code === "COMPANY_REQUIRED") return "شرکتی انتخاب نشده است.";
  }
  return fallback;
}
