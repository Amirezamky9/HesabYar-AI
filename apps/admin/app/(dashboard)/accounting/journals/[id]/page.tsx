import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { JournalDetail } from "@/components/journal-entries/journal-detail";

export const metadata: Metadata = {
  title: "جزئیات سند | حسابیار",
  description: "مشاهده و ویرایش پیش‌نویس سند، بررسی اعتبار، ثبت و برگشت",
};

/**
 * This page awaits `params`/`searchParams` (the journal id and the
 * optional `?origin=` link). Under cacheComponents that URL data must
 * not be read during instant-navigation validation — without this opt
 * out, a client-side navigation here (e.g. after a reversal) errors
 * and leaves both the old and the new page mounted. Same pattern as
 * the dashboard layout's `instant = false`.
 */
export const instant = false;

interface JournalDetailPageProps {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ origin?: string }>;
}

export default async function JournalDetailPage({ params, searchParams }: JournalDetailPageProps) {
  const { id } = await params;
  const { origin } = await searchParams;
  const numericId = Number(id);
  if (!Number.isInteger(numericId) || numericId <= 0) {
    notFound();
  }
  const originId = origin !== undefined ? Number(origin) : NaN;
  return (
    <JournalDetail
      id={numericId}
      originId={Number.isInteger(originId) && originId > 0 ? originId : null}
    />
  );
}
