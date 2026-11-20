/**
 * Accounting section landing (US-302) — the section has no page of its
 * own; the breadcrumb's «حسابداری» link lands on the chart of accounts.
 */

import { redirect } from "next/navigation";

export default function AccountingIndexPage() {
  redirect("/accounting/accounts");
}
