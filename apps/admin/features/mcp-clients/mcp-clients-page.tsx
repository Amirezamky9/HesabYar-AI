/**
 * MCP authorized-clients page (US-606) — under هوش مصنوعی و MCP ←
 * کلاینت‌های MCP (spec §18 module 10).
 *
 * Two tabs:
 *
 * - «کلاینت‌ها»: the authorized clients (one API key each — the
 *   registry is a management layer over the API-key system, not a
 *   second credential system). Create / rotate / revoke. The secret
 *   is shown exactly once, in the secret dialog.
 * - «گزارش دسترسی»: the ai_access_audit log — who called what, when,
 *   for which company, with what outcome. Lifecycle rows
 *   (mcp.client.create/rotate/revoke) are kind=مدیریت.
 *
 * Clients are tenant-level, so this page has no company picker; the
 * company grant is a per-client field set at creation.
 */

"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { Ban, Plus, RefreshCw, RotateCw } from "lucide-react";
import {
  ApiError,
  api,
  type McpAuditRowResponse,
  type McpClientResponse,
  type McpClientWithSecretResponse,
} from "@/lib/api-client";
import { formatIsoDateTime } from "@/lib/format-date";
import { fa } from "@/lib/utils";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  Badge,
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
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Skeleton,
  Switch,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  Textarea,
} from "@/components/ui";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import { isPermissionDenied, persianApiErrorMessage } from "@/features/shared/api-error";
import {
  MCP_AUDIT_KIND_LABELS,
  MCP_AUDIT_OUTCOME_LABELS,
  MCP_AUDIT_TRANSPORT_LABELS,
  MCP_MANAGEMENT_TOOLS,
  scopeLabelFa,
} from "./mcp-clients-data";
import { McpClientFormDialog } from "./mcp-client-form-dialog";
import { McpClientSecretDialog } from "./mcp-client-secret-dialog";

type TabId = "clients" | "audit";

const AUDIT_PAGE_LIMIT = 25;

