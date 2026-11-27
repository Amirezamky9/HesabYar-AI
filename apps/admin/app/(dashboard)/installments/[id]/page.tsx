import type { Metadata } from "next";
import { InstallmentDetailPage } from "@/features/installments/installment-detail-page";

export const metadata: Metadata = {
  title: "جزئیات برنامهٔ قسط",
  description: "جدول سررسید، دریافت‌ها، نقل‌قول تسویه و تاریخچهٔ تغییرات برنامهٔ قسط",
};

export default async function InstallmentDetailRoute({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <InstallmentDetailPage planId={id} />;
}
