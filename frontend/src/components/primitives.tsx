import Link from "next/link";
import type { ReactNode } from "react";

export function Panel({
  title,
  action,
  children,
  className = "",
}: {
  title?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`rounded-lg border bg-panel ${className}`}>
      {(title || action) && (
        <header className="flex items-center justify-between gap-4 border-b px-4 py-2.5">
          <h2 className="text-sm font-semibold">{title}</h2>
          {action}
        </header>
      )}
      {children}
    </section>
  );
}

/**
 * One number and its label. `hint` carries the second-order detail — the denominator behind a
 * rate, the token count behind a cost — so the headline number can stay a single glanceable thing.
 */
export function Stat({
  label,
  value,
  hint,
  tone = "default",
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: "default" | "pass" | "fail" | "warn";
}) {
  const toneClass = { default: "", pass: "text-pass", fail: "text-fail", warn: "text-warn" }[tone];
  return (
    <div className="rounded-lg border bg-panel px-3.5 py-3">
      <div className="text-xs text-muted">{label}</div>
      <div className={`num mt-1 text-xl ${toneClass}`}>{value}</div>
      {hint && <div className="mt-0.5 text-xs text-faint">{hint}</div>}
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="px-4 py-10 text-center">
      <p className="text-sm font-medium">{title}</p>
      {children && <div className="mt-1 text-sm text-muted">{children}</div>}
    </div>
  );
}

export function ErrorState({ title, detail }: { title: string; detail?: string }) {
  return (
    <div className="rounded-lg border border-fail/40 bg-fail-bg px-4 py-3">
      <p className="text-sm font-medium text-fail">{title}</p>
      {detail && <p className="wrap-anywhere mt-1 font-mono text-xs text-fail/90">{detail}</p>}
    </div>
  );
}

export function Breadcrumbs({ items }: { items: { label: string; href?: string }[] }) {
  return (
    <nav className="flex items-center gap-1.5 text-sm text-muted">
      {items.map((item, i) => (
        <span key={i} className="flex items-center gap-1.5">
          {i > 0 && <span className="text-faint">/</span>}
          {item.href ? (
            <Link href={item.href} className="hover:text-text hover:underline">
              {item.label}
            </Link>
          ) : (
            <span className="text-text">{item.label}</span>
          )}
        </span>
      ))}
    </nav>
  );
}

export function Th({ children, className = "" }: { children?: ReactNode; className?: string }) {
  return (
    <th
      scope="col"
      className={`border-b px-3 py-2 text-left text-xs font-medium tracking-wide text-muted uppercase ${className}`}
    >
      {children}
    </th>
  );
}

export function Td({ children, className = "" }: { children?: ReactNode; className?: string }) {
  return <td className={`border-b px-3 py-2 align-top text-sm ${className}`}>{children}</td>;
}