export function McpClientsPage() {
  const router = useRouter();
  const [tab, setTab] = React.useState<TabId>("clients");
  const [clients, setClients] = React.useState<McpClientResponse[]>([]);
  const [clientsLoading, setClientsLoading] = React.useState(true);
  const [clientsError, setClientsError] = React.useState<unknown>(null);
  const [showRevoked, setShowRevoked] = React.useState(false);
  const [reloadKey, setReloadKey] = React.useState(0);

  const [auditRows, setAuditRows] = React.useState<McpAuditRowResponse[]>([]);
  const [auditCursor, setAuditCursor] = React.useState<string | null>(null);
  const [auditLoading, setAuditLoading] = React.useState(false);
  const [auditLoadingMore, setAuditLoadingMore] = React.useState(false);
  const [auditError, setAuditError] = React.useState<unknown>(null);
  const [auditLoaded, setAuditLoaded] = React.useState(false);
  const [kindFilter, setKindFilter] = React.useState("all");
  const [outcomeFilter, setOutcomeFilter] = React.useState("all");
  const [toolFilter, setToolFilter] = React.useState("all");
  const [clientFilter, setClientFilter] = React.useState("all");

  const [formOpen, setFormOpen] = React.useState(false);
  const [secretClient, setSecretClient] = React.useState<McpClientWithSecretResponse | null>(null);
  const [secretMode, setSecretMode] = React.useState<"create" | "rotate">("create");
  const [rotateTarget, setRotateTarget] = React.useState<McpClientResponse | null>(null);
  const [revokeTarget, setRevokeTarget] = React.useState<McpClientResponse | null>(null);
  const [revokeReason, setRevokeReason] = React.useState("");
  const [actingId, setActingId] = React.useState<string | null>(null);
  const [actionError, setActionError] = React.useState<string | null>(null);

  const refreshClients = () => {
    setClientsLoading(true);
    setClientsError(null);
    setReloadKey((k) => k + 1);
  };

  React.useEffect(() => {
    let cancelled = false;
    api
      .listMcpClients({ include_revoked: showRevoked })
      .then((data) => {
        if (cancelled) return;
        setClients(data);
        setClientsLoading(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.replace("/login");
          return;
        }
        setClientsError(err);
        setClientsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [showRevoked, reloadKey, router]);

  const fetchAuditPage = React.useCallback(
    (cursor?: string) =>
      api.listMcpAuditEvents({
        limit: AUDIT_PAGE_LIMIT,
        cursor,
        kind: kindFilter === "all" ? undefined : kindFilter,
        outcome: outcomeFilter === "all" ? undefined : outcomeFilter,
        tool: toolFilter === "all" ? undefined : toolFilter,
        client: clientFilter === "all" ? undefined : clientFilter,
      }),
    [kindFilter, outcomeFilter, toolFilter, clientFilter],
  );

  // The audit tab loads on first open and on every filter change
  // (the bank-rules pattern: state settles in .then/.catch, never
  // synchronously in the effect body). The loading flag is raised by
  // the tab/filter change handlers, which are event handlers.
  React.useEffect(() => {
    if (tab !== "audit") return;
    let cancelled = false;
    fetchAuditPage()
      .then((page) => {
        if (cancelled) return;
        setAuditRows(page.items);
        setAuditCursor(page.next_cursor);
        setAuditLoaded(true);
        setAuditLoading(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.replace("/login");
          return;
        }
        setAuditError(err);
        setAuditLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [tab, fetchAuditPage, router]);

  /** Re-fetch page 1 (retry button, post-revoke refresh). */
  const loadAudit = async () => {
    setAuditLoading(true);
    setAuditError(null);
    try {
      const page = await fetchAuditPage();
      setAuditRows(page.items);
      setAuditCursor(page.next_cursor);
      setAuditLoaded(true);
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setAuditError(err);
    } finally {
      setAuditLoading(false);
    }
  };

  /** Append the next page (load-more button). */
  const loadAuditMore = async () => {
    if (!auditCursor) return;
    setAuditLoadingMore(true);
    try {
      const page = await fetchAuditPage(auditCursor);
      setAuditRows((prev) => [...prev, ...page.items]);
      setAuditCursor(page.next_cursor);
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setAuditError(err);
    } finally {
      setAuditLoadingMore(false);
    }
  };

  /** A filter change reloads the audit list (via the effect above). */
  const handleFilterChange = (setter: (value: string) => void) => (value: string | null) => {
    if (!value) return;
    setter(value);
    setAuditLoading(true);
    setAuditError(null);
  };

  const handleTabChange = (next: string) => {
    setTab(next as TabId);
    if (next === "audit") {
      setAuditLoading(true);
      setAuditError(null);
    }
  };

  const handleCreated = (created: McpClientWithSecretResponse) => {
    setFormOpen(false);
    setSecretMode("create");
    setSecretClient(created);
    refreshClients();
  };

  const handleRotate = async () => {
    if (!rotateTarget) return;
    setActingId(rotateTarget.id);
    setActionError(null);
    try {
      const rotated = await api.rotateMcpClient(rotateTarget.id);
      setRotateTarget(null);
      setSecretMode("rotate");
      setSecretClient(rotated);
      refreshClients();
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setActionError(persianApiErrorMessage(err, "چرخش کلید ناموفق بود."));
    } finally {
      setActingId(null);
    }
  };

  const handleRevoke = async () => {
    if (!revokeTarget) return;
    setActingId(revokeTarget.id);
    setActionError(null);
    try {
      await api.revokeMcpClient(revokeTarget.id, revokeReason.trim() || undefined);
      setRevokeTarget(null);
      setRevokeReason("");
      refreshClients();
      if (auditLoaded) void loadAudit();
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setActionError(persianApiErrorMessage(err, "ابطال کلاینت ناموفق بود."));
    } finally {
      setActingId(null);
    }
  };

  if (clientsLoading) {
    return (
      <div className="flex flex-col gap-4" data-testid="mcp-clients-loading">
        <Skeleton className="h-10 w-64" />
        <Skeleton className="h-32 w-full" />
        <Skeleton className="h-32 w-full" />
      </div>
    );
  }

  if (clientsError) {
    if (isPermissionDenied(clientsError)) {
      return <PermissionDeniedState message="برای مدیریت کلاینت‌های MCP به دسترسی مدیریت کلیدها نیاز دارید." />;
    }
    return (
      <ErrorState
        message={persianApiErrorMessage(clientsError, "بارگذاری کلاینت‌ها ناموفق بود.")}
        onRetry={refreshClients}
      />
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">کلاینت‌های MCP</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            میزبان‌های هوش مصنوعی که می‌توانند از طریق MCP به حساب‌یار متصل شوند. هر کلاینت
            یک کلید دسترسی دارد؛ ابطال کلید، دسترسی را همان لحظه قطع می‌کند.
          </p>
        </div>
        <Button onClick={() => setFormOpen(true)} data-testid="mcp-create-button">
          <Plus className="size-4" />
          کلاینت جدید
        </Button>
      </div>

      {actionError && (
        <p className="text-sm text-destructive" role="alert">
          {actionError}
        </p>
      )}

      <Tabs value={tab} onValueChange={handleTabChange}>
        <TabsList>
          <TabsTrigger value="clients" data-testid="mcp-clients-tab">
            کلاینت‌ها ({fa(clients.length)})
          </TabsTrigger>
          <TabsTrigger value="audit" data-testid="mcp-audit-tab">
            گزارش دسترسی
          </TabsTrigger>
        </TabsList>

        <TabsContent value="clients" className="flex flex-col gap-4">
          <div className="flex items-center gap-2">
            <Switch
              id="mcp-show-revoked"
              checked={showRevoked}
              onCheckedChange={setShowRevoked}
              data-testid="mcp-show-revoked-switch"
            />
            <Label htmlFor="mcp-show-revoked">نمایش کلاینت‌های باطل‌شده</Label>
          </div>

          {clients.length === 0 ? (
            <Card>
              <CardContent className="py-8 text-center text-muted-foreground">
                هنوز هیچ کلاینتی ثبت نشده است. با «کلاینت جدید» اولین کلید دسترسی را صادر
                کنید.
              </CardContent>
            </Card>
          ) : (
            clients.map((client) => (
              <Card key={client.id} data-testid="mcp-client-card" data-client-name={client.name}>
                <CardHeader className="flex flex-row items-start justify-between gap-4">
                  <div className="flex flex-col gap-1">
                    <CardTitle className="text-lg">{client.name}</CardTitle>
                    {client.description && (
                      <p className="text-sm text-muted-foreground">{client.description}</p>
                    )}
                  </div>
                  <Badge variant={client.status === "active" ? "default" : "secondary"}>
                    {client.status === "active" ? "فعال" : "باطل‌شده"}
                  </Badge>
                </CardHeader>
                <CardContent className="flex flex-col gap-3">
                  <div className="flex flex-wrap gap-2 text-sm">
                    <span className="text-muted-foreground">کلید:</span>
                    <span dir="ltr" className="font-mono">
                      {client.key_prefix}…
                    </span>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {client.scopes.map((scope) => (
                      <Badge key={scope} variant="outline" title={scope}>
                        {scopeLabelFa(scope)}
                      </Badge>
                    ))}
                  </div>
                  <div className="grid grid-cols-1 gap-2 text-sm sm:grid-cols-2">
                    <p>
                      <span className="text-muted-foreground">شرکت: </span>
                      {client.company_name ?? "همهٔ شرکت‌ها"}
                    </p>
                    <p>
                      <span className="text-muted-foreground">ایجاد: </span>
                      {formatIsoDateTime(client.created_at)}
                    </p>
                    <p>
                      <span className="text-muted-foreground">آخرین استفاده: </span>
                      {client.last_used_at ? formatIsoDateTime(client.last_used_at) : "هرگز"}
                    </p>
                    <p>
                      <span className="text-muted-foreground">انقضا: </span>
                      {client.expires_at ? formatIsoDateTime(client.expires_at) : "بدون انقضا"}
                    </p>
                    {client.status === "revoked" && (
                      <>
                        <p>
                          <span className="text-muted-foreground">ابطال: </span>
                          {client.revoked_at ? formatIsoDateTime(client.revoked_at) : "—"}
                        </p>
                        {client.revoked_reason && (
                          <p>
                            <span className="text-muted-foreground">دلیل ابطال: </span>
                            {client.revoked_reason}
                          </p>
                        )}
                      </>
                    )}
                  </div>
                  {client.status === "active" && (
                    <div className="flex flex-wrap gap-2">
                      <Button
                        variant="outline"
                        onClick={() => setRotateTarget(client)}
                        disabled={actingId === client.id}
                        data-testid="mcp-rotate-button"
                      >
                        <RotateCw className="size-4" />
                        چرخش کلید
                      </Button>
                      <Button
                        variant="outline"
                        onClick={() => setRevokeTarget(client)}
                        disabled={actingId === client.id}
                        data-testid="mcp-revoke-button"
                      >
                        <Ban className="size-4" />
                        ابطال
                      </Button>
                    </div>
                  )}
                </CardContent>
              </Card>
            ))
          )}
        </TabsContent>

        <TabsContent value="audit" className="flex flex-col gap-4">
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <div className="flex flex-col gap-2">
              <Label htmlFor="mcp-audit-kind">نوع</Label>
              <Select value={kindFilter} onValueChange={handleFilterChange(setKindFilter)}>
                <SelectTrigger id="mcp-audit-kind" data-testid="mcp-audit-kind-filter">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">همه</SelectItem>
                  {Object.entries(MCP_AUDIT_KIND_LABELS).map(([value, label]) => (
                    <SelectItem key={value} value={value}>
                      {label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="mcp-audit-outcome">نتیجه</Label>
              <Select value={outcomeFilter} onValueChange={handleFilterChange(setOutcomeFilter)}>
                <SelectTrigger id="mcp-audit-outcome" data-testid="mcp-audit-outcome-filter">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">همه</SelectItem>
                  {Object.entries(MCP_AUDIT_OUTCOME_LABELS).map(([value, label]) => (
                    <SelectItem key={value} value={value}>
                      {label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="mcp-audit-tool">رویداد مدیریت</Label>
              <Select value={toolFilter} onValueChange={handleFilterChange(setToolFilter)}>
                <SelectTrigger id="mcp-audit-tool" data-testid="mcp-audit-tool-filter">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">همه</SelectItem>
                  {MCP_MANAGEMENT_TOOLS.map(({ value, label }) => (
                    <SelectItem key={value} value={value}>
                      {label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="mcp-audit-client">کلاینت</Label>
              <Select value={clientFilter} onValueChange={handleFilterChange(setClientFilter)}>
                <SelectTrigger id="mcp-audit-client" data-testid="mcp-audit-client-filter">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">همه</SelectItem>
                  {clients.map((client) => (
                    <SelectItem key={client.id} value={client.api_client_id}>
                      {client.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          {auditLoading ? (
            <div className="flex flex-col gap-4" data-testid="mcp-audit-loading">
              <Skeleton className="h-24 w-full" />
              <Skeleton className="h-24 w-full" />
            </div>
          ) : auditError ? (
            <ErrorState
              message={persianApiErrorMessage(auditError, "بارگذاری گزارش دسترسی ناموفق بود.")}
              onRetry={() => loadAudit()}
            />
          ) : auditRows.length === 0 ? (
            <Card>
              <CardContent className="py-8 text-center text-muted-foreground">
                هیچ رویدادی با این فیلترها ثبت نشده است.
              </CardContent>
            </Card>
          ) : (
            <>
              <div className="flex flex-col gap-4">
                {auditRows.map((row) => (
                  <Card key={row.id} data-testid="mcp-audit-row">
                    <CardContent className="flex flex-col gap-2 pt-4">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <span dir="ltr" className="font-mono text-sm">
                          {row.tool}
                        </span>
                        <div className="flex gap-2">
                          <Badge variant="outline">
                            {MCP_AUDIT_KIND_LABELS[row.kind] ?? row.kind}
                          </Badge>
                          <Badge
                            variant={row.outcome === "allowed" ? "default" : "destructive"}
                          >
                            {MCP_AUDIT_OUTCOME_LABELS[row.outcome] ?? row.outcome}
                          </Badge>
                        </div>
                      </div>
                      <div className="grid grid-cols-1 gap-2 text-sm sm:grid-cols-2">
                        <p>
                          <span className="text-muted-foreground">زمان: </span>
                          {formatIsoDateTime(row.created_at)}
                        </p>
                        <p>
                          <span className="text-muted-foreground">کلید: </span>
                          <span dir="ltr" className="font-mono">
                            {row.key_prefix ?? "—"}
                          </span>
                        </p>
                        <p>
                          <span className="text-muted-foreground">شرکت: </span>
                          {row.company_name ?? "—"}
                        </p>
                        <p>
                          <span className="text-muted-foreground">مسیر: </span>
                          {MCP_AUDIT_TRANSPORT_LABELS[row.transport] ?? row.transport}
                        </p>
                        {row.error_code && (
                          <p>
                            <span className="text-muted-foreground">خطا: </span>
                            <span dir="ltr" className="font-mono">
                              {row.error_code}
                            </span>
                          </p>
                        )}
                        {row.client_info && (
                          <p>
                            <span className="text-muted-foreground">میزبان: </span>
                            {row.client_info}
                          </p>
                        )}
                      </div>
                    </CardContent>
                  </Card>
                ))}
              </div>
              {auditCursor && (
                <Button
                  variant="outline"
                  onClick={loadAuditMore}
                  disabled={auditLoadingMore}
                  data-testid="mcp-audit-load-more-button"
                >
                  <RefreshCw className="size-4" />
                  {auditLoadingMore ? "در حال بارگذاری…" : "بارگذاری بیشتر"}
                </Button>
              )}
            </>
          )}
        </TabsContent>
      </Tabs>

      <McpClientFormDialog
        key={formOpen ? "open" : "closed"}
        open={formOpen}
        onClose={() => setFormOpen(false)}
        onCreated={handleCreated}
      />
      <McpClientSecretDialog
        key={secretClient?.id ?? "closed"}
        client={secretClient}
        mode={secretMode}
        onClose={() => setSecretClient(null)}
      />

      <AlertDialog open={rotateTarget !== null} onOpenChange={(next) => !next && setRotateTarget(null)}>
        <AlertDialogContent data-testid="mcp-rotate-confirm-dialog">
          <AlertDialogHeader>
            <AlertDialogTitle>چرخش کلید {rotateTarget?.name}</AlertDialogTitle>
            <AlertDialogDescription>
              کلید فعلی همین حالا باطل می‌شود و یک کلید جدید صادر می‌شود. میزبان باید با کلید
              جدید پیکربندی شود؛ کلید قدیمی دیگر کار نمی‌کند.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>انصراف</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleRotate}
              disabled={actingId === rotateTarget?.id}
              data-testid="mcp-rotate-confirm-button"
            >
              چرخش کلید
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <Dialog open={revokeTarget !== null} onOpenChange={(next) => !next && setRevokeTarget(null)}>
        <DialogContent data-testid="mcp-revoke-dialog">
          <DialogHeader>
            <DialogTitle>ابطال {revokeTarget?.name}</DialogTitle>
            <DialogDescription>
              کلید این کلاینت همین حالا باطل می‌شود و دسترسی آن قطع می‌شود. این کار برگشت‌پذیر
              نیست؛ برای اتصال دوباره باید یک کلاینت جدید ساخت.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-2">
            <Label htmlFor="mcp-revoke-reason">دلیل ابطال (اختیاری)</Label>
            <Textarea
              id="mcp-revoke-reason"
              data-testid="mcp-revoke-reason-input"
              value={revokeReason}
              onChange={(e) => setRevokeReason(e.target.value)}
              placeholder="مثلاً میزبان از رده خارج شد"
            />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRevokeTarget(null)}>
              انصراف
            </Button>
            <Button
              variant="destructive"
              onClick={handleRevoke}
              disabled={actingId === revokeTarget?.id}
              data-testid="mcp-revoke-confirm-button"
            >
              ابطال کلاینت
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
