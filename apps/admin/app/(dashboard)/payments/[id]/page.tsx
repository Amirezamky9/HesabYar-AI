import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { PaymentDetail } from "@/features/payments/payment-detail";

export const metadata: Metadata = {
  title: "جزئیات پرداخت | حسابیار",
  description: "جزئیات یک پرداخت: طرف حساب، مبلغ و وضعیت",
};

export default async function PaymentDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const paymentId = Number(id);
  if (!Number.isInteger(paymentId) || paymentId <= 0) {
    notFound();
  }
  return <PaymentDetail paymentId={paymentId} />;
}
