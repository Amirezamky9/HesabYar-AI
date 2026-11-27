/**
 * Honest coming-soon card for the reports page (US-305): no number,
 * no fake figure — just the title and the backend gap that blocks it.
 * Children may add a link to the closest live view (e.g. the drafts
 * list for the posted-move ledger).
 */

"use client";

import * as React from "react";
import { CircleAlert } from "lucide-react";
import { Badge, Card, CardContent, CardHeader, CardTitle } from "@/components/ui";

export function ReportComingSoon({
  title,
  reason,
  testId,
  children,
}: {
  title: string;
  reason: string;
  testId: string;
  children?: React.ReactNode;
}) {
  return (
    <Card data-testid={testId}>
      <CardHeader>
        <div className="flex items-center gap-2">
          <CardTitle>{title}</CardTitle>
          <Badge variant="outline">به‌زودی</Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="flex items-start gap-2 text-sm text-muted-foreground">
          <CircleAlert className="mt-1 size-4 shrink-0" strokeWidth={1.5} aria-hidden />
          {reason}
        </p>
        {children}
      </CardContent>
    </Card>
  );
}
