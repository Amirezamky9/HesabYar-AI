import type { Metadata } from "next";
import { StatementImport } from "@/features/statements/statement-import";

export const metadata: Metadata = {
  title: "ایمپورت صورت‌حساب | حسابیار",
  description: "ایمپورت فایل صورت‌حساب بانکی (اکسل، CSV یا PDF) و ثبت پیش‌نویس آن",
};

export default function ImportStatementRoute() {
  return <StatementImport />;
}
