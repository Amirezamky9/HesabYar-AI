import type { Metadata } from "next";
import { CashBoxPage } from "@/features/cash-box/cash-box-page";

export const metadata: Metadata = {
  title: "صندوق‌ها | حسابیار",
  description: "شمارش صندوق با حضور ناظر، صورتجلسهٔ امضاشده و سند کسری و اضافه پس از تأیید مدیر",
};

export default function CashBoxesRoute() {
  return <CashBoxPage />;
}
