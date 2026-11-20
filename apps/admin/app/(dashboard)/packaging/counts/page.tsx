import type { Metadata } from "next";
import { CountsPage } from "@/features/packaging/counts-page";

export const metadata: Metadata = {
  title: "شمارش دوره‌ای مواد | حسابیار",
  description: "موجودی موردانتظار در برابر شمارش واقعی مواد بسته‌بندی",
};

export default function PackagingCountsRoute() {
  return <CountsPage />;
}
