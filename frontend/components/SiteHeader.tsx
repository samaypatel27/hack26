import Link from "next/link";
import type { ConnectionState } from "@/lib/types";

const TABS = [
  { id: "home", href: "/", label: "Home" },
  { id: "forums", href: "/forums", label: "Forums" },
  { id: "docs", href: "/docs", label: "Docs" },
] as const;

/**
 * The one top bar for every page: wordmark at the left edge, page tabs at the right.
 * Render it outside a page's centred column, as the first thing on the page.
 * `active` says which tab to light; a post page counts as part of the forums.
 * `overlay` lays the bar over the top of a full-screen hero instead of above it.
 * A note appears beside the tabs only when the server can't be reached.
 */
export function SiteHeader({
  active,
  connection,
  overlay = false,
}: {
  active?: (typeof TABS)[number]["id"];
  connection?: ConnectionState;
  overlay?: boolean;
}) {
  return (
    <header className={`z-10 border-b border-ink/10 ${overlay ? "absolute inset-x-0 top-0" : "relative"}`}>
      <div className="flex w-full items-center justify-between gap-x-6 gap-y-2 px-6 py-4 sm:px-10">
        <Link href="/" className="text-xl font-bold tracking-tight">
          Breakpoint
        </Link>
        <div className="flex items-center gap-x-6">
          <span className="hidden font-mono text-xs text-accent sm:inline" role="status" aria-live="polite">
            {connection === "offline" ? "can't reach the server, retrying" : ""}
          </span>
          <nav aria-label="Pages" className="flex items-center gap-5 text-[15px] sm:gap-7">
            {TABS.map((t) => {
              const on = active === t.id;
              return (
                <Link
                  key={t.id}
                  href={t.href}
                  aria-current={on ? "page" : undefined}
                  className={`underline-offset-[7px] transition ${
                    on ? "font-semibold text-accent underline decoration-2" : "text-ink-soft hover:text-ink"
                  }`}
                >
                  {t.label}
                </Link>
              );
            })}
          </nav>
        </div>
      </div>
    </header>
  );
}
