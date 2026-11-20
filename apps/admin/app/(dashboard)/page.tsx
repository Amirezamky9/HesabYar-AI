/**
 * Dashboard home (US-302) — §11.3 widgets from live API data only.
 * The client home fetches through the BFF under the shell's company
 * context; this page is a thin server wrapper.
 */

import type { Metadata } from "next";
import { DashboardHome } from "@/features/dashboard/dashboard-home";

export const metadata: Metadata = {
  title: "داشبورد | حسابیار",
  description: "نمای کلی مالی: پیش‌نویس‌های ثبت‌نشده، مطالبات، بدهی‌ها و آخرین صورت‌حساب‌ها و پرداخت‌ها",
};

export default function DashboardPage() {
  return <DashboardHome />;
}
