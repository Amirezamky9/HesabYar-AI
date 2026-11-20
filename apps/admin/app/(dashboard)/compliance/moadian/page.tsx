import type { Metadata } from "next";
import { MoadianPage } from "@/features/compliance/moadian-page";

export const metadata: Metadata = {
  title: "مودیان | حسابیار",
  description: "پروفایل‌ها، اسناد و ارسال‌های آزمایشی سامانهٔ مودیان با شبیه‌ساز",
};

export default function MoadianRoute() {
  return <MoadianPage />;
}
