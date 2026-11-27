/**
 * Moadian tab (US-405).
 *
 * Live profiles / documents / submissions from the US-404 APIs, all
 * through the BFF. The full simulator lifecycle is drivable from here:
 * create a test profile → create a draft document (Table 7 payload
 * built from the form) → create a draft submission → recorded human
 * approval → queue (outbox) → reconcile pass (simulator submit +
 * inquiry) → accepted/rejected with status evidence (hashes,
 * reference number, status events).
 *
 * Simulator only: production queueing is refused by the backend with
 * 501 MOADIAN_PRODUCTION_NOT_CONNECTED and the UI shows that refusal
 * in Persian. No key material is ever entered or shown — only the
 * key_reference (where the key lives).
 */

"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { ApiError, api, type MoadianDocumentResponse, type MoadianProfileResponse, type MoadianStatusEventResponse, type MoadianSubmissionResponse } from "@/lib/api-client";
import { fa } from "@/lib/utils";
import { formatIsoDateTime } from "@/lib/format-date";
import {
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  DataTable,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Input,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  StatusBadge,
  Textarea,
  type DataTableColumn,
} from "@/components/ui";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import { isPermissionDenied, persianApiErrorMessage } from "@/features/shared/api-error";
import {
  buildMoadianPayload,
  newFixtureTaxid,
  validateMoadianForm,
  type MoadianDocumentForm,
} from "./compliance-data";
import {
  documentStatusLabel,
  documentStatusTone,
  MOADIAN_ENVIRONMENT_LABELS,
  officialStatusLabel,
  officialStatusTone,
  SENDER_MODE_LABELS,
  submissionStatusLabel,
  submissionStatusTone,
  verificationLabel,
  verificationTone,
} from "./labels";

const PROFILE_COLUMNS: DataTableColumn<MoadianProfileResponse>[] = [
  { key: "name", header: "نام", render: (row) => row.name },
  { key: "slug", header: "شناسه", render: (row) => <span dir="ltr">{row.slug}</span> },
  {
    key: "env",
    header: "محیط",
    render: (row) => MOADIAN_ENVIRONMENT_LABELS[row.environment] ?? row.environment,
  },
  {
    key: "sender",
    header: "ارسال‌کننده",
    render: (row) => SENDER_MODE_LABELS[row.sender_mode] ?? row.sender_mode,
  },
  {
    key: "verification",
    header: "وضعیت تأیید",
    render: (row) => (
      <StatusBadge
        status={row.verification_status}
        label={verificationLabel(row.verification_status)}
        tone={verificationTone(row.verification_status)}
      />
    ),
  },
  { key: "active", header: "فعال", render: (row) => (row.is_active ? "بله" : "خیر") },
];

const DOCUMENT_COLUMNS: DataTableColumn<MoadianDocumentResponse>[] = [
  { key: "taxid", header: "شمارهٔ مالیاتی یکتا", render: (row) => <span dir="ltr">{row.taxid ?? "—"}</span> },
  { key: "serial", header: "سریال", render: (row) => <span dir="ltr">{row.serial ?? "—"}</span> },
  {
    key: "status",
    header: "وضعیت",
    render: (row) => (
      <StatusBadge status={row.status} label={documentStatusLabel(row.status)} tone={documentStatusTone(row.status)} />
    ),
  },
  { key: "created", header: "ایجاد", render: (row) => formatIsoDateTime(row.created_at) },
];

const SUBMISSION_COLUMNS: DataTableColumn<MoadianSubmissionResponse>[] = [
  {
    key: "uid",
    header: "شناسهٔ ارسال",
    render: (row) => <span dir="ltr" className="text-xs tabular-nums">{row.uid.slice(0, 13)}…</span>,
  },
  {
    key: "status",
    header: "وضعیت",
    render: (row) => (
      <StatusBadge status={row.status} label={submissionStatusLabel(row.status)} tone={submissionStatusTone(row.status)} />
    ),
  },
  {
    key: "scenario",
    header: "سناریوی شبیه‌ساز",
    render: (row) => (row.scenario === "accept" ? "پذیرش" : "رد"),
  },
  {
    key: "reference",
    header: "شمارهٔ رسید",
    render: (row) => <span dir="ltr" className="text-xs tabular-nums">{row.reference_number ?? "—"}</span>,
  },
  { key: "updated", header: "آخرین تغییر", render: (row) => formatIsoDateTime(row.updated_at) },
];

