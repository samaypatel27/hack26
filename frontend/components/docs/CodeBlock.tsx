"use client";

import { useState } from "react";

/** A code block with a copy button. Clipboard access can be refused, in which case nothing happens. */
export function CodeBlock({ code, label }: { code: string; label?: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* the text is still selectable */
    }
  }

  return (
    <div className="overflow-hidden rounded-[2px] border border-rule border-l-[3px] border-l-rule-strong bg-[#dfeaec]">
      <div className="flex items-center justify-between border-b border-rule px-3 py-1 font-mono text-[11px] text-faint">
        <span>{label ?? ""}</span>
        <button
          type="button"
          onClick={copy}
          className="text-accent hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink"
          aria-label={copied ? "Copied" : `Copy ${label ?? "code"}`}
        >
          {copied ? "copied" : "copy"}
        </button>
      </div>
      <pre className="overflow-x-auto px-3 py-2.5 font-mono text-[13px] leading-relaxed text-ink">
        <code className="!border-0 !bg-transparent !p-0 !text-[13px]">{code}</code>
      </pre>
    </div>
  );
}
