import type { ReactNode } from "react";

/** Built things get a solid outline; anything the code only sketches gets a dashed one. */
export function Status({ s }: { s: "built" | "planned" }) {
  return (
    <span
      className={`inline-block rounded-[2px] border px-1.5 py-px align-middle font-mono text-[10.5px] font-medium uppercase tracking-[0.1em] ${
        s === "built" ? "border-ink text-ink" : "border-dashed border-faint text-faint"
      }`}
    >
      {s}
    </span>
  );
}

export function Section({ id, n, title, children }: { id: string; n: string; title: string; children: ReactNode }) {
  return (
    <section id={id} className="scroll-mt-6 pt-12">
      <h2 className="flex items-baseline gap-3 border-b-2 border-ink pb-1.5 text-[26px] font-bold leading-tight tracking-tight">
        <span className="font-mono text-[13px] font-medium text-accent">{n}</span>
        {title}
      </h2>
      <div className="mt-5 space-y-4 text-[16.5px] leading-[1.65] [&_code]:rounded-[2px] [&_code]:border [&_code]:border-rule [&_code]:bg-paper-deep [&_code]:px-1 [&_code]:font-mono [&_code]:text-[0.84em]">
        {children}
      </div>
    </section>
  );
}

export function Caption({ children }: { children: ReactNode }) {
  return <p className="mt-2 font-mono text-[12px] leading-snug text-faint">{children}</p>;
}

/** A diagram sits in a bordered frame, like a figure in a paper. */
export function Figure({ children, caption }: { children: ReactNode; caption?: ReactNode }) {
  return (
    <figure className="my-6">
      <div className="rounded-[2px] border border-rule-strong bg-card p-3 sm:p-5">{children}</div>
      {caption && <figcaption className="mt-2 font-mono text-[12px] leading-snug text-faint">{caption}</figcaption>}
    </figure>
  );
}

export function Legend() {
  return (
    <p className="flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[12px] text-faint">
      <span className="inline-flex items-center gap-1.5">
        <span className="inline-block h-0 w-6 border-t-[1.5px] border-ink" /> built
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span className="inline-block h-0 w-6 border-t-[1.5px] border-dashed border-faint" /> planned, not working yet
      </span>
    </p>
  );
}
