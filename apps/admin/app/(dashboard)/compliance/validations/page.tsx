import type { Metadata } from "next";
import { ValidationsPage } from "@/features/compliance/validations-page";

export const metadata: Metadata = {
  title: "اعتبارسنجی‌ها | حسابیار",
  description: "اعتبارسنجی تراز آزمایشی زنده بر اساس استانداردها، با نتیجهٔ هر قاعده و منشأ ارقام",
};

export default function ValidationsRoute() {
  return <ValidationsPage />;
}
