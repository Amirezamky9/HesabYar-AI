import type { Metadata } from "next";
import { RulesPage } from "@/features/rules/rules-page";

export const metadata: Metadata = {
  title: "گردش‌کارها | حسابیار",
  description: "قوانین خودکار: وقتی یک رویداد رخ می‌دهد و شرط‌ها برقرارند، اقدام انجام می‌شود — با آزمایش پیش از فعال‌کردن و تاریخچهٔ کامل اجراها",
};

export default function WorkflowsRoute() {
  return <RulesPage />;
}
