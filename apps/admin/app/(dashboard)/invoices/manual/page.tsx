import type { Metadata } from "next";
import { ManualEntriesPage } from "@/features/manual-entries/manual-entries-page";

export const metadata: Metadata = {
  title: "فاکتور جدید (دستی) | حسابیار",
  description:
    "ورود دستی سفارش، برگشت و لغو سفارش — همان رویداد متعارف فروشگاه، از همان خط لوله: اعتبارسنجی، نگاشت، دروازهٔ سیاست و ممیزی",
};

export default function ManualEntriesRoute() {
  return <ManualEntriesPage />;
}
