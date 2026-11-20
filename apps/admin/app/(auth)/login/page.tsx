"use client";

/**
 * Login — the operator pastes their API key (`hya_live_...`). The key is
 * POSTed once to the BFF, validated against the backend, and kept
 * server-side in the session; the browser never holds it after login.
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import { Eye, EyeOff, KeyRound } from "lucide-react";
import { Button, Card, CardContent, CardDescription, CardHeader, CardTitle, Input, Label } from "@/components/ui";

function errorMessage(status: number, code: string): string {
  if (status === 401 || code === "UNAUTHORIZED") return "این کلید معتبر نیست. کلید را بررسی و دوباره تلاش کنید.";
  if (status === 502 || code === "BACKEND_UNREACHABLE") return "سرور حسابیار در دسترس نیست. چند لحظه بعد دوباره تلاش کنید.";
  if (status === 400) return "کلید باید با hya_live_ شروع شود.";
  return "ورود انجام نشد. دوباره تلاش کنید.";
}

export default function LoginPage() {
  const router = useRouter();
  const [apiKey, setApiKey] = React.useState("");
  const [showKey, setShowKey] = React.useState(false);
  const [pending, setPending] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const key = apiKey.trim();
    if (!key) {
      setError("کلید دسترسی را وارد کنید.");
      return;
    }
    setPending(true);
    setError(null);
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ apiKey: key }),
      });
      const data = (await res.json().catch(() => null)) as { code?: string } | null;
      if (!res.ok) {
        setError(errorMessage(res.status, data?.code ?? ""));
        return;
      }
      // The key is now server-side only; forget it in the browser.
      setApiKey("");
      router.push("/");
      router.refresh();
    } catch {
      setError("ارتباط با سرور برقرار نشد. دوباره تلاش کنید.");
    } finally {
      setPending(false);
    }
  }

  return (
    <Card>
      <CardHeader className="text-center">
        <div className="mx-auto mb-2 flex size-11 items-center justify-center rounded-full bg-primary text-primary-foreground">
          <KeyRound className="size-5" aria-hidden />
        </div>
        <CardTitle className="text-xl">ورود به حسابیار</CardTitle>
        <CardDescription>کلید دسترسی اپراتور را وارد کنید تا وارد پنل مدیریت شوید.</CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} className="space-y-4" noValidate>
          <div className="space-y-2">
            <Label htmlFor="api-key">کلید دسترسی</Label>
            <div className="relative">
              <Input
                id="api-key"
                type={showKey ? "text" : "password"}
                dir="ltr"
                lang="en"
                autoComplete="off"
                autoCapitalize="off"
                autoCorrect="off"
                spellCheck={false}
                placeholder="hya_live_…"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                disabled={pending}
                aria-invalid={error ? true : undefined}
                aria-describedby={error ? "login-error" : undefined}
                className="pe-10 text-start"
              />
              <button
                type="button"
                onClick={() => setShowKey((s) => !s)}
                disabled={pending}
                aria-label={showKey ? "پنهان کردن کلید" : "نمایش کلید"}
                className="absolute inset-y-0 end-0 flex w-10 items-center justify-center text-muted-foreground hover:text-foreground disabled:opacity-50"
              >
                {showKey ? <EyeOff className="size-4" aria-hidden /> : <Eye className="size-4" aria-hidden />}
              </button>
            </div>
          </div>
          {error ? (
            <p id="login-error" role="alert" className="text-sm text-destructive">
              {error}
            </p>
          ) : null}
          <Button type="submit" className="w-full" disabled={pending}>
            {pending ? "در حال بررسی…" : "ورود"}
          </Button>
          <p className="text-center text-xs text-muted-foreground">
            کلید فقط روی سرور نگه‌داری می‌شود و در مرورگر ذخیره نمی‌شود.
          </p>
        </form>
      </CardContent>
    </Card>
  );
}
