import type { Metadata } from "next";
import { InvoiceForm } from "@/features/invoices/invoice-form";

export const metadata: Metadata = {
  title: "فاکتور جدید | حسابیار",
  description: "ساخت پیش‌نویس فاکتور جدید",
};

export default function NewInvoicePage() {
  return <InvoiceForm />;
}
