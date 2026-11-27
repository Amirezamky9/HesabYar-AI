import type { Metadata } from "next";
import { MonitoringPanel } from "@/features/monitoring/monitoring-panel";

export const metadata: Metadata = {
  title: "پایش سیستم | حسابیار",
  description:
    "سلامت فرایندها، صف‌ها و هشدارها، انقضای توکن‌ها و وضعیت پشتیبان‌گیری",
};

export default function MonitoringRoute() {
  return <MonitoringPanel />;
}
