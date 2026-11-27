/**
 * Approximate price calculator (US-515, Phase 5 wave 2).
 *
 * Goods + packaging (current cost) + shipment (carrier rate or an
 * explicit amount) + margin (percent XOR fixed). The result is
 * labelled approximate and NEVER auto-applies to an invoice — the
 * page states that plainly. Carrier rates are user-entered config,
 * never hard-coded.
 */

"use client";

import * as React from "react";
import { Calculator, Plus, Trash2, TriangleAlert } from "lucide-react";
import {
  api,
  type CarrierRateResponse,
  type MaterialDetailResponse,
  type PriceQuoteResponse,
} from "@/lib/api-client";
import { faNumber } from "@/lib/utils";
import {
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
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
  TableRow,
} from "@/components/ui";
import { CompanyPicker } from "@/features/shared/company-picker";
import { useCompany } from "@/features/shared/use-company";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import { isPermissionDenied } from "@/features/shared/api-error";
import {
  formatQuantity,
  packagingApiErrorMessage,
  parseDecimalInput,
  parseTomanInput,
} from "./packaging-data";

interface QuoteLineDraft {
  key: number;
  materialId: string;
  quantity: string;
}

export function CalculatorPage() {
  const { companies, companyId, setCompanyId, loading: companiesLoading, error: companiesError } =
    useCompany();
  const [materials, setMaterials] = React.useState<MaterialDetailResponse[]>([]);
  const [rates, setRates] = React.useState<CarrierRateResponse[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<unknown>(null);

  const [goodsToman, setGoodsToman] = React.useState("");
  const [lines, setLines] = React.useState<QuoteLineDraft[]>([{ key: 0, materialId: "", quantity: "" }]);
  const [nextKey, setNextKey] = React.useState(1);
  const [shipmentMode, setShipmentMode] = React.useState<"rate" | "explicit" | "none">("rate");
  const [rateId, setRateId] = React.useState("");
  const [shipmentToman, setShipmentToman] = React.useState("");
  const [marginMode, setMarginMode] = React.useState<"percent" | "fixed" | "none">("percent");
  const [marginPercent, setMarginPercent] = React.useState("10");
  const [marginToman, setMarginToman] = React.useState("");

  const [quote, setQuote] = React.useState<PriceQuoteResponse | null>(null);
  const [computing, setComputing] = React.useState(false);
  const [formError, setFormError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!companyId) return;
    let cancelled = false;
    Promise.all([
      api.listPackagingMaterials(companyId, { limit: 100 }),
      api.listCarrierRates(companyId, { active_only: true, limit: 50 }),
    ])
      .then(([page, ratesPage]) => {
        if (cancelled) return;
        setMaterials(page.items);
        setRates(ratesPage.items);
        if (ratesPage.items.length > 0) setRateId(ratesPage.items[0].id);
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
  }, [companyId]);

  async function compute() {
    if (!companyId) return;
    const goodsIrr = parseTomanInput(goodsToman);
    if (goodsIrr === null) {
      setFormError("هزینهٔ کالا را به تومان وارد کنید.");
      return;
    }
    const packaging: { material_id: string; quantity: string }[] = [];
    for (const line of lines) {
      if (!line.materialId && !line.quantity.trim()) continue;
      const quantity = parseDecimalInput(line.quantity);
      if (!line.materialId || quantity === null || Number(quantity) <= 0) {
        setFormError("هر ردیف بسته‌بندی باید ماده و مقدار بزرگ‌تر از صفر داشته باشد.");
        return;
      }
      packaging.push({ material_id: line.materialId, quantity });
    }
    let shipmentCostIrr: number | null = null;
    let carrierRateId: string | null = null;
    if (shipmentMode === "rate") {
      if (!rateId) {
        setFormError("نرخ باربری انتخاب نشده است؛ ابتدا یک نرخ ثبت کنید یا مبلغ را دستی وارد کنید.");
        return;
      }
      carrierRateId = rateId;
    } else if (shipmentMode === "explicit") {
      shipmentCostIrr = parseTomanInput(shipmentToman);
      if (shipmentCostIrr === null) {
        setFormError("هزینهٔ ارسال را به تومان وارد کنید.");
        return;
      }
    }
    let marginPercentValue: string | null = null;
    let marginFixedIrr: number | null = null;
    if (marginMode === "percent") {
      marginPercentValue = parseDecimalInput(marginPercent);
      if (marginPercentValue === null) {
        setFormError("درصد سود باید عدد باشد.");
        return;
      }
    } else if (marginMode === "fixed") {
      marginFixedIrr = parseTomanInput(marginToman);
      if (marginFixedIrr === null) {
        setFormError("سود ثابت را به تومان وارد کنید.");
        return;
      }
    }
    setComputing(true);
    setFormError(null);
    try {
      const result = await api.computePriceQuote({
        company_id: companyId,
        goods_cost_irr: goodsIrr,
        packaging,
        shipment_cost_irr: shipmentCostIrr,
        carrier_rate_id: carrierRateId,
        margin_percent: marginPercentValue,
        margin_fixed_irr: marginFixedIrr,
      });
      setQuote(result);
    } catch (err: unknown) {
      setFormError(packagingApiErrorMessage(err, "محاسبهٔ قیمت ناموفق بود."));
    } finally {
      setComputing(false);
    }
  }

  if (companiesLoading || loading) {
    return (
      <div className="space-y-4" data-testid="calculator-loading">
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
        message={packagingApiErrorMessage(companiesError ?? error, "دریافت اطلاعات ناموفق بود.")}
        onRetry={() => window.location.reload()}
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
          <h1 className="text-2xl font-semibold">محاسبه‌گر قیمت</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            قیمت تقریبی سفارش شامل بسته‌بندی و ارسال — نتیجه تقریبی است و به‌صورت خودکار روی
            فاکتور اعمال نمی‌شود.
          </p>
        </div>
        <CompanyPicker companies={companies} companyId={companyId} onChange={setCompanyId} />
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>ورودی‌ها</CardTitle>
          </CardHeader>
          <CardContent className="space-y-6">
            <div className="space-y-2">
              <Label htmlFor="quote-goods">هزینهٔ کالا (تومان)</Label>
              <Input
                id="quote-goods"
                value={goodsToman}
                onChange={(event) => setGoodsToman(event.target.value)}
                placeholder="مثلاً ۵۰۰٬۰۰۰"
                inputMode="numeric"
                data-testid="quote-goods"
              />
            </div>

            <div className="space-y-2">
              <Label>بسته‌بندی</Label>
              {lines.map((line, index) => (
                <div key={line.key} className="flex items-end gap-2">
                  <div className="flex-1 space-y-2">
                    <Select
                      value={line.materialId}
                      onValueChange={(value) =>
                        setLines((prev) =>
                          prev.map((item) =>
                            item.key === line.key ? { ...item, materialId: value ?? "" } : item,
                          ),
                        )
                      }
                    >
                      <SelectTrigger data-testid={`quote-material-${index}`}>
                        <SelectValue placeholder="انتخاب ماده" />
                      </SelectTrigger>
                      <SelectContent>
                        {materials.map((detail) => (
                          <SelectItem key={detail.material.id} value={detail.material.id}>
                            {detail.material.name_fa}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <Input
                    value={line.quantity}
                    onChange={(event) =>
                      setLines((prev) =>
                        prev.map((item) =>
                          item.key === line.key ? { ...item, quantity: event.target.value } : item,
                        ),
                      )
                    }
                    placeholder="مقدار"
                    inputMode="decimal"
                    className="w-24"
                    aria-label="مقدار"
                    data-testid={`quote-quantity-${index}`}
                  />
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label="حذف ردیف"
                    onClick={() =>
                      setLines((prev) => prev.filter((item) => item.key !== line.key))
                    }
                    disabled={lines.length === 1}
                  >
                    <Trash2 className="size-4" />
                  </Button>
                </div>
              ))}
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  setLines((prev) => [...prev, { key: nextKey, materialId: "", quantity: "" }]);
                  setNextKey((k) => k + 1);
                }}
                data-testid="quote-add-line"
              >
                <Plus className="size-4" />
                افزودن ردیف
              </Button>
            </div>

            <div className="space-y-2">
              <Label>هزینهٔ ارسال</Label>
              <Select value={shipmentMode} onValueChange={(v) => setShipmentMode(v as typeof shipmentMode)}>
                <SelectTrigger data-testid="quote-shipment-mode">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="rate">نرخ باربری ثبت‌شده</SelectItem>
                  <SelectItem value="explicit">مبلغ دستی</SelectItem>
                  <SelectItem value="none">بدون ارسال</SelectItem>
                </SelectContent>
              </Select>
              {shipmentMode === "rate" && (
                <Select value={rateId} onValueChange={(v) => setRateId(v ?? "")}>
                  <SelectTrigger data-testid="quote-rate">
                    <SelectValue placeholder="انتخاب باربری" />
                  </SelectTrigger>
                  <SelectContent>
                    {rates.map((rate) => (
                      <SelectItem key={rate.id} value={rate.id}>
                        {rate.carrier_name} — {faNumber(rate.rate_irr / 10)} تومان
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
              {shipmentMode === "explicit" && (
                <Input
                  value={shipmentToman}
                  onChange={(event) => setShipmentToman(event.target.value)}
                  placeholder="مبلغ ارسال به تومان"
                  inputMode="numeric"
                  data-testid="quote-shipment"
                />
              )}
              {shipmentMode === "rate" && rates.length === 0 && (
                <p className="text-sm text-muted-foreground">
                  نرخی ثبت نشده است؛ از بخش نرخ‌های باربری نرخ اضافه کنید یا مبلغ را دستی وارد کنید.
                </p>
              )}
            </div>

            <div className="space-y-2">
              <Label>سود</Label>
              <Select value={marginMode} onValueChange={(v) => setMarginMode(v as typeof marginMode)}>
                <SelectTrigger data-testid="quote-margin-mode">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="percent">درصدی</SelectItem>
                  <SelectItem value="fixed">مبلغ ثابت</SelectItem>
                  <SelectItem value="none">بدون سود</SelectItem>
                </SelectContent>
              </Select>
              {marginMode === "percent" && (
                <Input
                  value={marginPercent}
                  onChange={(event) => setMarginPercent(event.target.value)}
                  placeholder="درصد سود، مثلاً ۱۰"
                  inputMode="decimal"
                  data-testid="quote-margin-percent"
                />
              )}
              {marginMode === "fixed" && (
                <Input
                  value={marginToman}
                  onChange={(event) => setMarginToman(event.target.value)}
                  placeholder="سود ثابت به تومان"
                  inputMode="numeric"
                  data-testid="quote-margin-fixed"
                />
              )}
            </div>

            {formError && (
              <p role="alert" className="text-sm text-destructive">
                {formError}
              </p>
            )}
            <Button onClick={compute} disabled={computing} data-testid="quote-compute">
              <Calculator className="size-4" />
              {computing ? "در حال محاسبه…" : "محاسبهٔ قیمت"}
            </Button>
          </CardContent>
        </Card>

        <Card data-testid="quote-result">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              نتیجه
              <StatusBadge status="approximate" label="تقریبی" tone="warning" />
            </CardTitle>
          </CardHeader>
          <CardContent>
            {!quote ? (
              <p className="text-sm text-muted-foreground">
                ورودی‌ها را کامل کنید و «محاسبهٔ قیمت» را بزنید.
              </p>
            ) : (
              <div className="space-y-4">
                <Table>
                  <TableBody>
                    <TableRow>
                      <TableCell>هزینهٔ کالا</TableCell>
                      <TableCell className="text-end">
                        <MoneyDisplay amountIrr={quote.goods_cost_irr} size="sm" />
                      </TableCell>
                    </TableRow>
                    <TableRow>
                      <TableCell>بسته‌بندی</TableCell>
                      <TableCell className="text-end">
                        <MoneyDisplay amountIrr={quote.packaging_cost_irr} size="sm" />
                      </TableCell>
                    </TableRow>
                    <TableRow>
                      <TableCell>
                        ارسال{quote.carrier_name ? ` (${quote.carrier_name})` : ""}
                      </TableCell>
                      <TableCell className="text-end">
                        <MoneyDisplay amountIrr={quote.shipment_cost_irr} size="sm" />
                      </TableCell>
                    </TableRow>
                    <TableRow>
                      <TableCell>سود</TableCell>
                      <TableCell className="text-end">
                        <MoneyDisplay amountIrr={quote.margin_irr} size="sm" />
                      </TableCell>
                    </TableRow>
                    <TableRow>
                      <TableCell className="font-semibold">جمع کل</TableCell>
                      <TableCell className="text-end">
                        <MoneyDisplay amountIrr={quote.total_irr} size="lg" />
                      </TableCell>
                    </TableRow>
                  </TableBody>
                </Table>
                {quote.lines.length > 0 && (
                  <div className="space-y-1 text-sm">
                    {quote.lines.map((line) => (
                      <p key={line.material_id} className="flex justify-between gap-2">
                        <span>
                          {line.material_name} × {formatQuantity(line.quantity)}
                          {line.stale_price && " (قیمت قدیمی — ماده تمام شده است)"}
                        </span>
                        <MoneyDisplay amountIrr={line.line_cost_irr} size="sm" />
                      </p>
                    ))}
                  </div>
                )}
                {quote.warnings.length > 0 && (
                  <div className="space-y-1" role="note">
                    {quote.warnings.map((warning) => (
                      <p key={warning} className="flex items-start gap-2 text-sm text-warning-foreground">
                        <TriangleAlert className="mt-0.5 size-4 shrink-0" />
                        {warning}
                      </p>
                    ))}
                  </div>
                )}
                <p className="text-sm text-muted-foreground">
                  این قیمت تقریبی است؛ برای صدور فاکتور باید جداگانه ثبت شود.
                </p>
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
