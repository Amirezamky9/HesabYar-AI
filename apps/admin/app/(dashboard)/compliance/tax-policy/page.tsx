import type { Metadata } from "next";
import { TaxPolicyPage } from "@/features/compliance/tax-policy-page";

export const metadata: Metadata = {
  title: "سیاست مالیاتی | حسابیار",
  description: "نسخه‌های سیاست مالیاتی با تاریخ اعتبار و وضعیت تأیید؛ تعیین تصمیم مالیاتی",
};

export default function TaxPolicyRoute() {
  return <TaxPolicyPage />;
}
