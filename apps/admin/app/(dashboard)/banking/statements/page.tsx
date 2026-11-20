import type { Metadata } from "next";
import { StatementList } from "@/features/statements/statement-list";

export const metadata: Metadata = {
  title: "صورت‌حساب‌های بانکی | حسابیار",
  description: "صورت‌حساب‌های بانک و صندوق شرکت",
};

export default function StatementsPage() {
  return <StatementList />;
}
