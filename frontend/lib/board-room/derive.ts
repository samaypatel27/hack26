import type { BoardState, Finding, Seat, TeamEvent } from "./types";

export const SEATS: Seat[] = ["docs", "issues", "community"];

export const SEAT_INFO: Record<Seat, { title: string; scope: string; defaultModel: string }> = {
  docs: { title: "Docs", scope: "Official documentation only", defaultModel: "claude-sonnet-5-5" },
  issues: { title: "GitHub issues", scope: "github.com only", defaultModel: "claude-sonnet-5-5" },
  community: { title: "Community & web", scope: "Stack Overflow and the open web", defaultModel: "gpt-6.1-sol" },
};

export const GATE_MODEL = "claude-haiku-5-5";
export const CHAIR_MODEL = "claude-opus-5-5";

export const CATEGORY_LABEL: Record<string, string> = {
  dependencies_install: "Dependencies & install",
  build_config: "Build & config",
  environment_platform: "Environment & platform",
  runtime_api: "Runtime & API",
  network_auth_permissions: "Network, auth & permissions",
  performance_memory: "Performance & memory",
};

/** "claude-sonnet-5-5" -> "Claude Sonnet 5.5", "gpt-6.1-sol" -> "GPT-6.1 Sol". Keeps a "(fallback: ...)" note apart. */
export function modelName(id: string | null | undefined): { name: string; note: string | null } {
  if (!id) return { name: "", note: null };
  const m = id.match(/^(\S+)\s*(?:\((.*)\))?\s*$/);
  const base = m?.[1] ?? id;
  const note = m?.[2] ?? null;
  let name = base;
  const claude = base.match(/^claude-([a-z]+)-(\d+)(?:-(\d+))?(?:-\d{8})?$/i);
  if (claude) {
    const family = claude[1][0].toUpperCase() + claude[1].slice(1);
    name = `Claude ${family} ${claude[2]}${claude[3] ? `.${claude[3]}` : ""}`;
  } else if (/^gpt-/i.test(base)) {
    const [, ...rest] = base.split("-");
    const version = rest.shift() ?? "";
    name = `GPT-${version}${rest.length ? " " + rest.map((w) => w[0].toUpperCase() + w.slice(1)).join(" ") : ""}`;
  } else if (base === "none") {
    name = id;
  }
  return { name, note };
}

export type FeedItem = { id: number; kind: "search" | "read" | "switch" | "error" | "note"; text: string; url?: string };
export type SourceChip = { url: string; title?: string; host: string };

export type SeatView = {
  seat: Seat;
  model: string;
  phase: "waiting" | "working" | "finished" | "failed" | "timeout" | "skipped";
  feed: FeedItem[];
  sources: SourceChip[];
  searches: number;
  pagesRead: number;
  findings: Finding[];
  category: string | null;
  elapsedS: number | null;
  costUsd: number | null;
  error: string | null;
  switchedTo: string | null;
};

export type ChairView = {
  model: string;
  phase: "waiting" | "thinking" | "done" | "failed" | "skipped";
  elapsedS: number | null;
  costUsd: number | null;
  error: string | null;
  text: string | null;
};

export function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url.slice(0, 40);
  }
}

/** A short chip label: host plus the last part of the path, e.g. "github.com/issues/1642". */
export function chipLabel(url: string): string {
  try {
    const u = new URL(url);
    const host = u.hostname.replace(/^www\./, "");
    const parts = u.pathname.split("/").filter(Boolean);
    if (!parts.length) return host;
    const last = parts[parts.length - 1];
    const tail = /^\d+$/.test(last) && parts.length > 1 ? `${parts[parts.length - 2]}/${last}` : last;
    return `${host}/${tail.length > 28 ? `${tail.slice(0, 27)}…` : tail}`;
  } catch {
    return url.slice(0, 40);
  }
}

export function shortUrl(url: string): string {
  try {
    const u = new URL(url);
    const path = (u.pathname + u.hash).replace(/\/$/, "");
    const host = u.hostname.replace(/^www\./, "");
    const tail = path.length > 42 ? `${path.slice(0, 20)}…${path.slice(-18)}` : path;
    return host + tail;
  } catch {
    return url.length > 60 ? `${url.slice(0, 57)}…` : url;
  }
}

const str = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

