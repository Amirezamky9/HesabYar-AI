import type { Metadata } from "next";
import { SettlementImportDetail } from "@/features/settlement/settlement-import-detail";

export const metadata: Metadata = {
  title: "جزئیات ایمپورت تسویه",
  description: "ردیف‌ها، اثر حسابداری، واریزهای بانکی و tie-out یک ایمپورت تسویه",
};

export default async function SettlementImportDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <SettlementImportDetail importId={id} />;
}
