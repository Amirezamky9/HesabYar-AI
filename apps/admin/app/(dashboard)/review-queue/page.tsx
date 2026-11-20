import type { Metadata } from "next";
import { ReviewQueuePage } from "@/features/review-queue/review-queue-page";

export const metadata: Metadata = {
  title: "صف بازبینی | حسابیار",
  description: "یک صف واحد برای همهٔ استثناها: نگاشت، تسویه، چک، بانک، بسته‌بندی، اقساط، ایمپورت، صندوق و رویدادهای مرده",
};

export default async function ReviewQueueRoute({
  searchParams,
}: {
  searchParams?: Promise<{ type?: string }>;
}) {
  const params = (await searchParams) ?? {};
  return <ReviewQueuePage initialType={params.type ?? null} />;
}
