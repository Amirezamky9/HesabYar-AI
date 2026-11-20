import type { Metadata } from "next";
import { SettlementPaymentDetail } from "@/features/settlement/settlement-payment-detail";

export const metadata: Metadata = {
  title: "جزئیات پرداخت درگاه",
  description: "پرداخت درگاه، برنامهٔ قسط‌ها، رکورد کارمزد و سندهای تسویه",
};

export default async function SettlementPaymentDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <SettlementPaymentDetail paymentId={id} />;
}
