import type { Metadata } from "next";
import { MaterialsPage } from "@/features/packaging/materials-page";

export const metadata: Metadata = {
  title: "انبار و بسته‌بندی | حسابیار",
  description: "مواد بسته‌بندی، موجودی و پوشش روزها — «کی تمام می‌شود؟»",
};

export default function PackagingRoute() {
  return <MaterialsPage />;
}
