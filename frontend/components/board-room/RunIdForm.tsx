"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { cleanRunId } from "@/lib/board-room/api";

/** Paste a run id (or the whole "TEAM_STARTED: tr_..." line) and open its board. */
export function RunIdForm({ initial = "" }: { initial?: string }) {
  const router = useRouter();
  const [value, setValue] = useState(initial);
  const id = cleanRunId(value);

  return (
    <form
      className="flex flex-col gap-2 sm:flex-row"
      onSubmit={(e) => {
        e.preventDefault();
        if (id) router.push(`/board-room/${encodeURIComponent(id)}`);
      }}
    >
      <label htmlFor="board-room-run-id" className="sr-only">
        Team run id
      </label>
      <input
        id="board-room-run-id"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="tr_3f9a1c2b7e4d  or paste the TEAM_STARTED line"
        autoComplete="off"
        spellCheck={false}
        className="min-w-0 flex-1 rounded-[2px] border border-rule-strong bg-card px-3 py-2.5 font-mono text-[14px] text-ink placeholder:text-faint focus:border-ink focus:outline-none"
      />
      <button type="submit" className="btn btn-primary" disabled={!id}>
        Open board <span className="btn-arrow">→</span>
      </button>
    </form>
  );
}
