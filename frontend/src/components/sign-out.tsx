"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

export function SignOut() {
  const router = useRouter();
  const [pending, setPending] = useState(false);

  return (
    <button
      type="button"
      disabled={pending}
      onClick={async () => {
        setPending(true);
        await fetch("/api/auth/logout", { method: "POST" });
        // Refresh first so the router cache does not hand back a signed-in render of "/".
        router.refresh();
        router.push("/login");
      }}
      className="text-xs text-muted hover:text-text hover:underline disabled:opacity-50"
    >
      {pending ? "Signing out…" : "Sign out"}
    </button>
  );
}
