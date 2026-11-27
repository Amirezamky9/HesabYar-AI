"use client";

/**
 * Toast (facade) — FarsiUI Sonner underneath.
 *
 * Mount <Toaster /> once in the dashboard layout. Feature pages call
 * `toast.success("...")` / `toast.error("...")` with Persian strings.
 * Position is bottom-center: centered toasts read the same in RTL and LTR.
 */

import { Toaster as SonnerToaster } from "./sonner";
import { toast as sonnerToast } from "sonner";

export function Toaster() {
  return <SonnerToaster position="bottom-center" closeButton richColors={false} />;
}

/** Imperative toast API (sonner). All strings must be Persian. */
export const toast = {
  success: (message: string, opts?: { description?: string }) =>
    sonnerToast.success(message, { description: opts?.description }),
  error: (message: string, opts?: { description?: string }) =>
    sonnerToast.error(message, { description: opts?.description }),
  info: (message: string, opts?: { description?: string }) =>
    sonnerToast.info(message, { description: opts?.description }),
  warning: (message: string, opts?: { description?: string }) =>
    sonnerToast.warning(message, { description: opts?.description }),
  loading: (message: string) => sonnerToast.loading(message),
  dismiss: (id?: string | number) => sonnerToast.dismiss(id),
};

export { sonnerToast };
