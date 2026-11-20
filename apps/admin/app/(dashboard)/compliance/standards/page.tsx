import type { Metadata } from "next";
import { StandardsPage } from "@/features/compliance/standards-page";

export const metadata: Metadata = {
  title: "استانداردهای حسابداری | حسابیار",
  description: "رجیستری استانداردهای حسابداری ایران با وضعیت تأیید هر بازبینی",
};

export default function StandardsRoute() {
  return <StandardsPage />;
}
