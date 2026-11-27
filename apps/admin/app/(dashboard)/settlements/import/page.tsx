import type { Metadata } from "next";
import { SettlementImport } from "@/features/settlement/settlement-import";

export const metadata: Metadata = {
  title: "ایمپورت فایل تسویه",
  description: "ایمپورت گزارش تسویهٔ درگاه: بارگذاری، اعتبارسنجی، پیش‌نمایش اثر حسابداری و تأیید",
};

export default function SettlementImportPage() {
  return <SettlementImport />;
}
