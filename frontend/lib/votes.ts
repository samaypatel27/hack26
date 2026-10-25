import type { VoteTarget } from "./types";

/** One vote per target per browser. Storage can be unavailable (private mode), so never throw. */
const key = (id: number, target: VoteTarget) => `vote:${id}:${target}`;

export function readVote(id: number, target: VoteTarget): 1 | -1 | 0 {
  try {
    const v = window.localStorage.getItem(key(id, target));
    return v === "1" ? 1 : v === "-1" ? -1 : 0;
  } catch {
    return 0;
  }
}

export function saveVote(id: number, target: VoteTarget, direction: 1 | -1): void {
  try {
    window.localStorage.setItem(key(id, target), String(direction));
  } catch {
    // Not remembered; the server's rate limit still applies.
  }
}
