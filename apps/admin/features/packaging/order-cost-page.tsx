/**
 * Per-order cost lookup (US-515, Phase 5 wave 2).
 *
 * One analytic account per order; packaging consumption, shipment
 * cost and cost of goods share the cost centre, so the full cost of
 * an order comes from one report. The order link wires to the store
 * connector (US-511) when it lands — order association is manual
 * entry for now. Shipment cost is recorded here as three separate
 * figures: invoiced to the customer, actually paid to the carrier,
 * and absorbed into the margin (derived).
 */

"use client";

import * as React from "react";
import { Search } from "lucide-react";
import { api, type OrderCostReportResponse } from "@/lib/api-client";
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
  Table,
  TableBody,
  TableCell,
  TableRow,
  toast,
} from "@/components/ui";
import { CompanyPicker } from "@/features/shared/company-picker";
import { useCompany } from "@/features/shared/use-company";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import { isPermissionDenied } from "@/features/shared/api-error";
import { packagingApiErrorMessage, parseTomanInput, shipmentCostMethodLabel } from "./packaging-data";

export function OrderCostPage() {
  const { companies, companyId, setCompanyId, loading: companiesLoading, error: companiesError } =
    useCompany();
  const [orderRef, setOrderRef] = React.useState("");
  const [report, setReport] = React.useState<OrderCostReportResponse | null>(null);
  const [searchedRef, setSearchedRef] = React.useState<string | null>(null);
  const [loadingReport, setLoadingReport] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const [method, setMethod] = React.useState("none");
  const [actualToman, setActualToman] = React.useState("");
  const [invoicedToman, setInvoicedToman] = React.useState("");
  const [carrierName, setCarrierName] = React.useState("");
  const [savingShipment, setSavingShipment] = React.useState(false);
  const [shipmentError, setShipmentError] = React.useState<string | null>(null);

  async function loadReport(ref: string) {
    if (!companyId) return;
    setLoadingReport(true);
    setError(null);
    try {
      const result = await api.getOrderCost(ref, companyId);
      setReport(result);
      setSearchedRef(ref);
    } catch (err: unknown) {
      setReport(null);
      setSearchedRef(null);
      setError(packagingApiErrorMessage(err, "دریافت هزینهٔ سفارش ناموفق بود."));
    } finally {
      setLoadingReport(false);
    }
  }

  async function createCostCenter() {
    if (!companyId || !searchedRef) return;
    setLoadingReport(true);
    setError(null);
    try {
      await api.createOrderCostCenter(searchedRef, {
        company_id: companyId,
        order_ref: searchedRef,
      });
      toast.success("مرکز هزینهٔ سفارش ساخته شد.");
      await loadReport(searchedRef);
    } catch (err: unknown) {
      setError(packagingApiErrorMessage(err, "ساخت مرکز هزینه ناموفق بود."));
    } finally {
      setLoadingReport(false);
    }
  }

  async function saveShipmentCost() {
    if (!companyId || !searchedRef) return;
    const actualIrr = parseTomanInput(actualToman);
    if (actualIrr === null) {
      setShipmentError("مبلغ پرداختی به باربری را به تومان وارد کنید.");
      return;
    }
    const invoicedIrr = invoicedToman.trim() === "" ? null : parseTomanInput(invoicedToman);
    if (invoicedToman.trim() !== "" && invoicedIrr === null) {
      setShipmentError("مبلغ فاکتورشده به مشتری را به تومان وارد کنید.");
      return;
    }
    setSavingShipment(true);
    setShipmentError(null);
    try {
      await api.recordShipmentCost({
        company_id: companyId,
        order_ref: searchedRef,
        method,
        actual_irr: actualIrr,
        invoiced_irr: invoicedIrr,
        carrier_name: carrierName.trim() || null,
      });
      toast.success("هزینهٔ ارسال ثبت شد.");
      setActualToman("");
      setInvoicedToman("");
      setCarrierName("");
      await loadReport(searchedRef);
    } catch (err: unknown) {
      setShipmentError(packagingApiErrorMessage(err, "ثبت هزینهٔ ارسال ناموفق بود."));
    } finally {
      setSavingShipment(false);
    }
  }

  if (companiesLoading) {
    return (
      <div className="space-y-4" data-testid="order-cost-loading">
        <Skeleton className="h-10 w-64" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (companiesError) {
    if (isPermissionDenied(companiesError)) {
      return <PermissionDeniedState message="کلید شما برای بخش بسته‌بندی دسترسی ندارد." />;
    }
    return (
      <ErrorState
        message="دریافت فهرست شرکت‌ها ناموفق بود."
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
          <h1 className="text-2xl font-semibold">هزینهٔ کامل سفارش</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            بهای تمام‌شدهٔ هر سفارش از یک مرکز هزینه: کالا، بسته‌بندی و ارسال.
          </p>
        </div>
        <CompanyPicker companies={companies} companyId={companyId} onChange={setCompanyId} />
      </div>

      <Card>
        <CardContent className="flex flex-wrap items-end gap-2 pt-6">
          <div className="min-w-56 flex-1 space-y-2">
            <Label htmlFor="order-ref">شمارهٔ سفارش</Label>
            <Input
              id="order-ref"
              value={orderRef}
              onChange={(event) => setOrderRef(event.target.value)}
              placeholder="مثلاً ORD-1001"
              dir="ltr"
              data-testid="order-ref"
            />
          </div>
          <Button
            onClick={() => {
              if (orderRef.trim()) loadReport(orderRef.trim());
            }}
            disabled={loadingReport || !orderRef.trim()}
            data-testid="order-search"
          >
            <Search className="size-4" />
            نمایش هزینه
          </Button>
        </CardContent>
      </Card>

      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}

      {loadingReport && (
        <div className="space-y-4" data-testid="order-cost-loading">
          <Skeleton className="h-64 w-full" />
        </div>
      )}

      {!loadingReport && searchedRef && report && (
        <div className="grid gap-6 lg:grid-cols-2">
          <Card data-testid="order-cost-report">
            <CardHeader>
              <CardTitle>هزینهٔ سفارش {searchedRef}</CardTitle>
            </CardHeader>
            <CardContent>
              {report.analytic_account_engine_id === null ? (
                <div className="space-y-3">
                  <p className="text-sm text-muted-foreground">
                    برای این سفارش هنوز مرکز هزینه ساخته نشده است؛ هزینه‌های کالا از خطوط تحلیلی
                    خوانده نمی‌شوند.
                  </p>
                  <Button onClick={createCostCenter} data-testid="order-create-center">
                    ساخت مرکز هزینه
                  </Button>
                </div>
              ) : (
                <Table>
                  <TableBody>
                    <TableRow>
                      <TableCell>هزینهٔ کالا</TableCell>
                      <TableCell className="text-end">
                        <MoneyDisplay amountIrr={report.goods_cost_irr} size="sm" />
                      </TableCell>
                    </TableRow>
                    <TableRow>
                      <TableCell>بسته‌بندی ({report.consumption_count} مصرف)</TableCell>
                      <TableCell className="text-end">
                        <MoneyDisplay amountIrr={report.packaging_cost_irr} size="sm" />
                      </TableCell>
                    </TableRow>
                    <TableRow>
                      <TableCell>ارسال — فاکتورشده به مشتری</TableCell>
                      <TableCell className="text-end">
                        <MoneyDisplay amountIrr={report.shipment_invoiced_irr} size="sm" />
                      </TableCell>
                    </TableRow>
                    <TableRow>
                      <TableCell>ارسال — پرداختی به باربری</TableCell>
                      <TableCell className="text-end">
                        <MoneyDisplay amountIrr={report.shipment_actual_irr} size="sm" />
                      </TableCell>
                    </TableRow>
                    <TableRow>
                      <TableCell>ارسال — جذب‌شده در سود</TableCell>
                      <TableCell className="text-end">
                        <MoneyDisplay amountIrr={report.shipment_absorbed_irr} size="sm" />
                      </TableCell>
                    </TableRow>
                    <TableRow>
                      <TableCell>درآمد سفارش</TableCell>
                      <TableCell className="text-end">
                        <MoneyDisplay amountIrr={report.revenue_irr} size="sm" />
                      </TableCell>
                    </TableRow>
                    <TableRow>
                      <TableCell className="font-semibold">جمع هزینه</TableCell>
                      <TableCell className="text-end">
                        <MoneyDisplay amountIrr={report.total_cost_irr} size="md" />
                      </TableCell>
                    </TableRow>
                    <TableRow>
                      <TableCell className="font-semibold">سود</TableCell>
                      <TableCell className="text-end">
                        <MoneyDisplay amountIrr={report.margin_irr} size="lg" />
                      </TableCell>
                    </TableRow>
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>ثبت هزینهٔ ارسال</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="shipment-method">روش محاسبهٔ هزینهٔ ارسال</Label>
                <Select value={method} onValueChange={(v) => setMethod(v ?? "none")}>
                  <SelectTrigger id="shipment-method" data-testid="shipment-method">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="on_order">{shipmentCostMethodLabel("on_order")}</SelectItem>
                    <SelectItem value="on_shipment">{shipmentCostMethodLabel("on_shipment")}</SelectItem>
                    <SelectItem value="none">{shipmentCostMethodLabel("none")}</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-2">
                <Label htmlFor="shipment-actual">مبلغ پرداختی به باربری (تومان)</Label>
                <Input
                  id="shipment-actual"
                  value={actualToman}
                  onChange={(event) => setActualToman(event.target.value)}
                  placeholder="مثلاً ۸٬۵۰۰"
                  inputMode="numeric"
                  data-testid="shipment-actual"
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="shipment-invoiced">مبلغ فاکتورشده به مشتری (تومان، اختیاری)</Label>
                <Input
                  id="shipment-invoiced"
                  value={invoicedToman}
                  onChange={(event) => setInvoicedToman(event.target.value)}
                  placeholder="مثلاً ۷٬۵۰۰"
                  inputMode="numeric"
                  data-testid="shipment-invoiced"
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="shipment-carrier">نام باربری (اختیاری)</Label>
                <Input
                  id="shipment-carrier"
                  value={carrierName}
                  onChange={(event) => setCarrierName(event.target.value)}
                  placeholder="مثلاً تیپاکس"
                  data-testid="shipment-carrier"
                />
              </div>
              {shipmentError && (
                <p role="alert" className="text-sm text-destructive">
                  {shipmentError}
                </p>
              )}
              <Button onClick={saveShipmentCost} disabled={savingShipment} data-testid="shipment-submit">
                {savingShipment ? "در حال ثبت…" : "ثبت هزینهٔ ارسال"}
              </Button>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}
