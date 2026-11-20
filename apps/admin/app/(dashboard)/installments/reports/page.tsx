import type { Metadata } from "next";
import { InstallmentReportsPage } from "@/features/installments/installment-reports-page";

export const metadata: Metadata = {
  title: "گزارش‌های اقساط",
  description: "۱۱ گزارش اقساط: معوق، پیش‌بینی وصول، سود تأمین مالی، پیگیری، BNPL در برابر داخلی و قدمت",
};

export default function InstallmentReportsRoute() {
  return <InstallmentReportsPage />;
}
