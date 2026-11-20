import type { Metadata } from "next";
import { SettlementSchedulePage } from "@/features/settlement/settlement-schedule-page";

export const metadata: Metadata = {
  title: "برنامهٔ قسط‌ها",
  description: "برنامهٔ اقساط تسویهٔ کانال‌های پرداخت و هشدارهای سررسید",
};

export default async function SettlementsSchedulePage({
  searchParams,
}: {
  searchParams?: Promise<{ channel?: string; period?: string; state?: string }>;
}) {
  const params = (await searchParams) ?? {};
  return (
    <SettlementSchedulePage
      initial={{
        channel: params.channel,
        period: params.period,
        state: params.state,
      }}
    />
  );
}
