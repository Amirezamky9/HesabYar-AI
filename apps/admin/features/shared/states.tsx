/**
 * Shared page states for the US-304 feature pages: error (with retry),
 * permission-denied (403), and not-found (404). All copy is Persian.
 */

"use client";

import { Button } from "@/components/ui";

export function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div role="alert" className="space-y-4 rounded-lg border border-destructive/40 bg-destructive/5 p-6 text-center">
      <p className="text-sm text-destructive">{message}</p>
      <Button variant="outline" onClick={onRetry}>
        تلاش دوباره
      </Button>
    </div>
  );
}

export function PermissionDeniedState({ message }: { message: string }) {
  return (
    <div role="alert" className="space-y-2 rounded-lg border p-6 text-center">
      <p className="font-medium">دسترسی ندارید</p>
      <p className="text-sm text-muted-foreground">{message}</p>
      <p className="text-sm text-muted-foreground">برای دسترسی به این بخش، با مدیر سیستم تماس بگیرید.</p>
    </div>
  );
}

export function NotFoundState({ message }: { message: string }) {
  return (
    <div role="alert" className="space-y-2 rounded-lg border p-6 text-center">
      <p className="font-medium">پیدا نشد</p>
      <p className="text-sm text-muted-foreground">{message}</p>
    </div>
  );
}
