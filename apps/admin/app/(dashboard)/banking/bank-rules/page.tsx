import type { Metadata } from "next";
import { BankRulesPage } from "@/features/bank-rules/bank-rules-page";

export const metadata: Metadata = {
  title: "قواعد تطبیق | حسابیار",
  description: "قواعد خودکار کدگذاری ردیف‌های صورت‌حساب بانکی",
};

export default function BankRulesRoutePage() {
  return <BankRulesPage />;
}
