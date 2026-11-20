/**
 * Packaging materials page (US-515, Phase 5 wave 2) — the warehouse
 * and packaging hub (dashboard ← انبار و بسته‌بندی).
 *
 * Live from the backend via the BFF: the company's materials with
 * live stock, days-of-cover ("when will it run out?") and reorder
 * points; create-material, record-consumption and set-reorder-point
 * dialogs; a supply-check button that reads the Tryton wizard's
 * results and files reorder items; and the per-company settings
 * (engine ids from the provisioning script). Nothing here is fake:
 * an unconfigured company renders an honest setup notice, and an
 * empty catalog renders an honest empty state.
 */

"use client";

import * as React from "react";
import { Package, Plus, RefreshCw, Settings2, TriangleAlert } from "lucide-react";
import {
  ApiError,
  api,
  type MaterialDetailResponse,
  type PackagingSettingsResponse,
  type SupplyCheckReportResponse,
} from "@/lib/api-client";
import { faNumber } from "@/lib/utils";
import {
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Input,
  Label,
  MoneyDisplay,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
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
  costMethodLabel,
  coverageTone,
  formatDaysOfCover,
  formatQuantity,
  packagingApiErrorMessage,
  parseDecimalInput,
} from "./packaging-data";

const PAGE_SIZE = 20;

