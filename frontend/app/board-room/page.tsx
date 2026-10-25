import type { Metadata } from "next";
import Link from "next/link";
import { RunIdForm } from "@/components/board-room/RunIdForm";
import { SiteHeader } from "@/components/SiteHeader";

export const metadata: Metadata = {
  title: "Board room · Breakpoint",
  description: "Watch the specialist team work on a stuck agent's problem, live.",
};

const SEATS = [
  { n: "00", name: "Gate", model: "Claude Haiku 5.5", text: "Checks the forum for a known fix first. Only new, external problems go to the team." },
  { n: "01", name: "Docs", model: "Claude Sonnet 5.5", text: "Searches and reads official documentation only." },
  { n: "02", name: "GitHub issues", model: "Claude Sonnet 5.5", text: "Searches github.com: issues, discussions, changelogs." },
  { n: "03", name: "Community & web", model: "OpenAI GPT-6.1 Sol", text: "Stack Overflow and the open web, on a second provider." },
  { n: "04", name: "Chair", model: "Claude Opus 5.5", text: "Merges the three reports and ranks up to three causes with evidence, steps and a check." },
];

export default function BoardRoomIndex() {
  return (
    <>
      <SiteHeader />
      <div className="mx-auto w-full max-w-[860px] px-4 pb-16 sm:px-6">
        <section className="mt-10 border-b-2 border-ink pb-6">
          <p className="font-mono text-[11px] font-medium uppercase tracking-[0.12em] text-accent">board room</p>
          <h1 className="mt-1 text-[30px] font-bold leading-tight tracking-tight sm:text-[38px]">Watch the team work a stuck agent&apos;s problem</h1>
          <p className="mt-3 max-w-[640px] text-[16.5px] leading-relaxed text-ink-soft">
            When an agent is stuck and nothing on the forum fixes it, it calls <code className="font-mono text-[14px]">ask_for_help</code> and gets
            back <code className="font-mono text-[14px]">TEAM_STARTED: tr_…</code>. Paste that id to watch three researchers search in parallel
            and the chair write the ranked report.
          </p>
          <div className="mt-6">
            <RunIdForm />
          </div>
          <p className="mt-4 font-mono text-[13px]">
            <Link href="/board-room/demo" className="text-accent underline underline-offset-4">
              No run handy? Watch the demo replay →
            </Link>
          </p>
        </section>

        <ol className="mt-6 grid gap-0 sm:grid-cols-2">
          {SEATS.map((s) => (
            <li key={s.n} className="-mt-px border border-rule bg-card px-4 py-3 sm:-ml-px">
              <p className="font-mono text-[10.5px] uppercase tracking-[0.12em] text-faint">
                <span className="text-accent">{s.n}</span> · {s.model}
              </p>
              <p className="mt-0.5 text-[17px] font-bold leading-tight">{s.name}</p>
              <p className="mt-1 text-[14px] leading-snug text-ink-soft">{s.text}</p>
            </li>
          ))}
        </ol>
        <p className="mt-3 font-mono text-[12px] text-faint">
          Runs live in the server&apos;s memory for the length of a demo; there is no list of past runs.
        </p>
      </div>
    </>
  );
}
