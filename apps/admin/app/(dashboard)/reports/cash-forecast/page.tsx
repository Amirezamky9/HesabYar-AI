import type { Metadata } from "next";
import { CashForecastPage } from "@/features/cash-forecast/cash-forecast-page";

export const metadata: Metadata = {
  title: "پیش‌بینی جریان نقد | حسابیار",
  description: "تقویم ماهانهٔ ورودی‌ها و خروجی‌های موردانتظار و پیش‌بینی ۳۰ تا ۱۲۰ روزه",
};

export default function CashForecastRoute() {
  return <CashForecastPage />;
}
