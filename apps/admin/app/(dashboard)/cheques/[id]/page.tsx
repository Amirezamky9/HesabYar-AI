import type { Metadata } from "next";
import { ChequeDetailPage } from "@/features/cheques/cheque-detail-page";

export const metadata: Metadata = {
  title: "جزئیات چک",
  description: "مشخصات چک، خط زمانی چرخهٔ عمر، لاگ نگهداری، رسیدهای استعلام و گذارها",
};

export default async function ChequeDetailRoute({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <ChequeDetailPage chequeId={id} />;
}
