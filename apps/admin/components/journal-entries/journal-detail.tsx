"use client";

/**
 * Journal detail page (US-303): one draft or posted journal.
 *
 * Drafts show the lines read-only with an «ویرایش» switch into the
 * editor, plus validate/post actions. Posted journals are read-only —
 * no edit/delete path exists (§19 rule 8) — with a reverse action that
 * links to the reversal. The audit trail lists the create/post/reverse
 * events recorded for this journal (hidden when the key lacks the
 * audit scope).
 */

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowRight, History, Pencil } from "lucide-react";
import {
  api,
  ApiError,
  type AccountResponse,
  type AuditEventResponse,
  type JournalDraftResponse,
  type PostedJournalResponse,
  type ValidationIssueResponse,
} from "@/lib/api-client";
import { formatIsoDate, formatIsoDateTime } from "@/lib/format-date";
import { fa } from "@/lib/utils";
import {
  apiErrorMessage,
  auditActionLabel,
  validationIssueMessage,
} from "@/features/journal-entries/lib/error-messages";
import { fetchAllAccounts, fetchJournalAuditEvents } from "@/features/journal-entries/lib/journal-api";
import {
  Badge,
  Button,
  buttonVariants,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  MoneyDisplay,
  Skeleton,
  StatusBadge,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui";
import { JournalActions } from "./journal-actions";
import { JournalEditor } from "./journal-editor";

export interface JournalDetailProps {
  id: number;
  /** Engine id of the journal this one reverses (from ?origin=, when present). */
  originId?: number | null;
}

export function JournalDetail({ id, originId = null }: JournalDetailProps) {
  const router = useRouter();
  const [draft, setDraft] = React.useState<JournalDraftResponse | null>(null);
  const [posted, setPosted] = React.useState<PostedJournalResponse | null>(null);
  const [accounts, setAccounts] = React.useState<Map<number, AccountResponse>>(new Map());
  const [auditEvents, setAuditEvents] = React.useState<AuditEventResponse[] | null>(null);
  const [issues, setIssues] = React.useState<ValidationIssueResponse[] | null>(null);
  const [editing, setEditing] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const loading = draft === null && error === null;

  // State changes only inside the promise chain (never synchronously in
  // an effect body) — see react-hooks/set-state-in-effect. A failed
  // chart load is non-fatal: lines fall back to showing the account id.
  const fetchDraft = React.useCallback(() => {
    api
      .getJournalDraft(id)
      .then((found) => {
        setDraft(found);
        fetchAllAccounts(found.company_id)
          .then((chart) => setAccounts(new Map(chart.map((a) => [a.id, a]))))
          .catch(() => setAccounts(new Map()));
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError && err.isUnauthorized) {
          router.replace("/login");
          return;
        }
        setError(apiErrorMessage(err));
      });
  }, [id, router]);

  React.useEffect(() => {
    fetchDraft();
  }, [fetchDraft]);

  const handleRetry = () => {
    setError(null);
    setDraft(null);
    fetchDraft();
  };

  // Audit trail: best-effort. A 403 (no settings:manage scope) simply
  // hides the panel; other failures are ignored the same way.
  React.useEffect(() => {
    if (!draft) return;
    let cancelled = false;
    fetchJournalAuditEvents(id, draft.company_id)
      .then((events) => {
        if (!cancelled) setAuditEvents(events);
      })
      .catch(() => {
        if (!cancelled) setAuditEvents([]);
      });
    return () => {
      cancelled = true;
    };
  }, [draft, id]);

  const handlePosted = (postedJournal: PostedJournalResponse) => {
    setPosted(postedJournal);
    setIssues(null);
    fetchDraft();
  };

  const handleReversed = (reversal: PostedJournalResponse) => {
    router.push(`/accounting/journals/${reversal.id}?origin=${id}`);
  };

  if (loading) {
    return (
      <div className="space-y-4" aria-label="در حال بارگذاری">
        <Skeleton className="h-8 w-2/3" />
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }

  if (error || !draft) {
    return (
      <div className="space-y-4">
        <p role="alert" className="text-sm text-destructive">
          {error ?? "سند پیدا نشد."}
        </p>
        <Button type="button" variant="outline" onClick={handleRetry}>
          تلاش دوباره
        </Button>
      </div>
    );
  }

  const isPosted = draft.state === "posted";

  if (editing && !isPosted) {
    return (
      <div className="space-y-6">
        <Button type="button" variant="ghost" onClick={() => setEditing(false)}>
          <ArrowRight className="size-4" aria-hidden />
          بازگشت به جزئیات سند
        </Button>
        <JournalEditor
          mode="edit"
          draft={draft}
          onSaved={(saved) => {
            setDraft(saved);
            setEditing(false);
            fetchDraft();
          }}
          onCancel={() => setEditing(false)}
        />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4" data-testid="journal-header">
        <div>
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="text-2xl font-bold">سند {fa(draft.id)}</h1>
            <StatusBadge status={draft.state} />
          </div>
          <p className="mt-1 text-muted-foreground">
            {formatIsoDate(draft.date)} · دورهٔ {draft.period.name}
            {posted ? ` · شمارهٔ ${fa(posted.number)}` : ""}
            {posted?.post_date ? ` · ثبت‌شده در ${formatIsoDate(posted.post_date)}` : ""}
          </p>
          {draft.description ? <p className="mt-2">{draft.description}</p> : null}
          {originId !== null ? (
            <p className="mt-2 text-sm text-muted-foreground">
              این سند، برگشت{" "}
              <Link href={`/accounting/journals/${originId}`} className="font-medium text-primary underline">
                سند {fa(originId)}
              </Link>{" "}
              است.
            </p>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          {!isPosted ? (
            <Button type="button" variant="outline" onClick={() => setEditing(true)}>
              <Pencil className="size-4" aria-hidden />
              ویرایش
            </Button>
          ) : null}
          <Link href="/accounting/journals" className={buttonVariants({ variant: "ghost" })}>
            <ArrowRight className="size-4" aria-hidden />
            فهرست سندها
          </Link>
        </div>
      </div>

      {issues && issues.length > 0 ? (
        <div role="alert" className="rounded-lg border border-warning/50 bg-warning/10 p-4">
          <p className="mb-2 text-sm font-medium">سند ایراد دارد:</p>
          <ul className="list-disc space-y-1 ps-5 text-sm">
            {issues.map((issue) => (
              <li key={issue.code}>{validationIssueMessage(issue)}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>ردیف‌های سند</CardTitle>
          <CardDescription>
            جمع بدهکار <MoneyDisplay amountIrr={draft.total_debit} size="sm" /> · جمع بستانکار{" "}
            <MoneyDisplay amountIrr={draft.total_credit} size="sm" />
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto rounded-lg border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>حساب</TableHead>
                  <TableHead>بدهکار</TableHead>
                  <TableHead>بستانکار</TableHead>
                  <TableHead>شرح</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {draft.lines.map((line, index) => {
                  const account = accounts.get(line.account_id);
                  return (
                    <TableRow key={`${line.account_id}-${index}`}>
                      <TableCell>
                        {account ? (
                          <span className="flex items-center gap-2">
                            <span className="ltr-isolate tabular-nums text-muted-foreground">
                              {account.code}
                            </span>
                            <span>{account.name}</span>
                          </span>
                        ) : (
                          <span className="ltr-isolate tabular-nums">#{fa(line.account_id)}</span>
                        )}
                      </TableCell>
                      <TableCell>
                        {line.debit > 0 ? <MoneyDisplay amountIrr={line.debit} size="sm" /> : "—"}
                      </TableCell>
                      <TableCell>
                        {line.credit > 0 ? <MoneyDisplay amountIrr={line.credit} size="sm" /> : "—"}
                      </TableCell>
                      <TableCell>{line.description || "—"}</TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      <JournalActions
        draft={draft}
        onPosted={handlePosted}
        onReversed={handleReversed}
        onValidated={setIssues}
      />

      {auditEvents && auditEvents.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <History className="size-4" aria-hidden />
              رد حسابرسی
            </CardTitle>
            <CardDescription>رویدادهای ثبت‌شده برای این سند.</CardDescription>
          </CardHeader>
          <CardContent>
            <ul className="space-y-3">
              {auditEvents.map((event) => (
                <li key={event.id} className="flex flex-wrap items-center justify-between gap-2 border-b pb-3 text-sm last:border-0 last:pb-0">
                  <span className="font-medium">{auditActionLabel(event.action)}</span>
                  <span className="flex items-center gap-3">
                    <Badge variant={event.outcome === "success" ? "secondary" : "destructive"}>
                      {event.outcome === "success" ? "موفق" : "ناموفق"}
                    </Badge>
                    <span className="text-muted-foreground">
                      {formatIsoDateTime(event.occurred_at)}
                    </span>
                  </span>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
