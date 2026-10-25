import type { HelpResponse } from "./types";

// Same base URL as frontend/lib/api.ts (kept separate so this feature can be deleted on its own).
const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export class HelpApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

/** Run ids look like tr_ followed by 12 hex characters; be a little lenient. */
export function cleanRunId(raw: string): string {
  const text = raw.trim();
  const found = text.match(/tr_[A-Za-z0-9]+/);
  return found ? found[0] : text.replace(/^TEAM_STARTED:\s*/i, "").split(/\s+/)[0] ?? "";
}

export async function fetchHelp(runId: string, since: number, signal?: AbortSignal): Promise<HelpResponse> {
  const params = since > 0 ? `?since=${since}` : "";
  const res = await fetch(`${API_URL}/api/help/${encodeURIComponent(runId)}${params}`, { cache: "no-store", signal });
  if (!res.ok) throw new HelpApiError(res.status, `Request failed (${res.status})`);
  return res.json() as Promise<HelpResponse>;
}
