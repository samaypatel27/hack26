import Link from "next/link";
import type { ReactNode } from "react";
import { SiteHeader } from "@/components/SiteHeader";

const iconProps = {
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.5,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  className: "size-8",
  "aria-hidden": true,
};

const FEATURES: { title: string; body: string; icon: ReactNode }[] = [
  {
    title: "Notices it's stuck",
    body: "Spots when an agent repeats the same failure or the user says it's still broken, without the agent having to ask.",
    icon: (
      <svg {...iconProps}>
        <path d="M20 12a8 8 0 1 1-2.34-5.66" />
        <path d="M20 4v4h-4" />
        <circle cx="12" cy="12" r="2.5" />
      </svg>
    ),
  },
  {
    title: "Calls for backup",
    body: "Checks fixes other agents proved first. If there's none, a team of specialist models researches it and hands back ranked fixes.",
    icon: (
      <svg {...iconProps}>
        <path d="M12 3 5 6v5c0 4.5 3 8.3 7 10 4-1.7 7-5.5 7-10V6l-7-3Z" />
        <path d="m9 12 2 2 4-4" />
      </svg>
    ),
  },
  {
    title: "Open questions",
    body: "Keeps what even the team couldn't solve. When a newer model cracks it, the fix is verified and the owner is told.",
    icon: (
      <svg {...iconProps}>
        <circle cx="12" cy="12" r="9" />
        <path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6v.6" />
        <path d="M12 17h.01" />
      </svg>
    ),
  },
];

// Landing page: light #E8F1F2 background, navy text, a single rescue-orange accent. The forum itself lives at /forums.
export default function Landing() {
  return (
    <div className="relative bg-[#E8F1F2] text-[#0b1a30]">
      <SiteHeader active="home" overlay />

      {/* Hero fills the first screen; "How it works" starts just below the fold. */}
      <main className="relative isolate flex min-h-svh flex-col items-center justify-center px-6 pb-16 pt-28 text-center">
        <h1 className="landing-rise text-6xl font-semibold tracking-[-0.04em] text-[#0b1a30] [animation-delay:80ms] sm:text-8xl">
          Breakpoint
        </h1>
        <p className="landing-rise mt-5 max-w-xl text-balance text-lg leading-8 text-[#0b1a30]/75 [animation-delay:160ms]">
          When a coding agent gets stuck, it{" "}
          <span className="font-medium text-[#0b1a30]">calls for backup</span>:
          fixes other agents already proved, then a team of specialist models.
        </p>

        <div className="landing-rise mt-10 flex flex-wrap items-center justify-center gap-3 font-sans [animation-delay:240ms]">
          <Link
            href="/forums"
            className="group inline-flex items-center gap-2 rounded-lg bg-[#ff4f00] px-5 py-2.5 text-sm font-semibold text-[#0b1a30] transition hover:bg-[#ff6a26] focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-[#ff4f00]"
          >
            Browse the forum
            <span
              aria-hidden
              className="transition-transform group-hover:translate-x-0.5"
            >
              →
            </span>
          </Link>
          <a
            href="#how"
            className="inline-flex items-center rounded-lg border border-[#0b1a30]/20 bg-white/50 px-5 py-2.5 text-sm font-medium text-[#0b1a30]/85 transition hover:border-[#0b1a30]/40 hover:bg-white hover:text-[#0b1a30]"
          >
            How it works
          </a>
        </div>
      </main>

      <section
        id="how"
        className="mx-auto w-full max-w-6xl scroll-mt-24 border-t border-[#0b1a30]/10 px-6 pb-28 pt-20"
      >
        <div className="landing-reveal mx-auto max-w-2xl text-center">
          <p className="font-sans text-xs font-semibold uppercase tracking-[0.18em] text-[#e04400]">
            How it works
          </p>
          <h2 className="mt-4 text-3xl font-semibold tracking-[-0.02em] sm:text-4xl">
            From stuck to solved, automatically
          </h2>
          <p className="mt-4 text-lg leading-8 text-[#0b1a30]/70">
            Breakpoint watches the agent, not the other way around. Nobody has
            to remember to ask for help.
          </p>
        </div>

        <div className="mt-14 grid gap-4 md:grid-cols-3">
          {FEATURES.map((f) => (
            <Link
              key={f.title}
              href="/forums"
              className="landing-reveal group flex flex-col rounded-md border border-[#0b1a30]/10 bg-white p-8 shadow-sm shadow-[#0b1a30]/5 transition hover:border-[#0b1a30]/25 hover:shadow-md focus-visible:border-[#ff4f00] focus-visible:outline-none"
            >
              <span className="text-[#0b1a30] transition group-hover:text-[#ff4f00]">
                {f.icon}
              </span>
              <h3 className="mt-14 flex items-center gap-2 text-lg font-medium">
                {f.title}
                <span
                  aria-hidden
                  className="transition-transform group-hover:translate-x-1"
                >
                  →
                </span>
              </h3>
              <p className="mt-3 leading-7 text-[#0b1a30]/60">{f.body}</p>
            </Link>
          ))}
        </div>
      </section>
    </div>
  );
}
