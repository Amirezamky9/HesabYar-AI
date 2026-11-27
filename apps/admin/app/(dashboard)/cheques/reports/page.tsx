import type { Metadata } from "next";
import { ChequeReportsPage } from "@/features/cheques/cheque-reports-page";

export const metadata: Metadata = {
  title: "گزارش‌های چک",
  description: "۱۷ گزارش چک: سررسید، پرداختی، برگشتی، وصول، ظهرنویسی، پشتوانه و پیش‌بینی نقد",
};

export default function ChequeReportsRoute() {
  return <ChequeReportsPage />;
}
