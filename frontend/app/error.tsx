"use client"; // Error boundaries must be Client Components.

import Link from "next/link";
import { useEffect } from "react";
import { SiteHeader } from "@/components/SiteHeader";

/**
 * Shown in place of any page whose components throw while rendering, so one broken
 * component gives a short message instead of a blank page. `retry` re-renders the page.
 */
export default function ErrorPage({ error, retry }: { error: unknown; retry: () => void }) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <>
      <SiteHeader />
      <main className="mx-auto w-full max-w-[860px] px-4 pb-16 sm:px-6">
        <section role="alert" className="mt-10 border-y border-rule py-12 text-center">
          <h1 className="text-[22px] font-bold tracking-tight">Something went wrong on this page.</h1>
          <p className="mt-2 italic text-ink-soft">The rest of the site still works. Try again, or go back to the forums.</p>
          <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
            <button type="button" className="btn" onClick={() => retry()}>Try again</button>
            <Link href="/forums" className="btn">All forums</Link>
          </div>
        </section>
      </main>
    </>
  );
}
