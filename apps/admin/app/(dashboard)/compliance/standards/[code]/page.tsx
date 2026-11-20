import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { StandardDetailPage } from "@/features/compliance/standard-detail";

export const metadata: Metadata = {
  title: "جزئیات استاندارد | حسابیار",
  description: "بازبینی‌ها، منشأ و قواعد ماشین‌خوان یک استاندارد حسابداری",
};

export const instant = false;

interface StandardDetailRouteProps {
  params: Promise<{ code: string }>;
}

export default async function StandardDetailRoute({ params }: StandardDetailRouteProps) {
  const { code } = await params;
  if (!/^\d+$/.test(code)) {
    notFound();
  }
  return <StandardDetailPage standardCode={code} />;
}
