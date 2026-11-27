import type { Metadata } from "next";
import { SettlementContractsPage } from "@/features/settlement/settlement-contracts-page";

export const metadata: Metadata = {
  title: "قراردادهای درگاه",
  description: "مدیریت نسخه‌های قرارداد کانال‌های پرداخت: نرخ کارمزد، مدل تسویه و قسط‌ها",
};

export default function SettlementContractsPageRoute() {
  return <SettlementContractsPage />;
}
