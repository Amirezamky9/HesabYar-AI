import type { Metadata } from "next";
import { ChequeCenterPage } from "@/features/cheques/cheque-center-page";

export const metadata: Metadata = {
  title: "مرکز چک",
  description: "رجیستری چک‌های دریافتی و پرداختی، فیلتر بر اساس وضعیت، نوع، هدف و بانک",
};

export default function ChequesPage() {
  return <ChequeCenterPage />;
}
