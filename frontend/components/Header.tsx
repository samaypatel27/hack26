import type { ConnectionState } from "@/lib/types";

const LABEL: Record<ConnectionState, string> = {
  connecting: "Connecting",
  live: "Live",
  offline: "Offline",
};

const DOT: Record<ConnectionState, string> = {
  connecting: "bg-muted",
  live: "bg-solved",
  offline: "bg-danger",
};

export function Header({
  connection,
  updatedAt,
  now,
}: {
  connection: ConnectionState;
  updatedAt: number | null;
  now: number | null;
}) {
  const seconds = updatedAt && now ? Math.max(0, Math.round((now - updatedAt) / 1000)) : null;

  return (
    <header className="border-b border-line bg-surface">
      <div className="mx-auto flex w-full max-w-7xl flex-wrap items-center justify-between gap-3 px-4 py-4 sm:px-6">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">AgentOverflow</h1>
          <p className="text-sm text-muted">Hard coding problems, and the fixes that were confirmed to work</p>
        </div>
        <div className="flex items-center gap-2 text-sm text-muted" role="status" aria-live="polite">
          <span
            className={`h-2.5 w-2.5 rounded-full ${DOT[connection]}`}
            style={connection === "live" ? { animation: "pulse-dot 2s ease-in-out infinite" } : undefined}
            aria-hidden="true"
          />
          <span className="font-medium text-text">{LABEL[connection]}</span>
          {connection === "live" && seconds !== null && (
            <span className="tabular-nums">· updated {seconds}s ago</span>
          )}
          {connection === "offline" && (
            <span>· {updatedAt ? "showing last data, " : ""}retrying</span>
          )}
        </div>
      </div>
    </header>
  );
}
