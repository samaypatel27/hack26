"use client";

import { useState } from "react";
import { modelName, pct } from "@/lib/board-room/derive";

export const label = "font-mono text-[10.5px] font-medium uppercase tracking-[0.12em] text-faint";
export const card = "min-w-0 rounded-lg border border-rule bg-card shadow-[0_1px_2px_rgba(42,33,24,0.04)]";

/** A new line in a feed: the site's highlighter wash (app/globals.css @keyframes wash). */
export const washIn = { animation: "wash 1.6s ease-out" } as const;
/** Fade up once, like the landing page (app/globals.css @keyframes landing-rise). */
export const riseIn = { animation: "landing-rise 0.5s cubic-bezier(0.2, 0.7, 0.2, 1) both" } as const;

export function Dot({ tone }: { tone: "live" | "done" | "idle" | "bad" }) {
  if (tone === "live") {
    return (
      <span className="relative inline-flex h-2 w-2 shrink-0">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-rescue opacity-60" />
        <span className="relative inline-flex h-2 w-2 rounded-full bg-rescue" />
      </span>
    );
  }
  const color = tone === "done" ? "bg-ink" : tone === "bad" ? "bg-accent" : "bg-rule-strong";
  return <span className={`inline-flex h-2 w-2 shrink-0 rounded-full ${color}`} />;
}

export function Pill({ tone, children }: { tone: "live" | "done" | "idle" | "bad"; children: React.ReactNode }) {
  const border = tone === "bad" ? "border-accent text-accent" : tone === "idle" ? "border-rule text-faint" : "border-rule-strong text-ink";
  return (
    <span className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-[2px] border bg-card px-1.5 py-px font-mono text-[11px] ${border}`}>
      <Dot tone={tone} />
      {children}
    </span>
  );
}

export function ModelTag({ id, className = "" }: { id: string; className?: string }) {
  const { name, note } = modelName(id);
  const provider = /^gpt-|^o\d/i.test(id) ? "OpenAI" : /^claude-/i.test(id) ? "Anthropic" : null;
  return (
    <span className={`inline-flex min-w-0 flex-wrap items-baseline gap-x-1.5 font-mono text-[11.5px] ${className}`} title={id}>
      <span className="font-semibold text-ink">{name || id}</span>
      {provider && <span className="text-faint">{provider}</span>}
      {note && <span className="text-accent">({note.length > 48 ? `${note.slice(0, 46)}…` : note})</span>}
    </span>
  );
}

export function ConfidenceBar({ value, bar = 0.4 }: { value: number; bar?: number }) {
  const strong = value >= bar;
  return (
    <span className="inline-flex items-center gap-2" title={`confidence ${pct(value)}; the bar for a report is ${pct(bar)}`}>
      <span className="relative h-1.5 w-28 overflow-hidden rounded-full bg-paper-deep sm:w-36">
        <span
          className={`absolute inset-y-0 left-0 rounded-full transition-[width] duration-700 ${strong ? "bg-rescue" : "bg-rule-strong"}`}
          style={{ width: pct(value) }}
        />
        <span className="absolute inset-y-0 w-px bg-ink/30" style={{ left: pct(bar) }} />
      </span>
      <span className={`font-mono text-[12px] font-semibold ${strong ? "text-ink" : "text-faint"}`}>{pct(value)}</span>
    </span>
  );
}

export function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="btn btn-sm shrink-0"
      onClick={() => {
        navigator.clipboard?.writeText(text).then(
          () => {
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          },
          () => undefined,
        );
      }}
    >
      {copied ? "copied" : "copy"}
    </button>
  );
}

export function ExternalLink({ href, children, className = "", title }: { href: string; children: React.ReactNode; className?: string; title?: string }) {
  const safe = /^https?:\/\//i.test(href);
  if (!safe) return <span className={className}>{children}</span>;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" className={className} title={title ?? href}>
      {children}
    </a>
  );
}
