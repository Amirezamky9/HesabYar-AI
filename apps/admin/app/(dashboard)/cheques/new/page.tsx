import type { Metadata } from "next";
import { ChequeNewPage } from "@/features/cheques/cheque-new-page";

export const metadata: Metadata = {
  title: "ثبت چک",
  description: "ثبت دستی چک دریافتی یا پرداختی با شناسهٔ صیاد و مبلغ به تومان",
};

export default function ChequeNewRoute() {
  return <ChequeNewPage />;
}
