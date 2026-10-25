import Link from "next/link";
import { SiteHeader } from "@/components/SiteHeader";

/** Shown for any URL that matches no page. */
export default function NotFound() {
  return (
    <>
      <SiteHeader />
      <main className="mx-auto w-full max-w-[860px] px-4 pb-16 sm:px-6">
        <section className="mt-10 border-y border-rule py-12 text-center">
          <h1 className="text-[22px] font-bold tracking-tight">No page here.</h1>
          <p className="mt-2 italic text-ink-soft">The link may be wrong, or the page may have moved.</p>
          <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
            <Link href="/" className="btn">Home</Link>
            <Link href="/forums" className="btn">All forums</Link>
          </div>
        </section>
      </main>
    </>
  );
}