const EVENT_COLUMNS: DataTableColumn<MoadianStatusEventResponse>[] = [
  {
    key: "status",
    header: "وضعیت رسمی",
    render: (row) => (
      <StatusBadge status={row.status} label={officialStatusLabel(row.status)} tone={officialStatusTone(row.status)} />
    ),
  },
  { key: "source", header: "منبع", render: (row) => (row.source === "simulator" ? "شبیه‌ساز" : row.source) },
  { key: "occurred", header: "زمان مشاهده", render: (row) => formatIsoDateTime(row.occurred_at) },
];

export function MoadianPage() {
  const router = useRouter();
  const [profiles, setProfiles] = React.useState<MoadianProfileResponse[] | null>(null);
  const [documents, setDocuments] = React.useState<MoadianDocumentResponse[] | null>(null);
  const [submissions, setSubmissions] = React.useState<MoadianSubmissionResponse[] | null>(null);
  const [error, setError] = React.useState<unknown>(null);
  const [reloadKey, setReloadKey] = React.useState(0);

  const [notice, setNotice] = React.useState<string | null>(null);
  const [actionError, setActionError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  // -- new profile form ---------------------------------------------------
  const [profileName, setProfileName] = React.useState("");
  const [profileSlug, setProfileSlug] = React.useState("");
  const [profileEnv, setProfileEnv] = React.useState("test");
  const [profileFiscalId, setProfileFiscalId] = React.useState("");
  const [profileEconomicCode, setProfileEconomicCode] = React.useState("");
  const [profileKeyRef, setProfileKeyRef] = React.useState("");
  const [profileNotes, setProfileNotes] = React.useState("");

  // -- new document form ----------------------------------------------------
  const [docProfileId, setDocProfileId] = React.useState("");
  const [docForm, setDocForm] = React.useState<MoadianDocumentForm>(() => ({
    taxid: newFixtureTaxid(),
    serial: "0002F2B4E7",
    sellerEconomicCode: "32652362589632",
    totalBeforeDiscount: 1000000,
    vatAmount: 90000,
    totalBill: 1090000,
    lineDescription: "پاستیل میوه‌ای",
    lineAmount: 1,
    lineFee: 1000000,
    vatRate: 0.09,
  }));

  // -- new submission form ----------------------------------------------------
  const [subDocumentId, setSubDocumentId] = React.useState("");
  const [subScenario, setSubScenario] = React.useState("accept");

  // -- approval dialog --------------------------------------------------------
  const [approving, setApproving] = React.useState<MoadianSubmissionResponse | null>(null);
  const [approvalNote, setApprovalNote] = React.useState("");

  // -- selected submission detail ----------------------------------------------
  const [selected, setSelected] = React.useState<MoadianSubmissionResponse | null>(null);
  const [events, setEvents] = React.useState<MoadianStatusEventResponse[] | null>(null);

  const load = React.useCallback(async () => {
    const [p, d, s] = await Promise.all([
      api.listMoadianProfiles({ limit: 100 }),
      api.listMoadianDocuments({ limit: 100 }),
      api.listMoadianSubmissions({ limit: 100 }),
    ]);
    setProfiles(p.items);
    setDocuments(d.items);
    setSubmissions(s.items);
  }, []);

  React.useEffect(() => {
    let cancelled = false;
    Promise.all([
      api.listMoadianProfiles({ limit: 100 }),
      api.listMoadianDocuments({ limit: 100 }),
      api.listMoadianSubmissions({ limit: 100 }),
    ])
      .then(([p, d, s]) => {
        if (cancelled) return;
        setProfiles(p.items);
        setDocuments(d.items);
        setSubmissions(s.items);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.replace("/login");
          return;
        }
        setError(err);
      });
    return () => {
      cancelled = true;
    };
  }, [reloadKey, router]);

  // Default document profile: the newest ACTIVE profile, until the
  // operator picks another. Old profiles may be deactivated, and a
  // document against an inactive profile is refused (409
  // MOADIAN_INVALID_STATE) — so profiles[0] (oldest-first) must
  // never be the default.
  const newestActiveProfileId =
    [...(profiles ?? [])]
      .filter((p) => p.is_active)
      .sort((a, b) => b.created_at.localeCompare(a.created_at))[0]?.profile_id ?? "";
  const effectiveDocProfileId = docProfileId || newestActiveProfileId;

  React.useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    api
      .listMoadianStatusEvents(selected.submission_id, { limit: 100 })
      .then((page) => {
        if (!cancelled) setEvents(page.items);
      })
      .catch(() => {
        if (!cancelled) setEvents([]);
      });
    return () => {
      cancelled = true;
    };
  }, [selected]);

  if (error && isPermissionDenied(error)) {
    return <PermissionDeniedState message={persianApiErrorMessage(error, "دریافت مودیان ناموفق بود.")} />;
  }

  if (error) {
    return (
      <ErrorState
        message={persianApiErrorMessage(error, "دریافت مودیان ناموفق بود.")}
        onRetry={() => setReloadKey((k) => k + 1)}
      />
    );
  }

  const guard = (err: unknown): boolean => {
    if (err instanceof ApiError && err.isUnauthorized) {
      router.replace("/login");
      return true;
    }
    setActionError(persianApiErrorMessage(err, "انجام عملیات ناموفق بود."));
    return false;
  };

  const createProfile = async () => {
    if (!profileName.trim() || !/^[a-z0-9][a-z0-9-]{0,127}$/.test(profileSlug)) {
      setActionError("نام پروفایل را وارد کنید و شناسهٔ یکتا باید با حروف کوچک انگلیسی، عدد یا خط تیره باشد.");
      return;
    }
    setBusy(true);
    setActionError(null);
    setNotice(null);
    try {
      const created = await api.createMoadianProfile({
        name: profileName.trim(),
        slug: profileSlug,
        environment: profileEnv,
        sender_mode: "self-tsp",
        fiscal_id: profileFiscalId || null,
        economic_code: profileEconomicCode || null,
        key_reference: profileKeyRef || null,
        notes: profileNotes || null,
      });
      setNotice(`پروفایل «${profileName.trim()}» ساخته شد.`);
      // The profile just created becomes the document form's profile,
      // so the next document does not silently target an older one.
      setDocProfileId(created.profile_id);
      setProfileName("");
      setProfileSlug("");
      setProfileFiscalId("");
      setProfileEconomicCode("");
      setProfileKeyRef("");
      setProfileNotes("");
      await load();
    } catch (err: unknown) {
      guard(err);
    } finally {
      setBusy(false);
    }
  };

  const createDocument = async () => {
    const problem = validateMoadianForm(docForm);
    if (problem) {
      setActionError(problem);
      return;
    }
    if (!effectiveDocProfileId) {
      setActionError("ابتدا یک پروفایل انتخاب کنید.");
      return;
    }
    setBusy(true);
    setActionError(null);
    setNotice(null);
    try {
      await api.createMoadianDocument({
        profile_id: effectiveDocProfileId,
        payload: buildMoadianPayload(docForm),
      });
      setNotice(`سند با شمارهٔ مالیاتی ${docForm.taxid} به‌صورت پیش‌نویس ساخته شد.`);
      setDocForm((prev) => ({ ...prev, taxid: newFixtureTaxid() }));
      await load();
    } catch (err: unknown) {
      guard(err);
    } finally {
      setBusy(false);
    }
  };

  const createSubmission = async () => {
    if (!subDocumentId) {
      setActionError("ابتدا یک سند پیش‌نویس انتخاب کنید.");
      return;
    }
    setBusy(true);
    setActionError(null);
    setNotice(null);
    try {
      const created = await api.createMoadianSubmission({ document_id: subDocumentId, scenario: subScenario });
      setNotice(`ارسال پیش‌نویس ساخته شد (سناریو: ${subScenario === "accept" ? "پذیرش" : "رد"}).`);
      setSelected(created);
      await load();
    } catch (err: unknown) {
      guard(err);
    } finally {
      setBusy(false);
    }
  };

  const approveSelected = async () => {
    if (!approving) return;
    setBusy(true);
    setActionError(null);
    try {
      const updated = await api.approveMoadianSubmission(approving.submission_id, {
        note: approvalNote.trim() || null,
      });
      setNotice("ارسال تأیید شد؛ برای ارسال به صف، دکمهٔ «ارسال به صف» را بزنید.");
      setApproving(null);
      setApprovalNote("");
      setSelected(updated);
      await load();
    } catch (err: unknown) {
      guard(err);
    } finally {
      setBusy(false);
    }
  };

  const queueSubmission = async (submission: MoadianSubmissionResponse) => {
    setBusy(true);
    setActionError(null);
    setNotice(null);
    try {
      const updated = await api.queueMoadianSubmission(submission.submission_id);
      setNotice("ارسال به صف رفت؛ برای پیشبرد شبیه‌ساز، «اجرای تطبیق» را بزنید.");
      setSelected(updated);
      await load();
    } catch (err: unknown) {
      guard(err);
    } finally {
      setBusy(false);
    }
  };

  const runReconcile = async () => {
    setBusy(true);
    setActionError(null);
    setNotice(null);
    try {
      const report = await api.runMoadianReconcile();
      setNotice(
        `تطبیق اجرا شد: ${fa(report.dispatched)} ارسال‌شده، ${fa(report.polled)} استعلام، ${fa(report.accepted)} پذیرفته‌شده، ${fa(report.rejected)} ردشده، ${fa(report.still_pending)} همچنان در انتظار.`,
      );
      if (selected) {
        const refreshed = await api.getMoadianSubmission(selected.submission_id);
        setSelected(refreshed);
      }
      await load();
    } catch (err: unknown) {
      guard(err);
    } finally {
      setBusy(false);
    }
  };

  const draftDocuments = (documents ?? []).filter((d) => d.status === "draft");

  return (
    <div className="space-y-6" data-testid="moadian-page">
      <div>
        <h1 className="text-2xl font-bold">مودیان</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          پروفایل‌های آزمایشی، اسناد و ارسال‌های سامانهٔ مودیان — فقط شبیه‌ساز؛ اتصال تولیدی در این فاز وجود
          ندارد.
        </p>
      </div>

      <div className="rounded-lg border border-warning/40 bg-warning/10 px-4 py-3" role="note">
        <p className="text-sm text-warning-foreground">
          شبیه‌ساز آزمایشی: ارسال به سامانهٔ واقعی مودیان انجام نمی‌شود. ارسال تولیدی مسدود است و بک‌اند آن را
          رد می‌کند. تأیید هر ارسال یک مرحلهٔ ثبت‌شدهٔ انسانی است؛ بدون تأیید، ارسال به صف نمی‌رود.
        </p>
      </div>

      {notice ? (
        <p className="rounded-lg border border-success/40 bg-success/10 px-4 py-3 text-sm text-success" role="status">
          {notice}
        </p>
      ) : null}
      {actionError ? (
        <p className="rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive" role="alert">
          {actionError}
        </p>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>پروفایل‌ها ({fa(profiles?.length ?? 0)})</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <DataTable
            columns={PROFILE_COLUMNS}
            rows={profiles ?? []}
            rowKey={(row) => row.profile_id}
            loading={profiles === null}
            emptyText="هنوز پروفایلی ساخته نشده است؛ از فرم زیر یک پروفایل آزمایشی بسازید."
            caption="کلید امضا هرگز ذخیره نمی‌شود؛ فقط مرجع محل نگهداری کلید ثبت می‌شود."
          />
          <div className="grid gap-4 border-t pt-4 md:grid-cols-3">
            <div className="space-y-2">
              <Label htmlFor="profile-name">نام پروفایل</Label>
              <Input id="profile-name" value={profileName} onChange={(e) => setProfileName(e.target.value)} placeholder="پروفایل آزمایشی" />
            </div>
            <div className="space-y-2">
              <Label htmlFor="profile-slug">شناسهٔ یکتا (انگلیسی)</Label>
              <Input id="profile-slug" dir="ltr" value={profileSlug} onChange={(e) => setProfileSlug(e.target.value)} placeholder="test-profile" />
            </div>
            <div className="space-y-2">
              <Label htmlFor="profile-env">محیط</Label>
              <Select value={profileEnv} onValueChange={(v) => v && setProfileEnv(v)}>
                <SelectTrigger id="profile-env">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="test">آزمایشی (شبیه‌ساز)</SelectItem>
                  <SelectItem value="production">تولیدی</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="profile-fiscal">شناسهٔ حافظهٔ مالیاتی (۶ کاراکتر)</Label>
              <Input id="profile-fiscal" dir="ltr" value={profileFiscalId} onChange={(e) => setProfileFiscalId(e.target.value)} placeholder="ABC123" />
            </div>
            <div className="space-y-2">
              <Label htmlFor="profile-economic">کد اقتصادی (۱۰ یا ۱۴ رقم)</Label>
              <Input id="profile-economic" dir="ltr" value={profileEconomicCode} onChange={(e) => setProfileEconomicCode(e.target.value)} placeholder="32652362589632" />
            </div>
            <div className="space-y-2">
              <Label htmlFor="profile-keyref">مرجع کلید (نام متغیر محیطی)</Label>
              <Input id="profile-keyref" dir="ltr" value={profileKeyRef} onChange={(e) => setProfileKeyRef(e.target.value)} placeholder="MOADIAN_TEST_KEY" />
            </div>
          </div>
          <div className="space-y-2">
            <Label htmlFor="profile-notes">یادداشت</Label>
            <Textarea id="profile-notes" value={profileNotes} onChange={(e) => setProfileNotes(e.target.value)} placeholder="یادداشت اختیاری" />
          </div>
          <Button onClick={createProfile} disabled={busy} data-testid="create-moadian-profile">
            ساخت پروفایل
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>اسناد ({fa(documents?.length ?? 0)})</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <DataTable
            columns={DOCUMENT_COLUMNS}
            rows={documents ?? []}
            rowKey={(row) => row.document_id}
            loading={documents === null}
            emptyText="هنوز سندی ساخته نشده است."
            caption="سند فقط پیش‌نویس ساخته می‌شود؛ تأیید لازم ندارد."
          />
          <div className="grid gap-4 border-t pt-4 md:grid-cols-3">
            <div className="space-y-2">
              <Label htmlFor="doc-profile">پروفایل</Label>
              <Select value={effectiveDocProfileId} onValueChange={(v) => v && setDocProfileId(v)}>
                <SelectTrigger id="doc-profile">
                  <SelectValue placeholder="انتخاب پروفایل" />
                </SelectTrigger>
                <SelectContent>
                  {(profiles ?? []).map((p) => (
                    <SelectItem key={p.profile_id} value={p.profile_id}>
                      {p.name} ({p.slug})
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="doc-taxid">شمارهٔ مالیاتی یکتا (۲۲ کاراکتر)</Label>
              <div className="flex gap-2">
                <Input
                  id="doc-taxid"
                  dir="ltr"
                  value={docForm.taxid}
                  onChange={(e) => setDocForm((prev) => ({ ...prev, taxid: e.target.value }))}
                />
                <Button
                  variant="outline"
                  onClick={() => setDocForm((prev) => ({ ...prev, taxid: newFixtureTaxid() }))}
                  type="button"
                >
                  شناسهٔ تازه
                </Button>
              </div>
            </div>
            <div className="space-y-2">
              <Label htmlFor="doc-serial">سریال صورت‌حساب (۱۰ کاراکتر)</Label>
              <Input
                id="doc-serial"
                dir="ltr"
                value={docForm.serial}
                onChange={(e) => setDocForm((prev) => ({ ...prev, serial: e.target.value }))}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="doc-tins">کد اقتصادی فروشنده</Label>
              <Input
                id="doc-tins"
                dir="ltr"
                value={docForm.sellerEconomicCode}
                onChange={(e) => setDocForm((prev) => ({ ...prev, sellerEconomicCode: e.target.value }))}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="doc-line-desc">شرح قلم</Label>
              <Input
                id="doc-line-desc"
                value={docForm.lineDescription}
                onChange={(e) => setDocForm((prev) => ({ ...prev, lineDescription: e.target.value }))}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="doc-line-fee">مبلغ واحد قلم (ریال)</Label>
              <Input
                id="doc-line-fee"
                dir="ltr"
                inputMode="numeric"
                value={String(docForm.lineFee)}
                onChange={(e) => setDocForm((prev) => ({ ...prev, lineFee: Number(e.target.value) || 0 }))}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="doc-total">مبلغ پیش از تخفیف (ریال)</Label>
              <Input
                id="doc-total"
                dir="ltr"
                inputMode="numeric"
                value={String(docForm.totalBeforeDiscount)}
                onChange={(e) => setDocForm((prev) => ({ ...prev, totalBeforeDiscount: Number(e.target.value) || 0 }))}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="doc-vat">مبلغ مالیات (ریال)</Label>
              <Input
                id="doc-vat"
                dir="ltr"
                inputMode="numeric"
                value={String(docForm.vatAmount)}
                onChange={(e) => setDocForm((prev) => ({ ...prev, vatAmount: Number(e.target.value) || 0 }))}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="doc-bill">مبلغ صورت‌حساب (ریال)</Label>
              <Input
                id="doc-bill"
                dir="ltr"
                inputMode="numeric"
                value={String(docForm.totalBill)}
                onChange={(e) => setDocForm((prev) => ({ ...prev, totalBill: Number(e.target.value) || 0 }))}
              />
            </div>
          </div>
          <p className="text-sm text-muted-foreground">
            فیلدهای ساختاری (نوع، الگو و موضوع صورت‌حساب) طبق نمونهٔ دستورالعمل رسمی مودیان تکمیل می‌شوند؛
            مقادیر مالی از همین فرم می‌آیند.
          </p>
          <Button onClick={createDocument} disabled={busy || !effectiveDocProfileId} data-testid="create-moadian-document">
            ساخت سند پیش‌نویس
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-wrap items-center justify-between gap-4">
          <CardTitle>ارسال‌ها ({fa(submissions?.length ?? 0)})</CardTitle>
          <Button variant="outline" onClick={runReconcile} disabled={busy} data-testid="run-moadian-reconcile">
            اجرای تطبیق (شبیه‌ساز)
          </Button>
        </CardHeader>
        <CardContent className="space-y-4">
          <DataTable
            columns={SUBMISSION_COLUMNS}
            rows={submissions ?? []}
            rowKey={(row) => row.submission_id}
            loading={submissions === null}
            emptyText="هنوز ارسالی ثبت نشده است؛ از فرم زیر یک ارسال پیش‌نویس بسازید."
            onRowClick={(row) => {
              setEvents(null);
              setSelected(row);
            }}
            caption="برای دیدن شواهد وضعیت (رویدادها، هش‌ها، شمارهٔ رسید) روی ردیف هر ارسال کلیک کنید."
          />
          <div className="grid gap-4 border-t pt-4 md:grid-cols-3">
            <div className="space-y-2">
              <Label htmlFor="sub-document">سند پیش‌نویس</Label>
              <Select value={subDocumentId} onValueChange={(v) => v && setSubDocumentId(v)}>
                <SelectTrigger id="sub-document">
                  <SelectValue placeholder="انتخاب سند" />
                </SelectTrigger>
                <SelectContent>
                  {draftDocuments.map((d) => (
                    <SelectItem key={d.document_id} value={d.document_id}>
                      {d.taxid ?? d.document_id}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="sub-scenario">سناریوی شبیه‌ساز</Label>
              <Select value={subScenario} onValueChange={(v) => v && setSubScenario(v)}>
                <SelectTrigger id="sub-scenario">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="accept">پذیرش (SUCCESS)</SelectItem>
                  <SelectItem value="reject">رد (FAILED)</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="flex items-end">
              <Button onClick={createSubmission} disabled={busy || draftDocuments.length === 0} data-testid="create-moadian-submission">
                ساخت ارسال پیش‌نویس
              </Button>
            </div>
          </div>

          {selected ? (
            <div className="space-y-4 rounded-lg border p-4" data-testid="moadian-submission-detail">
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="font-semibold">جزئیات ارسال</h3>
                <StatusBadge
                  status={selected.status}
                  label={submissionStatusLabel(selected.status)}
                  tone={submissionStatusTone(selected.status)}
                />
              </div>
              <dl className="grid gap-2 text-sm md:grid-cols-2">
                <div>
                  <dt className="text-muted-foreground">شناسهٔ یکتای ارسال</dt>
                  <dd dir="ltr" className="break-all tabular-nums">{selected.uid}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">شناسهٔ همبستگی</dt>
                  <dd dir="ltr" className="break-all tabular-nums">{selected.correlation_id}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">شمارهٔ رسید</dt>
                  <dd dir="ltr" className="break-all tabular-nums">{selected.reference_number ?? "هنوز صادر نشده است"}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">تأییدکننده</dt>
                  <dd>
                    {selected.approved_by ? (
                      <>
                        <span dir="ltr" className="text-xs tabular-nums">{selected.approved_by}</span>
                        {selected.approved_at ? ` · ${formatIsoDateTime(selected.approved_at)}` : ""}
                        {selected.approval_note ? ` · ${selected.approval_note}` : ""}
                      </>
                    ) : (
                      "هنوز تأیید نشده است"
                    )}
                  </dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">هش درخواست (SHA-256)</dt>
                  <dd dir="ltr" className="break-all text-xs tabular-nums">{selected.request_hash ?? "—"}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">هش پاسخ (SHA-256)</dt>
                  <dd dir="ltr" className="break-all text-xs tabular-nums">{selected.response_hash ?? "—"}</dd>
                </div>
              </dl>
              <div className="flex flex-wrap gap-2">
                {selected.status === "draft" ? (
                  <Button onClick={() => setApproving(selected)} disabled={busy} data-testid="approve-moadian-submission">
                    تأیید ارسال
                  </Button>
                ) : null}
                {selected.status === "approved" ? (
                  <Button onClick={() => queueSubmission(selected)} disabled={busy} data-testid="queue-moadian-submission">
                    ارسال به صف
                  </Button>
                ) : null}
                <Button variant="outline" onClick={() => { setEvents(null); setSelected(null); }}>
                  بستن جزئیات
                </Button>
              </div>
              <div>
                <h4 className="mb-2 font-medium">رویدادهای وضعیت</h4>
                <DataTable
                  columns={EVENT_COLUMNS}
                  rows={events ?? []}
                  rowKey={(row) => row.event_id}
                  loading={events === null}
                  emptyText="هنوز رویداد وضعیتی برای این ارسال ثبت نشده است."
                  caption="هر استعلام شبیه‌ساز با شواهد کامل آن ثبت می‌شود."
                />
              </div>
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Dialog open={approving !== null} onOpenChange={(open) => !open && setApproving(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>تأیید ارسال مودیان</DialogTitle>
            <DialogDescription>
              این تأیید به‌نام شما ثبت می‌شود و بدون آن ارسال به صف نمی‌رود. ارسال فقط به شبیه‌ساز آزمایشی
              می‌رسد، نه به سامانهٔ واقعی مودیان.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Label htmlFor="approval-note">یادداشت تأیید (اختیاری)</Label>
            <Textarea
              id="approval-note"
              value={approvalNote}
              onChange={(e) => setApprovalNote(e.target.value)}
              placeholder="مثلاً: بررسی شد؛ ارسال آزمایشی"
            />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setApproving(null)}>
              انصراف
            </Button>
            <Button onClick={approveSelected} disabled={busy} data-testid="confirm-moadian-approve">
              تأیید و ثبت
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
