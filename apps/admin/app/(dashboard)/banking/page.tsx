/**
 * Banking section landing (US-516) — the section has no page of its
 * own; the breadcrumb's «بانک و صندوق» link lands on the statements
 * list. Without this page the breadcrumb prefetches a route that
 * does not exist (404).
 */

import { redirect } from "next/navigation";

export default function BankingIndexPage() {
  redirect("/banking/statements");
}
