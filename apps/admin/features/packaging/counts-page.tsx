/**
 * Periodic materials counts (US-515, Phase 5 wave 2).
 *
 * A count opens a `stock.inventory` at the packaging location with
 * expected quantities filled in; the operator enters counted
 * quantities per line and confirms — the engine then posts the
 * lost-and-found adjustment moves. Confirming requires every line
 * counted (the human approval step). A draft count can be cancelled;
 * no adjustment is posted then.
 */

"use client";

import * as React from "react";
import { ClipboardList, Plus } from "lucide-react";
import { api, type InventoryCountResponse } from "@/lib/api-client";
import { formatJalali } from "@/lib/jalali";
import {
  Button,
  Card,
  CardContent,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Input,
  Label,
  Skeleton,
  StatusBadge,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  toast,
} from "@/components/ui";
import { CompanyPicker } from "@/features/shared/company-picker";
import { useCompany } from "@/features/shared/use-company";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import { isPermissionDenied } from "@/features/shared/api-error";
import {
  countStateLabel,
  countStateTone,
  formatQuantity,
  packagingApiErrorMessage,
  parseDecimalInput,
} from "./packaging-data";

const PAGE_SIZE = 20;

function formatCountDate(iso: string): string {
  const date = new Date(`${iso}T00:00:00Z`);
  return formatJalali(date);
}

