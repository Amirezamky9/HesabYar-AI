"use client";

/**
 * Journal draft editor (US-303) — create and edit.
 *
 * Create: POST /accounting/journal-drafts. Edit: PATCH the same draft
 * (lines replace wholesale). Both go through the BFF with the session
 * key; the typed client attaches a fresh Idempotency-Key per call.
 * Dates are Gregorian ISO in state, Jalali in the picker; money is
 * integer IRR. An unbalanced grid blocks the save button.
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import {
  api,
  ApiError,
  type AccountResponse,
  type CompanyResponse,
  type JournalDraftResponse,
} from "@/lib/api-client";
import { todayIso } from "@/lib/jalali";
import { en } from "@/lib/utils";
import { apiErrorMessage } from "@/features/journal-entries/lib/error-messages";
import {
  emptyLines,
  fromDraftResponse,
  toDraftLineRequests,
  validateLines,
  type JournalLineDraft,
} from "@/features/journal-entries/lib/balance";
import { fetchAllAccounts, fetchCompanies } from "@/features/journal-entries/lib/journal-api";
import {
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Input,
  JalaliDatePicker,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Skeleton,
  toast,
} from "@/components/ui";
import { JournalLineGrid } from "./journal-line-grid";

export interface JournalEditorProps {
  mode: "create" | "edit";
  /** The draft being edited (edit mode only). */
  draft?: JournalDraftResponse;
  onSaved: (draft: JournalDraftResponse) => void;
  onCancel: () => void;
}

