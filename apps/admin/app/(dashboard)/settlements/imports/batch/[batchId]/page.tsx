import type { Metadata } from "next";
import { ImportBatchDetail } from "@/features/imports/import-batch-detail";

export const metadata: Metadata = {
  title: "بچ ایمپورت تسویه",
  description: "مراحل، پیش‌نمایش اثر حسابداری و گزارش مغایرت یک بچ ایمپورت تسویه",
};

export default async function SettlementImportBatchPage({
  params,
}: {
  params: Promise<{ batchId: string }>;
}) {
  const { batchId } = await params;
  return (
    <ImportBatchDetail
      batchId={batchId}
      backHref="/settlements"
      backLabel="بازگشت به مرکز تسویه"
    />
  );
}
