import type { Metadata } from "next";
import { CalculatorPage } from "@/features/packaging/calculator-page";

export const metadata: Metadata = {
  title: "محاسبه‌گر قیمت | حسابیار",
  description: "قیمت تقریبی سفارش شامل بسته‌بندی و ارسال",
};

export default function PackagingCalculatorRoute() {
  return <CalculatorPage />;
}