export function deriveSeat(seat: Seat, events: TeamEvent[], state: Pick<BoardState, "report">): SeatView {
  const view: SeatView = {
    seat,
    model: state.report?.models?.[seat] ?? "",
    phase: "waiting",
    feed: [],
    sources: [],
    searches: 0,
    pagesRead: 0,
    findings: [],
    category: null,
    elapsedS: null,
    costUsd: null,
    error: null,
    switchedTo: null,
  };
  const seenSources = new Set<string>();
  const runnerStart = events.find((e) => e.member === "runner" && e.type === "started");
  const seats = runnerStart?.data?.models as Record<string, string> | undefined;
  if (!view.model && seats?.[seat]) view.model = seats[seat];

  for (const e of events) {
    if (e.member !== seat) continue;
    const d = e.data ?? {};
    switch (e.type) {
      case "started":
        view.phase = "working";
        if (!state.report?.models?.[seat] && str(d.model)) view.model = str(d.model)!;
        break;
      case "searching": {
        view.searches += 1;
        const queries = Array.isArray(d.queries) ? (d.queries as unknown[]).filter((q) => typeof q === "string") : [];
        view.feed.push({ id: e.id, kind: "search", text: (queries.length ? queries.join("; ") : e.text) || "(search)" });
        break;
      }
      case "fetching": {
        const url = str(d.url) ?? e.text;
        view.pagesRead += 1;
        view.feed.push({ id: e.id, kind: "read", text: shortUrl(url), url });
        break;
      }
      case "source": {
        const url = str(d.url);
        const key = url ? url.split("#")[0].replace(/\/$/, "") : "";
        if (url && !seenSources.has(key)) {
          seenSources.add(key);
          const title = str(d.title) ?? undefined;
          view.sources.push({ url, title, host: hostOf(url) });
        }
        break;
      }
      case "fallback":
        view.switchedTo = str(d.model);
        view.feed.push({ id: e.id, kind: "switch", text: e.text });
        if (!state.report?.models?.[seat] && str(d.model)) view.model = `${str(d.model)} (fallback)`;
        break;
      case "tool_error":
        view.feed.push({ id: e.id, kind: "error", text: e.text });
        break;
      case "finished":
        view.phase = "finished";
        view.findings = Array.isArray(d.findings) ? (d.findings as Finding[]) : [];
        view.category = str(d.category);
        view.elapsedS = num(d.elapsed_s);
        view.costUsd = num(d.cost_usd);
        if (!state.report?.models?.[seat] && str(d.model)) view.model = str(d.model)!;
        break;
      case "failed":
      case "timeout":
      case "skipped":
        view.phase = e.type;
        view.error = str(d.error) ?? e.text;
        view.elapsedS = num(d.elapsed_s);
        break;
      default:
        break;
    }
  }
  if (!view.model) view.model = SEAT_INFO[seat].defaultModel;
  view.findings = [...view.findings].sort((a, b) => (b.confidence ?? 0) - (a.confidence ?? 0));
  return view;
}

export function deriveChair(events: TeamEvent[], state: Pick<BoardState, "report" | "status">): ChairView {
  const view: ChairView = {
    model: state.report?.models?.chair ?? CHAIR_MODEL,
    phase: "waiting",
    elapsedS: null,
    costUsd: null,
    error: null,
    text: null,
  };
  for (const e of events) {
    if (e.member !== "chair") continue;
    const d = e.data ?? {};
    if (e.type === "chair_started") {
      view.phase = "thinking";
      view.text = e.text;
      if (!state.report?.models?.chair && str(d.model)) view.model = str(d.model)!;
    } else if (e.type === "chair_done") {
      view.phase = "done";
      view.elapsedS = num(d.elapsed_s);
      view.costUsd = num(d.cost_usd);
    } else if (e.type === "failed") {
      view.phase = "failed";
      view.error = str(d.error) ?? e.text;
    }
  }
  if (state.status !== "working" && view.phase === "waiting") view.phase = "skipped";
  if (state.status !== "working" && view.phase === "thinking") view.phase = "done";
  return view;
}

/** Running cost: the report's total once there is one, otherwise what finished members reported so far. */
export function costSoFar(state: BoardState, seats: SeatView[], chair: ChairView): { usd: number; final: boolean } {
  if (typeof state.report?.cost_usd === "number") return { usd: state.report.cost_usd, final: true };
  const done = runnerDoneCost(state.events);
  if (done !== null) return { usd: done, final: true };
  const sum = seats.reduce((s, v) => s + (v.costUsd ?? 0), 0) + (chair.costUsd ?? 0);
  return { usd: sum, final: false };
}

function runnerDoneCost(events: TeamEvent[]): number | null {
  const done = [...events].reverse().find((e) => e.member === "runner" && e.type === "done");
  return num(done?.data?.cost_usd);
}

export function pct(x: number): string {
  return `${Math.round(Math.max(0, Math.min(1, x)) * 100)}%`;
}

export function formatElapsed(s: number): string {
  const whole = Math.max(0, Math.floor(s));
  const m = Math.floor(whole / 60);
  const sec = whole % 60;
  return m ? `${m}m ${String(sec).padStart(2, "0")}s` : `${(Math.max(0, s)).toFixed(1)}s`;
}

/** "[caution: needs admin rights] sudo ..." -> { caution, text }. */
export function splitCaution(step: string): { caution: string | null; text: string } {
  const m = step.match(/^\[caution: ([^\]]+)\]\s*([\s\S]*)$/);
  return m ? { caution: m[1], text: m[2] } : { caution: null, text: step };
}
