"use client";

/** Logout button: destroys the server session, then returns to /login. */

import * as React from "react";
import { useRouter } from "next/navigation";
import { LogOut } from "lucide-react";
import { Button } from "@/components/ui";

export function LogoutButton() {
  const router = useRouter();
  const [pending, setPending] = React.useState(false);

  async function logout() {
    setPending(true);
    try {
      await fetch("/api/auth/logout", { method: "POST" });
    } finally {
      router.push("/login");
      router.refresh();
    }
  }

  return (
    <Button variant="ghost" size="sm" onClick={logout} disabled={pending}>
      <LogOut className="size-4" aria-hidden />
      خروج
    </Button>
  );
}
