import type { Metadata } from "next";
import { CheckbooksPage } from "@/features/cheques/checkbooks-page";

export const metadata: Metadata = {
  title: "دسته‌چک‌ها",
  description: "دسته‌چک‌های شرکت با برگ‌های مصرف‌شده، باطل‌شده و باقی‌مانده",
};

export default function CheckbooksRoute() {
  return <CheckbooksPage />;
}
