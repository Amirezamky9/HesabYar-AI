/**
 * Dashboard home (US-551) — the unified dashboard: six live sections
 * (§7.3) + the overview widgets. The client home fetches through the
 * BFF under the shell's company context; this page is a thin server
 * wrapper.
 */

import type { Metadata } from "next";
import { DashboardHome } from "@/features/dashboard/dashboard-home";

export const metadata: Metadata = {
  title: "داشبورد | حسابیار",
  description: "نمای کلی مالی: پولِ امروز، پولِ در راه، صف بازبینی، خزانه، هشدارها و ایمپورت‌های دستی",
};

export default function DashboardPage() {
  return <DashboardHome />;
}
