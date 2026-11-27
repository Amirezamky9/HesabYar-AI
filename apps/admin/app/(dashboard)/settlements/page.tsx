import type { Metadata } from "next";
import { SettlementCenterPage } from "@/features/settlement/settlement-center-page";

export const metadata: Metadata = {
  title: "گزارش تسویه",
  description: "گزارش تسویهٔ کانال‌های پرداخت، دقت کارمزد و استثناها",
};

export default function SettlementsPage() {
  return <SettlementCenterPage />;
}
