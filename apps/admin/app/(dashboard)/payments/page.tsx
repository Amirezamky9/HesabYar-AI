import { Suspense } from "react";
import type { Metadata } from "next";
import { PaymentList } from "@/features/payments/payment-list";

export const metadata: Metadata = {
  title: "پرداخت‌ها | حسابیار",
  description: "دریافت‌ها و پرداخت‌های شرکت",
};

/**
 * The list reads ?state= / ?kind= / ?q= via useSearchParams (report
 * deep-links, US-305); the Suspense boundary keeps that read out of
 * prerendering (Next 16 instant-shell rule).
 */
export default function PaymentsPage() {
  return (
    <Suspense
      fallback={
        <div className="space-y-6" aria-label="در حال بارگذاری">
          <div className="h-8 w-40 animate-pulse rounded-lg bg-muted" />
          <div className="h-64 animate-pulse rounded-lg bg-muted" />
        </div>
      }
    >
      <PaymentList />
    </Suspense>
  );
}