export function CountsPage() {
  const { companies, companyId, setCompanyId, loading: companiesLoading, error: companiesError } =
    useCompany();
  const [counts, setCounts] = React.useState<InventoryCountResponse[]>([]);
  const [selected, setSelected] = React.useState<InventoryCountResponse | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<unknown>(null);
  const [refreshNonce, setRefreshNonce] = React.useState(0);
  const [createOpen, setCreateOpen] = React.useState(false);

  React.useEffect(() => {
    if (!companyId) return;
    let cancelled = false;
    api
      .listCounts(companyId, { limit: PAGE_SIZE })
      .then((page) => {
        if (cancelled) return;
        setCounts(page.items);
        setError(null);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err);
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [companyId, refreshNonce]);

  const refresh = React.useCallback(() => {
    setLoading(true);
    setRefreshNonce((n) => n + 1);
  }, []);

  if (companiesLoading || loading) {
    return (
      <div className="space-y-4" data-testid="counts-loading">
        <Skeleton className="h-10 w-64" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (companiesError || error) {
    if (isPermissionDenied(companiesError ?? error)) {
      return <PermissionDeniedState message="کلید شما برای بخش بسته‌بندی دسترسی ندارد." />;
    }
    return (
      <ErrorState
        message={packagingApiErrorMessage(companiesError ?? error, "دریافت شمارش‌ها ناموفق بود.")}
        onRetry={refresh}
      />
    );
  }

  if (!companyId) {
    return (
      <div className="rounded-lg border p-6 text-center">
        <p className="font-medium">شرکتی در دسترس نیست</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">شمارش دوره‌ای مواد</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            موجودی موردانتظار در برابر شمارش واقعی؛ مغایرت با حرکت به/از انبار اشیای گمشده اصلاح
            می‌شود.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <CompanyPicker companies={companies} companyId={companyId} onChange={setCompanyId} />
          <Button onClick={() => setCreateOpen(true)} data-testid="create-count">
            <Plus className="size-4" />
            شمارش جدید
          </Button>
        </div>
      </div>

      {counts.length === 0 ? (
        <Card data-testid="counts-empty">
          <CardContent className="flex flex-col items-center gap-2 py-12 text-center">
            <ClipboardList className="size-10 text-muted-foreground" />
            <p className="font-medium">هنوز شمارشی ثبت نشده است</p>
            <p className="text-sm text-muted-foreground">
              شمارش دوره‌ای، موجودی دفتری را با واقعیت انبار مقایسه می‌کند.
            </p>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>تاریخ</TableHead>
                  <TableHead>انبار</TableHead>
                  <TableHead>وضعیت</TableHead>
                  <TableHead>ردیف‌ها</TableHead>
                  <TableHead>شمارش‌کننده</TableHead>
                  <TableHead>اقدام</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {counts.map((count) => (
                  <TableRow key={count.id} data-testid={`count-row-${count.id}`}>
                    <TableCell>{formatCountDate(count.count_date)}</TableCell>
                    <TableCell>{count.location_name}</TableCell>
                    <TableCell>
                      <StatusBadge
                        status={count.state}
                        label={countStateLabel(count.state)}
                        tone={countStateTone(count.state)}
                      />
                    </TableCell>
                    <TableCell>{count.lines.length}</TableCell>
                    <TableCell>{count.counted_by ?? "—"}</TableCell>
                    <TableCell>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setSelected(count)}
                        data-testid={`count-open-${count.id}`}
                      >
                        مشاهده
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}

      {selected && (
        <CountDetailDialog
          count={selected}
          onOpenChange={(open) => {
            if (!open) setSelected(null);
          }}
          onChanged={(updated) => {
            setSelected(updated);
            refresh();
          }}
        />
      )}
      <CreateCountDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        companyId={companyId}
        onCreated={(count) => {
          refresh();
          setSelected(count);
        }}
      />
    </div>
  );
}

function CreateCountDialog({
  open,
  onOpenChange,
  companyId,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  companyId: string;
  onCreated: (count: InventoryCountResponse) => void;
}) {
  const [countedBy, setCountedBy] = React.useState("");
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  async function submit() {
    setSaving(true);
    setError(null);
    try {
      const count = await api.createCount({
        company_id: companyId,
        counted_by: countedBy.trim() || null,
      });
      toast.success("شمارش جدید باز شد.");
      setCountedBy("");
      onOpenChange(false);
      onCreated(count);
    } catch (err: unknown) {
      setError(packagingApiErrorMessage(err, "باز کردن شمارش ناموفق بود."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>شمارش جدید</DialogTitle>
          <DialogDescription>
            شمارش در انبار بسته‌بندی باز می‌شود و مقادیر موردانتظار از ترایتون پر می‌شوند.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="count-by">شمارش‌کننده (اختیاری)</Label>
            <Input
              id="count-by"
              value={countedBy}
              onChange={(event) => setCountedBy(event.target.value)}
              placeholder="مثلاً انباردار"
              data-testid="count-by"
            />
          </div>
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            انصراف
          </Button>
          <Button onClick={submit} disabled={saving} data-testid="count-submit">
            {saving ? "در حال باز کردن…" : "باز کردن شمارش"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function CountDetailDialog({
  count,
  onOpenChange,
  onChanged,
}: {
  count: InventoryCountResponse;
  onOpenChange: (open: boolean) => void;
  onChanged: (count: InventoryCountResponse) => void;
}) {
  const [drafts, setDrafts] = React.useState<Record<string, string>>({});
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const isDraft = count.state === "draft";

  async function saveLine(lineId: number | null, materialId: string) {
    const raw = drafts[materialId];
    const parsed = raw === undefined ? null : parseDecimalInput(raw);
    if (lineId === null || parsed === null) {
      setError("مقدار شمارش‌شده باید عددی بزرگ‌تر یا مساوی صفر باشد.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const updated = await api.setCountLine(count.id, { line_id: lineId, quantity: parsed });
      onChanged(updated);
    } catch (err: unknown) {
      setError(packagingApiErrorMessage(err, "ذخیرهٔ ردیف ناموفق بود."));
    } finally {
      setBusy(false);
    }
  }

  async function confirm() {
    setBusy(true);
    setError(null);
    try {
      const updated = await api.confirmCount(count.id);
      toast.success("شمارش تأیید شد؛ مغایرت‌ها اصلاح شدند.");
      onChanged(updated);
    } catch (err: unknown) {
      setError(packagingApiErrorMessage(err, "تأیید شمارش ناموفق بود."));
    } finally {
      setBusy(false);
    }
  }

  async function cancel() {
    setBusy(true);
    setError(null);
    try {
      const updated = await api.cancelCount(count.id);
      toast.success("شمارش لغو شد.");
      onChanged(updated);
    } catch (err: unknown) {
      setError(packagingApiErrorMessage(err, "لغو شمارش ناموفق بود."));
    } finally {
      setBusy(false);
    }
  }

  const uncounted = count.lines.filter((line) => line.counted_quantity === null).length;

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>شمارش {formatCountDate(count.count_date)}</DialogTitle>
          <DialogDescription>
            {count.location_name}
            {count.counted_by ? ` — شمارش‌کننده: ${count.counted_by}` : ""}
            {isDraft && uncounted > 0 && ` — ${uncounted} ردیف هنوز شمارش نشده است.`}
          </DialogDescription>
        </DialogHeader>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>ماده</TableHead>
              <TableHead>موردانتظار</TableHead>
              <TableHead>شمارش‌شده</TableHead>
              <TableHead>مغایرت</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {count.lines.map((line) => (
              <TableRow key={line.material_id}>
                <TableCell>{line.material_name}</TableCell>
                <TableCell>{formatQuantity(line.expected_quantity)}</TableCell>
                <TableCell>
                  {isDraft ? (
                    <div className="flex items-center gap-2">
                      <Input
                        value={drafts[line.material_id] ?? line.counted_quantity ?? ""}
                        onChange={(event) =>
                          setDrafts((prev) => ({ ...prev, [line.material_id]: event.target.value }))
                        }
                        inputMode="decimal"
                        className="w-24"
                        aria-label={`شمارش‌شدهٔ ${line.material_name}`}
                        data-testid={`count-line-${line.material_id}`}
                      />
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={busy || drafts[line.material_id] === undefined}
                        onClick={() => saveLine(line.tryton_line_id, line.material_id)}
                        data-testid={`count-line-save-${line.material_id}`}
                      >
                        ثبت
                      </Button>
                    </div>
                  ) : (
                    formatQuantity(line.counted_quantity)
                  )}
                </TableCell>
                <TableCell>
                  {line.variance === null ? (
                    "—"
                  ) : (
                    <span className={Number(line.variance) === 0 ? "" : "font-medium text-destructive"}>
                      {formatQuantity(line.variance)}
                    </span>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            بستن
          </Button>
          {isDraft && (
            <>
              <Button variant="outline" onClick={cancel} disabled={busy} data-testid="count-cancel">
                لغو شمارش
              </Button>
              <Button onClick={confirm} disabled={busy || uncounted > 0} data-testid="count-confirm">
                {busy ? "در حال تأیید…" : "تأیید شمارش"}
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
