/**
 * Chart of accounts route (US-302). Thin server wrapper; the client
 * page fetches through the BFF under the shell's company context.
 */

import type { Metadata } from "next";
import { AccountsPage } from "@/features/chart-of-accounts/accounts-page";

export const metadata: Metadata = {
  title: "سر فصل حساب‌ها | حسابیار",
  description: "نمودار حساب‌ها: درخت سلسله‌مراتبی، جستجو با کد یا نام و فیلتر نوع حساب",
};

export default function AccountsRoutePage() {
  return <AccountsPage />;
}
