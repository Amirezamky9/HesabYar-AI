import type { Metadata } from "next";
import { JournalList } from "@/components/journal-entries/journal-list";

export const metadata: Metadata = {
  title: "دفتر روزنامه | حسابیار",
  description: "فهرست پیش‌نویس‌های سند حسابداری — جست‌وجو، فیلتر و ثبت سند",
};

export default function JournalsPage() {
  return <JournalList />;
}
