import type { Metadata } from "next";
import { InstallmentNewPage } from "@/features/installments/installment-new-page";

export const metadata: Metadata = {
  title: "برنامهٔ قسط جدید",
  description: "ساخت برنامهٔ قسط برای یک فاکتور فروش — از شرایط پرداخت یا با اقساط صریح",
};

export default function InstallmentNewRoute() {
  return <InstallmentNewPage />;
}
