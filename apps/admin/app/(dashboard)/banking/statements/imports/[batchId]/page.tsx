import type { Metadata } from "next";
import { ImportBatchDetail } from "@/features/imports/import-batch-detail";

export const metadata: Metadata = {
  title: "بچ ایمپورت صورت‌حساب",
  description: "مراحل، پیش‌نمایش اثر حسابداری و گزارش مغایرت یک بچ ایمپورت صورت‌حساب",
};

export default async function StatementImportBatchPage({
  params,
}: {
  params: Promise<{ batchId: string }>;
}) {
  const { batchId } = await params;
  return (
    <ImportBatchDetail
      batchId={batchId}
      backHref="/banking/statements"
      backLabel="بازگشت به صورت‌حساب‌های بانکی"
    />
  );
}
