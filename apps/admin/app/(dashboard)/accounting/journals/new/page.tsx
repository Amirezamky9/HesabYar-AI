import type { Metadata } from "next";
import { CreateJournalPage } from "@/components/journal-entries/create-journal-page";

export const metadata: Metadata = {
  title: "سند جدید | حسابیار",
  description: "ایجاد پیش‌نویس سند حسابداری با ردیف‌های بدهکار و بستانکار",
};

export default function NewJournalPage() {
  return <CreateJournalPage />;
}
