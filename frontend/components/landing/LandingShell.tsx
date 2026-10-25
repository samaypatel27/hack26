"use client";

import { useRouter } from "next/navigation";
import { useEffect, useLayoutEffect, useRef, type MouseEvent, type ReactNode } from "react";

// Where each page sends the landing page: the forum pulls it left, the docs push it right.
const EXITS: Record<string, "left" | "right"> = { "/forums": "left", "/docs": "right" };
const EXIT_MS = 620;

// The hero's fade-in only plays when the browser loaded the landing page directly (first visit
// or reload). Arriving from /forums or /docs via Home or Back shows the page straight away.
let leftLanding = false; // set when the visitor clicks through to /forums or /docs

/** True if this document was loaded at a different URL, i.e. we got here by client navigation. */
function arrivedFromAnotherPage(): boolean {
  const entry = performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming | undefined;
  return entry !== undefined && new URL(entry.name).pathname !== window.location.pathname;
}

/**
 * Wraps the landing page. A plain click on any link to /forums or /docs (the header tabs or the
 * hero buttons) first slides everything off to one side, then navigates. Modified clicks
 * (new tab, etc.) and reduced motion go straight through.
 *
 * The leaving state lives on the DOM node rather than in React state: with cacheComponents the
 * page is kept alive in the background, and React state would still say "leaving" when the visitor
 * comes back, replaying the slide-off. Layout effects re-run each time the page is shown, so that
 * is where the state is reset.
 */
export function LandingShell({ children }: { children: ReactNode }) {
  const router = useRouter();
  const root = useRef<HTMLDivElement>(null);

  // Layout effect so a revisit never paints a frame of the old state.
  useLayoutEffect(() => {
    const el = root.current;
    if (!el) return;
    delete el.dataset.leaving;
    el.removeAttribute("aria-busy");
    if (leftLanding || arrivedFromAnotherPage()) el.dataset.intro = "skip";
  }, []);

  useEffect(() => {
    for (const href of Object.keys(EXITS)) router.prefetch(href);
  }, [router]);

  function onClickCapture(e: MouseEvent) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const link = (e.target as Element).closest("a");
    const href = link?.getAttribute("href");
    if (!href || !(href in EXITS)) return;
    leftLanding = true;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    e.preventDefault();
    e.stopPropagation();
    const el = root.current;
    if (!el || el.dataset.leaving) return;
    el.dataset.leaving = EXITS[href];
    el.setAttribute("aria-busy", "true");
    setTimeout(() => router.push(href), EXIT_MS);
  }

  return (
    <div ref={root} onClickCapture={onClickCapture}>
      {children}
    </div>
  );
}
