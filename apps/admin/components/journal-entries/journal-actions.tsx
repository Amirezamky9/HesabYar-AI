"use client";

/**
 * Journal lifecycle actions (US-303): validate -> post -> reverse.
 *
 * Validate never mutates; post and reverse ask for confirmation first
 * (AlertDialog) and send an Idempotency-Key via the typed client. Every
 * outcome surfaces as a Persian toast; errors are mapped from the stable
 * API code, never raw JSON. A posted journal has no edit/delete path —
 * this component only offers reverse for posted journals (§19 rule 8).
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import { Check, Loader2, Undo2 } from "lucide-react";
import {
  api,
  ApiError,
  type JournalDraftResponse,
  type PostedJournalResponse,
  type ValidationIssueResponse,
} from "@/lib/api-client";
import { fa } from "@/lib/utils";
import {
  apiErrorMessage,
  validationIssueMessage,
} from "@/features/journal-entries/lib/error-messages";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  Button,
  Label,
  Textarea,
  toast,
} from "@/components/ui";

export interface JournalActionsProps {
  draft: JournalDraftResponse;
  /** Called with the posted journal after a successful post. */
  onPosted: (posted: PostedJournalResponse) => void;
  /** Called with the reversal after a successful reverse. */
  onReversed: (reversal: PostedJournalResponse) => void;
  /** Called with the validation issues (empty when valid). */
  onValidated: (issues: ValidationIssueResponse[]) => void;
}

export function JournalActions({ draft, onPosted, onReversed, onValidated }: JournalActionsProps) {
  const router = useRouter();
  const [validating, setValidating] = React.useState(false);
  const [posting, setPosting] = React.useState(false);
  const [reversing, setReversing] = React.useState(false);
  const [postDialogOpen, setPostDialogOpen] = React.useState(false);
  const [reverseDialogOpen, setReverseDialogOpen] = React.useState(false);
  const [reverseDescription, setReverseDescription] = React.useState("");

  const isPosted = draft.state === "posted";
  const busy = validating || posting || reversing;

  const handleUnauthorized = (err: unknown): boolean => {
    if (err instanceof ApiError && err.isUnauthorized) {
      router.replace("/login");
      return true;
    }
    return false;
  };

  const handleValidate = async () => {
    setValidating(true);
    try {
      const result = await api.validateJournalDraft(draft.id);
      const issues = result.issues ?? [];
      onValidated(issues);
      if (result.valid) {
        toast.success("سند معتبر است و آمادهٔ ثبت است.");
      } else {
        toast.error("سند ایراد دارد.", {
          description: issues.length > 0 ? validationIssueMessage(issues[0]) : undefined,
        });
      }
    } catch (err) {
      if (!handleUnauthorized(err)) toast.error(apiErrorMessage(err));
    } finally {
      setValidating(false);
    }
  };

  const handlePost = async () => {
    setPosting(true);
    try {
      const posted = await api.postJournalDraft(draft.id);
      setPostDialogOpen(false);
      toast.success("سند ثبت شد.", {
        description: `شمارهٔ سند: ${fa(posted.number)}`,
      });
      onPosted(posted);
    } catch (err) {
      if (!handleUnauthorized(err)) toast.error(apiErrorMessage(err));
    } finally {
      setPosting(false);
    }
  };

  const handleReverse = async () => {
    setReversing(true);
    try {
      const reversal = await api.reverseJournal(draft.id, {
        description: reverseDescription.trim(),
      });
      setReverseDialogOpen(false);
      toast.success("سند برگشت خورد.", {
        description: `سند برگشت با شمارهٔ ${fa(reversal.number)} ثبت شد.`,
      });
      onReversed(reversal);
    } catch (err) {
      if (!handleUnauthorized(err)) toast.error(apiErrorMessage(err));
    } finally {
      setReversing(false);
    }
  };

  return (
    <div className="flex flex-wrap items-center gap-3">
      {!isPosted ? (
        <>
          <Button type="button" variant="outline" onClick={handleValidate} disabled={busy}>
            {validating ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <Check className="size-4" aria-hidden />}
            بررسی اعتبار
          </Button>
          <AlertDialog open={postDialogOpen} onOpenChange={setPostDialogOpen}>
            <Button type="button" onClick={() => setPostDialogOpen(true)} disabled={busy}>
              {posting ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null}
              ثبت سند
            </Button>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>سند ثبت شود؟</AlertDialogTitle>
                <AlertDialogDescription>
                  پس از ثبت، سند در دفتر کل ثبت می‌شود و دیگر قابل ویرایش یا حذف نیست. برای
                  اصلاح آن باید سند برگشت صادر کنید.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel disabled={posting}>انصراف</AlertDialogCancel>
                <AlertDialogAction onClick={handlePost} disabled={posting}>
                  {posting ? "در حال ثبت…" : "ثبت سند"}
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </>
      ) : (
        <AlertDialog open={reverseDialogOpen} onOpenChange={setReverseDialogOpen}>
          <Button
            type="button"
            variant="outline"
            onClick={() => setReverseDialogOpen(true)}
            disabled={busy}
          >
            <Undo2 className="size-4" aria-hidden />
            برگشت سند
          </Button>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>سند برگشت بخورد؟</AlertDialogTitle>
              <AlertDialogDescription>
                یک سند برگشت با ردیف‌های معکوس ثبت می‌شود و اثر این سند را خنثی می‌کند. خود
                سند حذف نمی‌شود.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <div className="space-y-2">
              <Label htmlFor="reverse-description">شرح برگشت (اختیاری)</Label>
              <Textarea
                id="reverse-description"
                value={reverseDescription}
                onChange={(e) => setReverseDescription(e.target.value)}
                placeholder="مثلاً: اصلاح اشتباه ثبت"
              />
            </div>
            <AlertDialogFooter>
              <AlertDialogCancel disabled={reversing}>انصراف</AlertDialogCancel>
              <AlertDialogAction onClick={handleReverse} disabled={reversing}>
                {reversing ? "در حال برگشت…" : "برگشت سند"}
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      )}
    </div>
  );
}
