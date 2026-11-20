/**
 * Fiscal years route (US-302) — سال‌های مالی، دوره‌های مالی و وضعیت
 * راه‌اندازی در یک صفحه با سه زبانه. Thin server wrapper; the client
 * view fetches through the BFF under the shell's company context.
 */

import type { Metadata } from "next";
import { FiscalView } from "@/features/chart-of-accounts/fiscal-view";

export const metadata: Metadata = {
  title: "سال‌های مالی | حسابیار",
  description: "سال‌ها و دوره‌های مالی و وضعیت راه‌اندازی نمودار حساب‌ها",
};

export default function FiscalYearsRoutePage() {
  return <FiscalView />;
}
