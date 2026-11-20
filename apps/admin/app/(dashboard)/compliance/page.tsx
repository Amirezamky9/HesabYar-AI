import type { Metadata } from "next";
import { ComplianceOverviewPage } from "@/features/compliance/compliance-overview";

export const metadata: Metadata = {
  title: "استانداردها و انطباق | حسابیار",
  description: "مرکز انطباق: استانداردها، سیاست مالیاتی، اعتبارسنجی‌ها و مودیان",
};

export default function ComplianceRoute() {
  return <ComplianceOverviewPage />;
}
