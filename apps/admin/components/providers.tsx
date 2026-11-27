"use client";

/**
 * Client providers for the whole admin app: theme (light default, dark
 * opt-in via `.dark` class), tooltips and the toast host.
 */

import { ThemeProvider } from "next-themes";
import { Toaster, TooltipProvider } from "@/components/ui";

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <ThemeProvider attribute="class" defaultTheme="light" enableSystem={false} disableTransitionOnChange>
      <TooltipProvider>
        {children}
        <Toaster />
      </TooltipProvider>
    </ThemeProvider>
  );
}
