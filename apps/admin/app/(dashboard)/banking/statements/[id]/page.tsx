import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { StatementDetail } from "@/features/statements/statement-detail";

export const metadata: Metadata = {
  title: "جزئیات صورت‌حساب بانکی | حسابیار",
  description: "جزئیات یک صورت‌حساب بانکی: ردیف‌ها، مانده‌ها و وضعیت تطبیق",
};

export default async function StatementDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const statementId = Number(id);
  if (!Number.isInteger(statementId) || statementId <= 0) {
    notFound();
  }
  return <StatementDetail statementId={statementId} />;
}
