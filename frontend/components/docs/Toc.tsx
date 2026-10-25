"use client";

import { useEffect, useRef, useState } from "react";

export type TocItem = { id: string; n: string; title: string };

/**
 * The section list, with the current section marked as you scroll. Two forms of the same list:
 * `side` is sticky in its own column on laptops; `inline` is a collapsed list for phones.
 * Links scroll smoothly (see scroll-behavior in globals.css).
 */
export function Toc({ items, variant }: { items: TocItem[]; variant: "side" | "inline" }) {
  const [current, setCurrent] = useState(items[0]?.id);
  const details = useRef<HTMLDetailsElement>(null);

  useEffect(() => {
    const seen = new Set<string>();
    const obs = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) seen.add(e.target.id);
          else seen.delete(e.target.id);
        }
        const first = items.find((i) => seen.has(i.id));
        if (first) setCurrent(first.id);
      },
      { rootMargin: "0px 0px -65% 0px" },
    );
    for (const i of items) {
      const el = document.getElementById(i.id);
      if (el) obs.observe(el);
    }
    return () => obs.disconnect();
  }, [items]);

  const list = (
    <ol className="space-y-1.5 text-[14.5px]">
      {items.map((i) => (
        <li key={i.id}>
          <a
            href={`#${i.id}`}
            onClick={() => {
              setCurrent(i.id);
              if (details.current) details.current.open = false;
            }}
            aria-current={current === i.id ? "location" : undefined}
            className={`flex gap-2 leading-snug ${
              current === i.id ? "font-semibold text-accent" : "text-ink-soft hover:text-ink"
            }`}
          >
            <span className="w-5 shrink-0 font-mono text-[11.5px] leading-[1.6] text-faint">{i.n}</span>
            {i.title}
          </a>
        </li>
      ))}
    </ol>
  );

  if (variant === "inline") {
    return (
      <details ref={details} className="mt-6 border border-rule-strong bg-card px-3 py-2 lg:hidden">
        <summary className="cursor-pointer font-mono text-[12px] uppercase tracking-[0.14em] text-faint">On this page</summary>
        <div className="mt-3 pb-1">{list}</div>
      </details>
    );
  }

  return (
    <nav aria-label="On this page" className="sticky top-8 hidden pt-10 lg:block">
      <p className="mb-3 font-mono text-[11px] uppercase tracking-[0.14em] text-faint">On this page</p>
      {list}
    </nav>
  );
}
