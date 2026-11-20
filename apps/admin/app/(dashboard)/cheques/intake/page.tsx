import type { Metadata } from "next";
import { ChequeIntakePage } from "@/features/cheques/cheque-intake-page";

export const metadata: Metadata = {
  title: "در انتظار برگ",
  description: "صف چک‌های ثبت‌شده در صیاد که برگ فیزیکی‌شان هنوز نرسیده است",
};

export default function ChequeIntakeRoute() {
  return <ChequeIntakePage />;
}
