import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { InvoiceDetail } from "@/features/invoices/invoice-detail";

export const metadata: Metadata = {
  title: "جزئیات فاکتور | حسابیار",
  description: "جزئیات یک فاکتور: طرف حساب، ردیف‌ها و مبالغ",
};

export default async function InvoiceDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const invoiceId = Number(id);
  if (!Number.isInteger(invoiceId) || invoiceId <= 0) {
    notFound();
  }
  return <InvoiceDetail invoiceId={invoiceId} />;
}
