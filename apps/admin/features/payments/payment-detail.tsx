/**
 * Payment detail panel (US-304). Read-only: the API exposes no payment
 * create/approve yet, so this page shows the payment and nothing else.
 * The "method" the API provides is the payment journal name.
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowRight } from "lucide-react";
import { ApiError, api, type PaymentResponse } from "@/lib/api-client";
import { formatIsoDate } from "@/lib/format-date";
import { fa } from "@/lib/utils";
import {
  buttonVariants,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  MoneyDisplay,
  Skeleton,
  StatusBadge,
} from "@/components/ui";
import {
  isNotFoundError,
  isPermissionDenied,
  persianApiErrorMessage,
} from "@/features/shared/api-error";
import { ErrorState, NotFoundState, PermissionDeniedState } from "@/features/shared/states";
import { PAYMENT_KIND_LABELS, PAYMENT_STATE_LABELS } from "./labels";

export function PaymentDetail({ paymentId }: { paymentId: number }) {
  const router = useRouter();
  const [payment, setPayment] = React.useState<PaymentResponse | null>(null);
  const [error, setError] = React.useState<unknown>(null);
  const [reloadKey, setReloadKey] = React.useState(0);

  React.useEffect(() => {
    let cancelled = false;
    api
      .getPayment(paymentId)
      .then((data) => {
        if (!cancelled) setPayment(data);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.replace("/login");
          return;
        }
        setError(err);
      })
      .finally(() => {
        /* loading is derived: null record + no error = loading */
      });
    return () => {
      cancelled = true;
    };
  }, [paymentId, reloadKey, router]);

  const loading = payment === null && error === null;

  if (loading) {
    return (
      <div className="space-y-6" aria-label="در حال بارگذاری">
        <Skeleton className="h-8 w-56" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (error && isPermissionDenied(error)) {
    return <PermissionDeniedState message={persianApiErrorMessage(error, "دریافت پرداخت ناموفق بود.")} />;
  }

  if (error && isNotFoundError(error)) {
    return <NotFoundState message="پرداخت موردنظر پیدا نشد." />;
  }

  if (error || !payment) {
    return (
      <ErrorState
        message={persianApiErrorMessage(error, "دریافت پرداخت ناموفق بود.")}
        onRetry={() => {
          setError(null);
          setPayment(null);
          setReloadKey((k) => k + 1);
        }}
      />
    );
  }

  const stateInfo = PAYMENT_STATE_LABELS[payment.state];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          {/* Plain Link styled as a button: Base UI's Button with
              render={<Link>} logs a nativeButton console error (US-305). */}
          <Link href="/payments" className={buttonVariants({ variant: "ghost", size: "icon" })} aria-label="بازگشت به پرداخت‌ها">
            <ArrowRight className="size-4" aria-hidden />
          </Link>
          <div>
            <h1 className="text-2xl font-bold">
              پرداخت{" "}
              <span dir="ltr" className="ltr-isolate">
                {payment.number}
              </span>
            </h1>
            <p className="mt-1 text-sm text-muted-foreground">
              {PAYMENT_KIND_LABELS[payment.kind] ?? payment.kind}
              {" · "}
              {payment.party_name}
            </p>
          </div>
        </div>
        {stateInfo ? (
          <StatusBadge status={payment.state} label={stateInfo.label} tone={stateInfo.tone} />
        ) : (
          <StatusBadge status={payment.state} />
        )}
      </div>

      <Card>
        <CardHeader>
          <CardTitle>مشخصات پرداخت</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid gap-4 sm:grid-cols-2">
            <div>
              <dt className="text-sm text-muted-foreground">طرف حساب</dt>
              <dd className="mt-1 font-medium">{payment.party_name}</dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">تاریخ</dt>
              <dd className="mt-1 font-medium">{formatIsoDate(payment.date)}</dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">دفتر پرداخت</dt>
              <dd className="mt-1 font-medium">{payment.journal_name}</dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">مرجع</dt>
              <dd className="mt-1 font-medium" dir="ltr">
                {payment.reference || "—"}
              </dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">شناسه</dt>
              <dd className="mt-1 font-medium tabular-nums">{fa(payment.id)}</dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">مبلغ</dt>
              <dd className="mt-1">
                <MoneyDisplay amountIrr={payment.amount} size="sm" />
              </dd>
            </div>
          </dl>
        </CardContent>
      </Card>
    </div>
  );
}
