import type { Metadata } from "next";
import { McpClientsPage } from "@/features/mcp-clients/mcp-clients-page";

export const metadata: Metadata = {
  title: "کلاینت‌های MCP | حسابیار",
  description:
    "میزبان‌های هوش مصنوعی مجاز به اتصال MCP: صدور و چرخش کلید، ابطال، و گزارش دسترسی",
};

export default function McpClientsRoute() {
  return <McpClientsPage />;
}