export function MaterialsPage() {
  const { companies, companyId, setCompanyId, loading: companiesLoading, error: companiesError } =
    useCompany();
  const [settings, setSettings] = React.useState<PackagingSettingsResponse | null>(null);
  const [materials, setMaterials] = React.useState<MaterialDetailResponse[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<unknown>(null);
  const [refreshNonce, setRefreshNonce] = React.useState(0);
  const [supplyReport, setSupplyReport] = React.useState<SupplyCheckReportResponse | null>(null);
  const [supplyRunning, setSupplyRunning] = React.useState(false);

  const [createOpen, setCreateOpen] = React.useState(false);
  const [consumeTarget, setConsumeTarget] = React.useState<MaterialDetailResponse | null>(null);
  const [reorderTarget, setReorderTarget] = React.useState<MaterialDetailResponse | null>(null);
  const [settingsOpen, setSettingsOpen] = React.useState(false);

  React.useEffect(() => {
    if (!companyId) return;
    let cancelled = false;
    Promise.all([
      api.getPackagingSettings(companyId),
      api.listPackagingMaterials(companyId, { limit: PAGE_SIZE }),
    ])
      .then(([nextSettings, page]) => {
        if (cancelled) return;
        setSettings(nextSettings);
        setMaterials(page.items);
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

  const configured =
    settings !== null &&
    settings.packaging_category_id !== null &&
    settings.packaging_location_id !== null &&
    settings.production_location_id !== null;

  async function runSupplyCheck() {
    if (!companyId) return;
    setSupplyRunning(true);
    try {
      const report = await api.runSupplyCheck({ company_id: companyId });
      setSupplyReport(report);
      if (report.items_filed > 0) {
        toast.success(`${faNumber(report.items_filed)} مورد سفارش در صف بازبینی ثبت شد.`);
      } else {
        toast.success("بررسی تأمین کامل شد؛ مورد جدیدی ثبت نشد.");
      }
    } catch (err: unknown) {
      toast.error(packagingApiErrorMessage(err, "بررسی تأمین ناموفق بود."));
    } finally {
      setSupplyRunning(false);
    }
  }

  if (companiesLoading) {
    return (
      <div className="space-y-4" data-testid="packaging-loading">
        <Skeleton className="h-10 w-64" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (companiesError) {
    return <ErrorState message="دریافت فهرست شرکت‌ها ناموفق بود." onRetry={refresh} />;
  }

  if (!companyId) {
    return (
      <div className="rounded-lg border p-6 text-center">
        <p className="font-medium">شرکتی در دسترس نیست</p>
        <p className="mt-2 text-sm text-muted-foreground">
          برای مشاهدهٔ انبار بسته‌بندی، ابتدا باید یک شرکت در دسترس باشد.
        </p>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="space-y-4" data-testid="packaging-loading">
        <Skeleton className="h-10 w-64" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (error) {
    if (isPermissionDenied(error)) {
      return <PermissionDeniedState message="کلید شما برای بخش بسته‌بندی دسترسی ندارد." />;
    }
    return (
      <ErrorState
        message={packagingApiErrorMessage(error, "دریافت مواد بسته‌بندی ناموفق بود.")}
        onRetry={refresh}
      />
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">انبار و بسته‌بندی</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            مواد بسته‌بندی، موجودی و پوشش روزها — «کی تمام می‌شود؟»
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <CompanyPicker companies={companies} companyId={companyId} onChange={setCompanyId} />
          <Button variant="outline" onClick={() => setSettingsOpen(true)} data-testid="packaging-settings">
            <Settings2 className="size-4" />
            تنظیمات
          </Button>
          <Button variant="outline" onClick={runSupplyCheck} disabled={supplyRunning} data-testid="supply-check">
            <RefreshCw className="size-4" />
            {supplyRunning ? "در حال بررسی…" : "بررسی تأمین"}
          </Button>
          <Button onClick={() => setCreateOpen(true)} disabled={!configured} data-testid="create-material">
            <Plus className="size-4" />
            مادهٔ جدید
          </Button>
        </div>
      </div>

      {supplyReport && (
        <Card data-testid="supply-report">
          <CardContent className="flex flex-wrap items-center gap-x-6 gap-y-2 pt-6 text-sm">
            <span>
              مواد بررسی‌شده: <strong>{faNumber(supplyReport.materials_checked)}</strong>
            </span>
            <span>
              درخواست خرید جدید: <strong>{faNumber(supplyReport.purchase_requests_new)}</strong>
            </span>
            <span>
              موارد ثبت‌شده در صف: <strong>{faNumber(supplyReport.items_filed)}</strong>
            </span>
            {supplyReport.errors.length > 0 && (
              <span className="text-destructive">{supplyReport.errors[0]}</span>
            )}
          </CardContent>
        </Card>
      )}

      {!configured && (
        <Card data-testid="packaging-setup-notice">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <TriangleAlert className="size-5 text-warning" />
              پیکربندی بسته‌بندی کامل نیست
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 text-sm text-muted-foreground">
            <p>
              برای ثبت مواد بسته‌بندی، ابتدا باید دسته‌بندی مواد، انبار بسته‌بندی و انبار مصرف در
              تنظیمات مشخص شوند. این شناسه‌ها را اسکریپت تأمین ترایتون (
              <span dir="ltr">setup_packaging_us515.py</span>) چاپ می‌کند.
            </p>
            <Button variant="outline" onClick={() => setSettingsOpen(true)}>
              باز کردن تنظیمات
            </Button>
          </CardContent>
        </Card>
      )}

      {materials.length === 0 ? (
        <Card data-testid="packaging-empty">
          <CardContent className="flex flex-col items-center gap-2 py-12 text-center">
            <Package className="size-10 text-muted-foreground" />
            <p className="font-medium">هنوز مادهٔ بسته‌بندی ثبت نشده است</p>
            <p className="text-sm text-muted-foreground">
              کارتن، چسب، نایلون حبابدار و سایر مواد مصرفی بسته‌بندی را اینجا ثبت کنید.
            </p>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>ماده</TableHead>
                  <TableHead>موجودی</TableHead>
                  <TableHead>قیمت واحد</TableHead>
                  <TableHead>پوشش روزها</TableHead>
                  <TableHead>نقطهٔ سفارش</TableHead>
                  <TableHead>اقدام</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {materials.map((detail) => {
                  const material = detail.material;
                  const stock = detail.stock;
                  const coverage = detail.coverage;
                  const alertDays = settings?.low_stock_alert_days ?? 7;
                  return (
                    <TableRow key={material.id} data-testid={`material-row-${material.id}`}>
                      <TableCell>
                        <div className="font-medium">{material.name_fa}</div>
                        <div className="text-xs text-muted-foreground">
                          {material.code ? `کد: ${material.code} — ` : ""}
                          {costMethodLabel(material.cost_method)}
                          {!material.is_active && " — غیرفعال"}
                        </div>
                      </TableCell>
                      <TableCell>
                        {stock ? (
                          <span className={stock.is_negative ? "font-medium text-destructive" : ""}>
                            {formatQuantity(stock.quantity)}
                            {stock.is_negative && " (منفی)"}
                          </span>
                        ) : (
                          "—"
                        )}
                      </TableCell>
                      <TableCell>
                        {stock?.unit_cost_irr ? (
                          <MoneyDisplay
                            amountIrr={Math.round(Number(stock.unit_cost_irr))}
                            size="sm"
                          />
                        ) : (
                          "—"
                        )}
                      </TableCell>
                      <TableCell>
                        {coverage ? (
                          <StatusBadge
                            status="coverage"
                            label={formatDaysOfCover(coverage.days_of_cover)}
                            tone={coverageTone(
                              coverage.days_of_cover,
                              alertDays,
                              coverage.is_out_of_stock,
                            )}
                          />
                        ) : (
                          "—"
                        )}
                      </TableCell>
                      <TableCell>
                        {detail.reorder_point ? (
                          <span>
                            کمینه {formatQuantity(detail.reorder_point.min_quantity)} — هدف{" "}
                            {formatQuantity(detail.reorder_point.target_quantity)}
                          </span>
                        ) : (
                          <span className="text-muted-foreground">تعیین نشده</span>
                        )}
                      </TableCell>
                      <TableCell>
                        <div className="flex flex-wrap gap-2">
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => setConsumeTarget(detail)}
                            disabled={!material.is_active}
                            data-testid={`consume-${material.id}`}
                          >
                            ثبت مصرف
                          </Button>
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => setReorderTarget(detail)}
                            data-testid={`reorder-${material.id}`}
                          >
                            نقطهٔ سفارش
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}

      <CreateMaterialDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        companyId={companyId}
        onCreated={refresh}
      />
      {consumeTarget && (
        <ConsumeDialog
          detail={consumeTarget}
          onOpenChange={(open) => {
            if (!open) setConsumeTarget(null);
          }}
          companyId={companyId}
          onRecorded={refresh}
        />
      )}
      {reorderTarget && (
        <ReorderPointDialog
          detail={reorderTarget}
          onOpenChange={(open) => {
            if (!open) setReorderTarget(null);
          }}
          companyId={companyId}
          onSaved={refresh}
        />
      )}
      {settings && (
        <SettingsDialog
          open={settingsOpen}
          onOpenChange={setSettingsOpen}
          settings={settings}
          onSaved={refresh}
        />
      )}
    </div>
  );
}

function CreateMaterialDialog({
  open,
  onOpenChange,
  companyId,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  companyId: string;
  onCreated: () => void;
}) {
  const [name, setName] = React.useState("");
  const [code, setCode] = React.useState("");
  const [costMethod, setCostMethod] = React.useState("average");
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  async function submit() {
    if (!name.trim()) {
      setError("نام ماده را وارد کنید.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await api.createPackagingMaterial({
        company_id: companyId,
        name_fa: name.trim(),
        cost_method: costMethod,
        code: code.trim() || null,
      });
      toast.success("مادهٔ بسته‌بندی ثبت شد.");
      setName("");
      setCode("");
      setCostMethod("average");
      onOpenChange(false);
      onCreated();
    } catch (err: unknown) {
      setError(packagingApiErrorMessage(err, "ثبت ماده ناموفق بود."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>مادهٔ بسته‌بندی جدید</DialogTitle>
          <DialogDescription>
            ماده به‌عنوان کالای مصرفی در انبار ترایتون ثبت می‌شود؛ مواد مصرفی هزینهٔ عملیاتی‌اند، نه
            دارایی ثابت.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="material-name">نام ماده</Label>
            <Input
              id="material-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="مثلاً کارتن مقوایی"
              data-testid="material-name"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="material-code">کد (اختیاری)</Label>
            <Input
              id="material-code"
              value={code}
              onChange={(event) => setCode(event.target.value)}
              placeholder="مثلاً BOX-01"
              dir="ltr"
              data-testid="material-code"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="material-cost-method">روش قیمت تمام‌شده</Label>
            <Select value={costMethod} onValueChange={(v) => setCostMethod(v ?? "average")}>
              <SelectTrigger id="material-cost-method" data-testid="material-cost-method">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="average">میانگین (پیش‌فرض)</SelectItem>
                <SelectItem value="fifo">فایفو (نیازمند ماژول فعال)</SelectItem>
              </SelectContent>
            </Select>
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
          <Button onClick={submit} disabled={saving} data-testid="material-submit">
            {saving ? "در حال ثبت…" : "ثبت ماده"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ConsumeDialog({
  detail,
  onOpenChange,
  companyId,
  onRecorded,
}: {
  detail: MaterialDetailResponse;
  onOpenChange: (open: boolean) => void;
  companyId: string;
  onRecorded: () => void;
}) {
  const [orderRef, setOrderRef] = React.useState("");
  const [shipmentRef, setShipmentRef] = React.useState("");
  const [quantity, setQuantity] = React.useState("");
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  async function submit() {
    const parsed = parseDecimalInput(quantity);
    if (!orderRef.trim()) {
      setError("شمارهٔ سفارش را وارد کنید.");
      return;
    }
    if (parsed === null || Number(parsed) <= 0) {
      setError("مقدار مصرف باید عددی بزرگ‌تر از صفر باشد.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const record = await api.recordConsumption({
        company_id: companyId,
        material_id: detail.material.id,
        order_ref: orderRef.trim(),
        shipment_ref: shipmentRef.trim() || null,
        quantity: parsed,
      });
      toast.success(
        `مصرف ثبت شد؛ هزینهٔ ${faNumber(Math.round(record.total_cost_irr / 10))} تومان به سفارش ${orderRef.trim()} تخصیص یافت.`,
      );
      onOpenChange(false);
      onRecorded();
    } catch (err: unknown) {
      if (err instanceof ApiError && err.code === "PACKAGING_INVALID_CONSUMPTION") {
        setError("مصرف نامعتبر است؛ مقدار را بررسی کنید.");
      } else {
        setError(packagingApiErrorMessage(err, "ثبت مصرف ناموفق بود."));
      }
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>ثبت مصرف «{detail.material.name_fa}»</DialogTitle>
          <DialogDescription>
            مصرف، موجودی انبار بسته‌بندی را کم می‌کند و هزینه را به مرکز هزینهٔ سفارش تخصیص می‌دهد.
            موجودی فعلی: {detail.stock ? formatQuantity(detail.stock.quantity) : "—"}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="consume-order">شمارهٔ سفارش</Label>
            <Input
              id="consume-order"
              value={orderRef}
              onChange={(event) => setOrderRef(event.target.value)}
              placeholder="مثلاً ORD-1001"
              dir="ltr"
              data-testid="consume-order"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="consume-shipment">مرجع ارسال (اختیاری)</Label>
            <Input
              id="consume-shipment"
              value={shipmentRef}
              onChange={(event) => setShipmentRef(event.target.value)}
              placeholder="مثلاً SHIP-1"
              dir="ltr"
              data-testid="consume-shipment"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="consume-quantity">مقدار مصرف</Label>
            <Input
              id="consume-quantity"
              value={quantity}
              onChange={(event) => setQuantity(event.target.value)}
              placeholder="مثلاً ۲"
              inputMode="decimal"
              data-testid="consume-quantity"
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
          <Button onClick={submit} disabled={saving} data-testid="consume-submit">
            {saving ? "در حال ثبت…" : "ثبت مصرف"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ReorderPointDialog({
  detail,
  onOpenChange,
  companyId,
  onSaved,
}: {
  detail: MaterialDetailResponse;
  onOpenChange: (open: boolean) => void;
  companyId: string;
  onSaved: () => void;
}) {
  const point = detail.reorder_point;
  const [minQuantity, setMinQuantity] = React.useState(point?.min_quantity ?? "");
  const [targetQuantity, setTargetQuantity] = React.useState(point?.target_quantity ?? "");
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  async function submit() {
    const min = parseDecimalInput(minQuantity);
    const target = parseDecimalInput(targetQuantity);
    if (min === null || target === null || Number(target) <= 0 || Number(min) > Number(target)) {
      setError("کمینه و هدف باید عدد باشند؛ هدف بزرگ‌تر از صفر و بزرگ‌تر یا مساوی کمینه باشد.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await api.setReorderPoint(detail.material.id, {
        company_id: companyId,
        min_quantity: min,
        target_quantity: target,
      });
      toast.success("نقطهٔ سفارش ذخیره شد.");
      onOpenChange(false);
      onSaved();
    } catch (err: unknown) {
      setError(packagingApiErrorMessage(err, "ذخیرهٔ نقطهٔ سفارش ناموفق بود."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>نقطهٔ سفارش «{detail.material.name_fa}»</DialogTitle>
          <DialogDescription>
            وقتی موجودی به زیر کمینه برسد، اجرای روزانهٔ تأمین در ترایتون درخواست خرید خودکار
            می‌سازد و موردی در صف بازبینی ثبت می‌شود.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="reorder-min">کمینه (سفارش وقتی موجودی به زیر این برسد)</Label>
            <Input
              id="reorder-min"
              value={minQuantity}
              onChange={(event) => setMinQuantity(event.target.value)}
              placeholder="مثلاً ۱۰"
              inputMode="decimal"
              data-testid="reorder-min"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="reorder-target">هدف (سفارش تا این مقدار)</Label>
            <Input
              id="reorder-target"
              value={targetQuantity}
              onChange={(event) => setTargetQuantity(event.target.value)}
              placeholder="مثلاً ۵۰"
              inputMode="decimal"
              data-testid="reorder-target"
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
          <Button onClick={submit} disabled={saving} data-testid="reorder-submit">
            {saving ? "در حال ذخیره…" : "ذخیرهٔ نقطهٔ سفارش"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function SettingsDialog({
  open,
  onOpenChange,
  settings,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  settings: PackagingSettingsResponse;
  onSaved: () => void;
}) {
  const [locationId, setLocationId] = React.useState(
    settings.packaging_location_id === null ? "" : String(settings.packaging_location_id),
  );
  const [productionId, setProductionId] = React.useState(
    settings.production_location_id === null ? "" : String(settings.production_location_id),
  );
  const [warehouseId, setWarehouseId] = React.useState(
    settings.warehouse_id === null ? "" : String(settings.warehouse_id),
  );
  const [categoryId, setCategoryId] = React.useState(
    settings.packaging_category_id === null ? "" : String(settings.packaging_category_id),
  );
  const [rootId, setRootId] = React.useState(
    settings.analytic_root_id === null ? "" : String(settings.analytic_root_id),
  );
  const [windowDays, setWindowDays] = React.useState(String(settings.days_of_cover_window_days));
  const [alertDays, setAlertDays] = React.useState(String(settings.low_stock_alert_days));
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  function parseId(raw: string): number | null {
    const trimmed = raw.trim();
    if (trimmed === "") return null;
    const value = Number(trimmed);
    return Number.isInteger(value) && value > 0 ? value : -1;
  }

  async function submit() {
    const ids = [parseId(locationId), parseId(productionId), parseId(warehouseId), parseId(categoryId), parseId(rootId)];
    if (ids.some((id) => id === -1)) {
      setError("شناسه‌های ترایتون باید عدد صحیح مثبت باشند.");
      return;
    }
    const window = Number(windowDays);
    const alert = Number(alertDays);
    if (!Number.isInteger(window) || window < 1 || !Number.isInteger(alert) || alert < 0) {
      setError("بازهٔ پوشش باید حداقل ۱ روز و آستانهٔ هشدار حداقل ۰ روز باشد.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await api.savePackagingSettings({
        company_id: settings.company_id,
        packaging_location_id: ids[0],
        production_location_id: ids[1],
        warehouse_id: ids[2],
        packaging_category_id: ids[3],
        analytic_root_id: ids[4],
        days_of_cover_window_days: window,
        low_stock_alert_days: alert,
      });
      toast.success("تنظیمات بسته‌بندی ذخیره شد.");
      onOpenChange(false);
      onSaved();
    } catch (err: unknown) {
      setError(packagingApiErrorMessage(err, "ذخیرهٔ تنظیمات ناموفق بود."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>تنظیمات بسته‌بندی</DialogTitle>
          <DialogDescription>
            شناسه‌های ترایتون را اسکریپت تأمین چاپ می‌کند. بدون دسته‌بندی و انبارها، ثبت ماده ممکن
            نیست.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="settings-location">انبار بسته‌بندی (PACK)</Label>
              <Input
                id="settings-location"
                value={locationId}
                onChange={(event) => setLocationId(event.target.value)}
                inputMode="numeric"
                dir="ltr"
                data-testid="settings-location"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="settings-production">انبار مصرف (PACK-CONSUME)</Label>
              <Input
                id="settings-production"
                value={productionId}
                onChange={(event) => setProductionId(event.target.value)}
                inputMode="numeric"
                dir="ltr"
                data-testid="settings-production"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="settings-warehouse">انبار اصلی (WH)</Label>
              <Input
                id="settings-warehouse"
                value={warehouseId}
                onChange={(event) => setWarehouseId(event.target.value)}
                inputMode="numeric"
                dir="ltr"
                data-testid="settings-warehouse"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="settings-category">دسته‌بندی مواد</Label>
              <Input
                id="settings-category"
                value={categoryId}
                onChange={(event) => setCategoryId(event.target.value)}
                inputMode="numeric"
                dir="ltr"
                data-testid="settings-category"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="settings-root">ریشهٔ تحلیلی سفارش‌ها</Label>
              <Input
                id="settings-root"
                value={rootId}
                onChange={(event) => setRootId(event.target.value)}
                inputMode="numeric"
                dir="ltr"
                data-testid="settings-root"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="settings-window">بازهٔ پوشش (روز)</Label>
              <Input
                id="settings-window"
                value={windowDays}
                onChange={(event) => setWindowDays(event.target.value)}
                inputMode="numeric"
                data-testid="settings-window"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="settings-alert">آستانهٔ هشدار کم‌موجودی (روز)</Label>
              <Input
                id="settings-alert"
                value={alertDays}
                onChange={(event) => setAlertDays(event.target.value)}
                inputMode="numeric"
                data-testid="settings-alert"
              />
            </div>
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
          <Button onClick={submit} disabled={saving} data-testid="settings-submit">
            {saving ? "در حال ذخیره…" : "ذخیرهٔ تنظیمات"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
