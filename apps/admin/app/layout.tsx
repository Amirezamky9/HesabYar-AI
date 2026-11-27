import type { Metadata } from "next";
import "../styles/globals.css";
import { vazirmatn } from "./fonts";
import { Providers } from "@/components/providers";

export const metadata: Metadata = {
  title: "حسابیار | پنل مدیریت",
  description: "پنل مدیریت حسابداری حسابیار — دفتر روزنامه، صورت‌حساب، پرداخت و گزارش‌ها",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="fa" dir="rtl" data-theme="paper" className={`${vazirmatn.variable} h-full antialiased`} suppressHydrationWarning>
      <body className="min-h-full bg-background font-sans text-foreground">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