export function JournalEditor({ mode, draft, onSaved, onCancel }: JournalEditorProps) {
  const router = useRouter();

  const [companies, setCompanies] = React.useState<CompanyResponse[] | null>(null);
  const [companyId, setCompanyId] = React.useState<string | null>(
    mode === "edit" && draft ? draft.company_id : null,
  );
  const [accounts, setAccounts] = React.useState<AccountResponse[]>([]);
  // The company whose chart is currently loaded; the picker is loading
  // until it matches the selected company (derived, not set in an effect).
  const [loadedCompanyId, setLoadedCompanyId] = React.useState<string | null>(null);
  const accountsLoading = loadedCompanyId !== companyId;
  const [loadError, setLoadError] = React.useState<string | null>(null);

  const [date, setDate] = React.useState<string | null>(
    mode === "edit" && draft ? draft.date : todayIso(),
  );
  const [description, setDescription] = React.useState(
    mode === "edit" && draft ? draft.description : "",
  );
  // Engine journal id (create mode only). The API auto-resolves the
  // journal when the engine holds exactly one general journal; with
  // several, the operator must name one. There is no journals list
  // endpoint yet (spec §9.2 lists GET /accounting/journals, unbuilt),
  // so until it lands this is a plain id field, not a picker.
  const [journalId, setJournalId] = React.useState("");
  const [lines, setLines] = React.useState<JournalLineDraft[]>(() =>
    mode === "edit" && draft ? fromDraftResponse(draft) : emptyLines(),
  );
  const [saveAttempted, setSaveAttempted] = React.useState(false);
  const [saving, setSaving] = React.useState(false);

  const validation = React.useMemo(() => validateLines(lines), [lines]);
  // Empty = auto-resolve; otherwise a positive integer (Persian digits ok).
  const parsedJournalId = React.useMemo(() => {
    const ascii = en(journalId).trim();
    if (ascii === "") return null;
    return /^\d+$/.test(ascii) && Number(ascii) > 0 ? Number(ascii) : -1;
  }, [journalId]);
  const journalIdValid = parsedJournalId !== -1;
  const canSave =
    validation.valid && date !== null && companyId !== null && journalIdValid && !saving;

  // Companies: one company is the norm (the key is usually restricted);
  // several companies get a select. Edit mode keeps the draft's company.
  React.useEffect(() => {
    let cancelled = false;
    fetchCompanies()
      .then((items) => {
        if (cancelled) return;
        setCompanies(items);
        if (mode === "create" && items.length > 0) {
          setCompanyId((current) => current ?? items[0].id);
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.replace("/login");
          return;
        }
        setLoadError(apiErrorMessage(err));
      });
    return () => {
      cancelled = true;
    };
  }, [mode, router]);

  React.useEffect(() => {
    if (!companyId) return;
    let cancelled = false;
    fetchAllAccounts(companyId)
      .then((items) => {
        if (cancelled) return;
        setAccounts(items);
        setLoadedCompanyId(companyId);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.replace("/login");
          return;
        }
        setLoadError(apiErrorMessage(err));
      });
    return () => {
      cancelled = true;
    };
  }, [companyId, router]);

  const handleSave = async () => {
    setSaveAttempted(true);
    if (!canSave || !companyId || !date) return;
    setSaving(true);
    try {
      const payloadLines = toDraftLineRequests(lines);
      const saved =
        mode === "create"
          ? await api.createJournalDraft({
              company_id: companyId,
              date,
              description: description.trim(),
              journal_id: parsedJournalId ?? undefined,
              lines: payloadLines,
            })
          : await api.updateJournalDraft(draft!.id, {
              date,
              description: description.trim(),
              lines: payloadLines,
            });
      toast.success(mode === "create" ? "پیش‌نویس سند ساخته شد." : "پیش‌نویس سند به‌روز شد.");
      onSaved(saved);
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      toast.error(apiErrorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  if (loadError) {
    return (
      <p role="alert" className="text-sm text-destructive">
        {loadError}
      </p>
    );
  }

  if (companies === null) {
    return (
      <div className="space-y-4" aria-label="در حال بارگذاری">
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }

  if (companies.length === 0) {
    return (
      <p role="alert" className="text-sm text-muted-foreground">
        هیچ شرکتی در دسترس نیست؛ بدون شرکت نمی‌توان سند ساخت.
      </p>
    );
  }

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>{mode === "create" ? "سند جدید" : "ویرایش پیش‌نویس"}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {companies.length > 1 && mode === "create" ? (
            <div className="space-y-2">
              <Label htmlFor="journal-company">شرکت</Label>
              <Select value={companyId ?? undefined} onValueChange={setCompanyId}>
                <SelectTrigger id="journal-company">
                  <SelectValue placeholder="انتخاب شرکت" />
                </SelectTrigger>
                <SelectContent>
                  {companies.map((company) => (
                    <SelectItem key={company.id} value={company.id}>
                      {company.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          ) : null}
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="journal-date">تاریخ سند</Label>
              <JalaliDatePicker
                id="journal-date"
                value={date}
                onChange={setDate}
                placeholder="انتخاب تاریخ سند"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="journal-description">شرح سند</Label>
              <Input
                id="journal-description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="مثلاً: خرید ملزومات از فروشگاه"
              />
            </div>
          </div>
          {mode === "create" ? (
            <div className="space-y-2">
              <Label htmlFor="journal-id">شناسهٔ دفتر روزنامه (اختیاری)</Label>
              <Input
                id="journal-id"
                dir="ltr"
                inputMode="numeric"
                value={journalId}
                onChange={(e) => setJournalId(e.target.value)}
                placeholder="مثلاً: ۲"
                aria-invalid={!journalIdValid || undefined}
                aria-describedby="journal-id-help"
                className="max-w-48 text-start"
              />
              <p id="journal-id-help" className="text-sm text-muted-foreground">
                اگر موتور حسابداری بیش از یک دفتر روزنامهٔ عمومی دارد، شناسهٔ دفتر موردنظر
                را وارد کنید؛ در غیر این صورت خالی بگذارید تا به‌صورت خودکار انتخاب شود.
              </p>
              {!journalIdValid ? (
                <p role="alert" className="text-sm text-destructive">
                  شناسهٔ دفتر روزنامه باید یک عدد صحیح مثبت باشد.
                </p>
              ) : null}
            </div>
          ) : null}
        </CardContent>
      </Card>

      <JournalLineGrid
        lines={lines}
        onChange={setLines}
        accounts={accounts}
        accountsLoading={accountsLoading}
        disabled={saving}
      />

      {saveAttempted && !validation.valid ? (
        <div role="alert" className="rounded-lg border border-destructive/40 bg-destructive/5 p-4">
          <p className="mb-2 text-sm font-medium text-destructive">سند هنوز قابل ذخیره نیست:</p>
          <ul className="list-disc space-y-1 ps-5 text-sm text-destructive">
            {validation.errors.map((error) => (
              <li key={error}>{error}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-3">
        <Button type="button" onClick={handleSave} disabled={!canSave}>
          {saving ? "در حال ذخیره…" : mode === "create" ? "ذخیرهٔ پیش‌نویس" : "ذخیرهٔ تغییرات"}
        </Button>
        <Button type="button" variant="outline" onClick={onCancel} disabled={saving}>
          انصراف
        </Button>
        {!validation.valid ? (
          <p className="text-sm text-muted-foreground">
            برای فعال شدن ذخیره، سند باید تراز باشد و هر ردیف یک حساب داشته باشد.
          </p>
        ) : null}
      </div>
    </div>
  );
}
