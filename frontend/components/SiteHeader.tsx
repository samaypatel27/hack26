import Link from "next/link";
import type { ConnectionState } from "@/lib/types";

const TABS = [
  { id: "home", href: "/", label: "Home" },
  { id: "forums", href: "/forums", label: "Forums" },
  { id: "docs", href: "/docs", label: "Docs" },
] as const;

/**
 * Wordmark and page tabs. A note appears on the right only when the server can't be reached.
 * `active` says which tab to light; a post page counts as part of the forums.
 */
export function SiteHeader({
  active,
  connection,
}: {
  active?: (typeof TABS)[number]["id"];
  connection?: ConnectionState;
}) {
  return (
    <header className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-2 border-b-2 border-ink pb-2 pt-6">
      <div className="flex flex-wrap items-baseline gap-x-7 gap-y-1">
        <Link href="/" className="text-[23px] font-bold leading-none tracking-tight">
          Breakpoint
        </Link>
        <nav aria-label="Pages" className="flex gap-5 text-[15px]">
          {TABS.map((t) => {
            const on = active === t.id;
            return (
              <Link
                key={t.id}
                href={t.href}
                aria-current={on ? "page" : undefined}
                className={`underline-offset-[7px] ${
                  on ? "font-semibold text-accent underline decoration-2" : "text-ink-soft hover:text-ink"
                }`}
              >
                {t.label}
              </Link>
            );
          })}
        </nav>
      </div>
      <span className="font-mono text-xs text-accent" role="status" aria-live="polite">
        {connection === "offline" ? "can't reach the server, retrying" : ""}
      </span>
    </header>
  );
}
