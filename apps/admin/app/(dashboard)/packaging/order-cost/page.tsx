import type { Metadata } from "next";
import { OrderCostPage } from "@/features/packaging/order-cost-page";

export const metadata: Metadata = {
  title: "هزینهٔ کامل سفارش | حسابیار",
  description: "بهای تمام‌شدهٔ هر سفارش از یک مرکز هزینه: کالا، بسته‌بندی و ارسال",
};

export default function PackagingOrderCostRoute() {
  return <OrderCostPage />;
}
