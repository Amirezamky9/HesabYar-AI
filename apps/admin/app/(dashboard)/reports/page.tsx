import type { Metadata } from "next";
import { ReportsPage } from "@/features/reports/reports-page";

export const metadata: Metadata = {
  title: "گزارش‌ها | حسابیار",
  description: "خلاصهٔ فاکتورها و پرداخت‌ها و صورت‌های مالی",
};

export default function ReportsRoute() {
  return <ReportsPage />;
}
