/**
 * The one-time secret dialog (US-606).
 *
 * Shown once after create/rotate. The secret is rendered from the
 * in-memory response only — it is never written to localStorage,
 * never logged, and never fetched again. Closing this dialog loses
 * it for good; the operator can always rotate the key for a new one.
 */

"use client";

import * as React from "react";
import { Check, Copy, TriangleAlert } from "lucide-react";
import type { McpClientWithSecretResponse } from "@/lib/api-client";
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Input,
} from "@/components/ui";

interface McpClientSecretDialogProps {
  /** Null = closed. */
  client: McpClientWithSecretResponse | null;
  /** "create" for a new client, "rotate" for a rotated key. */
  mode: "create" | "rotate";
  onClose: () => void;
}

export function McpClientSecretDialog({ client, mode, onClose }: McpClientSecretDialogProps) {
  // The parent remounts this dialog per client (key={client.id}), so
  // `copied` starts false for every new secret — no effect needed.
  const [copied, setCopied] = React.useState(false);

  const handleCopy = async () => {
    if (!client) return;
    try {
      await navigator.clipboard.writeText(client.secret);
      setCopied(true);
    } catch {
      // Clipboard unavailable (non-secure context): the operator can
      // still select the text manually.
      setCopied(false);
    }
  };

  return (
    <Dialog open={client !== null} onOpenChange={(next) => !next && onClose()}>
      <DialogContent data-testid="mcp-client-secret-dialog">
        <DialogHeader>
          <DialogTitle>
            {mode === "create" ? "کلاینت ساخته شد" : "کلید چرخش خورد"}
          </DialogTitle>
          <DialogDescription>
            {client?.name} — کلید جدید این کلاینت است.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4">
          <div className="flex items-start gap-2 rounded-md border border-border bg-muted p-3">
            <TriangleAlert className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
            <p className="text-sm">
              این کلید فقط یک بار نمایش داده می‌شود. پس از بستن این پنجره دیگر قابل مشاهده
              نیست؛ آن را همین حالا کپی و در جای امنی نگه دارید.
            </p>
          </div>

          <div className="flex items-center gap-2">
            <Input
              readOnly
              dir="ltr"
              value={client?.secret ?? ""}
              aria-label="کلید دسترسی"
              data-testid="mcp-client-secret-input"
              className="font-mono text-start"
            />
            <Button
              variant="outline"
              size="icon"
              onClick={handleCopy}
              aria-label={copied ? "کپی شد" : "کپی کلید"}
              data-testid="mcp-client-secret-copy-button"
            >
              {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
            </Button>
          </div>
          {copied && <p className="text-sm text-muted-foreground">کلید کپی شد.</p>}
        </div>

        <DialogFooter>
          <Button onClick={onClose} data-testid="mcp-client-secret-close-button">
            متوجه شدم، بستن
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
