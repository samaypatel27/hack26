"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState, type MouseEvent, type ReactNode } from "react";

// Where each page sends the landing page: the forum pulls it left, the docs push it right.
const EXITS: Record<string, "left" | "right"> = { "/forums": "left", "/docs": "right" };
const EXIT_MS = 620;

/**
 * Wraps the landing page. A plain click on any link to /forums or /docs (the header tabs or the
 * hero buttons) first slides everything off to one side, then navigates. Modified clicks
 * (new tab, etc.) and reduced motion go straight through.
 */
export function LandingShell({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [leaving, setLeaving] = useState<"left" | "right" | null>(null);

  useEffect(() => {
    for (const href of Object.keys(EXITS)) router.prefetch(href);
  }, [router]);

  function onClickCapture(e: MouseEvent) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const link = (e.target as Element).closest("a");
    const href = link?.getAttribute("href");
    if (!href || !(href in EXITS)) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    e.preventDefault();
    e.stopPropagation();
    if (leaving) return;
    setLeaving(EXITS[href]);
    setTimeout(() => router.push(href), EXIT_MS);
  }

  return (
    <div onClickCapture={onClickCapture} data-leaving={leaving ?? undefined} aria-busy={leaving !== null}>
      {children}
    </div>
  );
}
