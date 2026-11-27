import type { Metadata } from "next";
import { InstallmentCenterPage } from "@/features/installments/installment-center-page";

export const metadata: Metadata = {
  title: "مرکز اقساط",
  description: "برنامه‌های قسط داخلی و BNPL، فیلتر بر اساس نوع، وضعیت، فاکتور و مشتری",
};

export default function InstallmentsPage() {
  return <InstallmentCenterPage />;
}
