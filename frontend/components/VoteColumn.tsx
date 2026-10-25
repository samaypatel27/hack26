"use client";

import { useState } from "react";
import { ApiError, vote } from "@/lib/api";
import type { VoteTarget } from "@/lib/types";
import { readVote, saveVote } from "@/lib/votes";

function Arrow({ up, filled }: { up: boolean; filled: boolean }) {
  return (
    <svg width="22" height="14" viewBox="0 0 22 14" aria-hidden="true">
      <path
        d={up ? "M11 1.5 20.5 12.5h-19z" : "M11 12.5 1.5 1.5h19z"}
        fill={filled ? "var(--accent)" : "none"}
        stroke={filled ? "var(--accent)" : "currentColor"}
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** Up arrow, count, down arrow. One vote per target per browser; the server rate-limits by IP. */
export function VoteColumn({ postId, target, count }: { postId: number; target: VoteTarget; count: number }) {
  // Only ever rendered on the client (after the post loads), so reading storage here is safe.
  const [voted, setVoted] = useState<1 | -1 | 0>(() => readVote(postId, target));
  // The count we showed after voting, valid until the next poll brings a different number.
  const [local, setLocal] = useState<{ base: number; value: number } | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const shown = local && local.base === count ? local.value : count;

  async function cast(direction: 1 | -1) {
    if (voted) return;
    setVoted(direction);
    setLocal({ base: count, value: count + direction });
    setNote(null);
    try {
      const res = await vote(postId, target, direction);
      saveVote(postId, target, direction);
      setLocal({ base: count, value: target === "post" ? res.post_votes : res.solution_votes });
    } catch (e) {
      setVoted(0);
      setLocal(null);
      setNote(e instanceof ApiError && e.status === 429 ? "Easy. Try again in a minute." : "Didn't go through.");
    }
  }

  const label = target === "post" ? "question" : "solution";

  return (
    <div className="flex w-12 flex-col items-center gap-1 pt-1 text-ink-soft">
      <button
        type="button"
        onClick={() => cast(1)}
        disabled={voted !== 0}
        aria-pressed={voted === 1}
        aria-label={`Vote this ${label} up`}
        title={voted ? "You already voted" : "Useful"}
        className="rounded-[2px] p-1 enabled:hover:bg-paper-deep enabled:hover:text-ink disabled:cursor-default disabled:opacity-60 aria-pressed:opacity-100"
      >
        <Arrow up filled={voted === 1} />
      </button>
      <span className="text-[22px] font-bold tabular-nums leading-none text-ink" aria-live="polite">
        {shown}
      </span>
      <button
        type="button"
        onClick={() => cast(-1)}
        disabled={voted !== 0}
        aria-pressed={voted === -1}
        aria-label={`Vote this ${label} down`}
        title={voted ? "You already voted" : "Not useful"}
        className="rounded-[2px] p-1 enabled:hover:bg-paper-deep enabled:hover:text-ink disabled:cursor-default disabled:opacity-60 aria-pressed:opacity-100"
      >
        <Arrow up={false} filled={voted === -1} />
      </button>
      {note && <p className="w-24 text-center font-mono text-[10.5px] leading-tight text-accent">{note}</p>}
    </div>
  );
}
