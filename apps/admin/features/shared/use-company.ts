/**
 * Company resolution for the US-304 list pages.
 *
 * The invoice/payment/statement list endpoints are company-scoped: an
 * unrestricted API key gets a 400 COMPANY_REQUIRED without the `company`
 * query parameter (docs/rest-api.md). This hook lists the operator's
 * companies once and auto-selects the first; when several exist the
 * page shows a picker (CompanyPicker).
 */

import * as React from "react";
import { api, type CompanyResponse } from "@/lib/api-client";

export interface UseCompanyResult {
  companies: CompanyResponse[];
  /** Selected company id (HesabYar API id), or null while loading. */
  companyId: string | null;
  setCompanyId: (id: string) => void;
  loading: boolean;
  error: unknown;
}

export function useCompany(): UseCompanyResult {
  const [companies, setCompanies] = React.useState<CompanyResponse[]>([]);
  const [companyId, setCompanyId] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<unknown>(null);

  React.useEffect(() => {
    let cancelled = false;
    api
      .listCompanies({ limit: 50 })
      .then((page) => {
        if (cancelled) return;
        setCompanies(page.items);
        if (page.items.length > 0) setCompanyId(page.items[0].id);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err);
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return { companies, companyId, setCompanyId, loading, error };
}
