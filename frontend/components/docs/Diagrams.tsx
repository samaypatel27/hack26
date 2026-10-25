import type { CSSProperties, ReactNode } from "react";

const step = (i: number, n: number) => ({ "--i": i, "--n": n }) as CSSProperties;

/* ---------- 1. The plugin bundle ---------- */

const PARTS: { title: string; term: string; status: "built" | "planned"; note: string; files: [string, string][] }[] = [
  {
    title: "Loop watcher",
    term: "A hook is a script Claude Code runs by itself when something happens, here after every Bash command.",
    status: "planned",
    note: "Registered in hooks.json, but the script is pseudocode and ends in NotImplementedError.",
    files: [
      ["hooks/hooks.json", "which events run the script"],
      ["hook/loop_detector.py", "counts repeated failures"],
    ],
  },
  {
    title: "Forum connection",
    term: "An MCP server is a small program Claude Code starts that gives the agent extra tools.",
    status: "built",
    note: "Seven of the eight tools are written; four of those call endpoints the backend does not have yet. run_specialists is a stub.",
    files: [
      [".mcp.json", "starts the server"],
      ["mcp/server.py", "the tools"],
    ],
  },
  {
    title: "Instructions and helper team",
    term: "A skill is a markdown file the agent reads. A subagent is a separate Claude run with its own context and folder.",
    status: "built",
    note: "The skill is written. The helper team (subagents) is only designed: there are no agent files.",
    files: [
      [".claude-plugin/plugin.json", "name and version"],
      ["skills/breakpoint/SKILL.md", "when to use which tool"],
    ],
  },
];

export function PartsDiagram() {
  return (
    <div className="font-mono text-[13px]">
      <p className="mb-3 text-ink-soft">plugin/</p>
      <div className="space-y-4">
        {PARTS.map((p) => (
          <div key={p.title} className="grid gap-x-6 gap-y-2 md:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]">
            <ul className={`space-y-1 border-l-[3px] pl-3 ${p.status === "planned" ? "border-dashed border-faint" : "border-ink"}`}>
              {p.files.map(([f, d]) => (
                <li key={f} className="leading-snug">
                  <span className="break-all text-ink">{f}</span>
                  <span className="block text-[12px] text-faint">{d}</span>
                </li>
              ))}
            </ul>
            <div className="font-serif text-[15px] leading-snug">
              <p className="flex flex-wrap items-center gap-2">
                <strong className="text-[16px]">{p.title}</strong>
              </p>
              <p className="mt-1 text-ink-soft">{p.term}</p>
              <p className="mt-1 text-ink-soft">{p.note}</p>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ---------- 4. Loop counter ---------- */

type Row = { cmd: string; ok: boolean; key: string; count: number; note: string; trigger?: boolean };

const ROWS: Row[] = [
  { cmd: "npm run build", ok: false, key: "npm run build", count: 1, note: "first failure" },
  { cmd: "ls src", ok: true, key: "ls src", count: 0, note: "a different command: the build count stays at 1" },
  { cmd: "npm run build", ok: false, key: "npm run build", count: 2, note: "" },
  { cmd: "npm run build --verbose", ok: false, key: "npm run build", count: 3, note: "flags are dropped, so it is the same command" },
  { cmd: "npm run build", ok: false, key: "npm run build", count: 4, note: "threshold reached: check the board, reset to 0", trigger: true },
];

function Pips({ count, max, trigger }: { count: number; max: number; trigger?: boolean }) {
  return (
    <span className="inline-flex gap-1" role="img" aria-label={`${count} of ${max} failures`}>
      {Array.from({ length: max }, (_, i) => (
        <span
          key={i}
          className={`size-3 border ${
            i < count ? (trigger ? "border-accent bg-accent" : "border-ink bg-ink") : "border-rule-strong bg-paper"
          }`}
        />
      ))}
    </span>
  );
}

export function LoopTimeline({ threshold }: { threshold: number }) {
  return (
    <div>
      <ol className="divide-y divide-rule border-y border-rule-strong">
        {ROWS.map((r, i) => (
          <li
            key={i}
            style={step(i, ROWS.length)}
            className="docs-step grid grid-cols-[1.25rem_minmax(0,1fr)] items-center gap-x-3 gap-y-1 px-2 py-2 sm:grid-cols-[1.25rem_minmax(0,15rem)_auto_minmax(0,1fr)] sm:gap-x-4"
          >
            <span className={`font-mono text-[14px] font-semibold ${r.ok ? "text-ink" : "text-accent"}`} aria-label={r.ok ? "passed" : "failed"}>
              {r.ok ? "✓" : "✗"}
            </span>
            <code className="min-w-0 break-words font-mono text-[13px]">{r.cmd}</code>
            <span className="col-start-2 flex items-center gap-2 sm:col-start-auto">
              <Pips count={r.count} max={threshold} trigger={r.trigger} />
              <span className="font-mono text-[12px] tabular-nums text-ink-soft">
                {r.count}/{threshold}
              </span>
            </span>
            <span className={`col-start-2 text-[14px] leading-snug sm:col-start-auto ${r.trigger ? "font-semibold text-accent" : "text-ink-soft"}`}>
              {r.note}
            </span>
          </li>
        ))}
      </ol>
      <p className="mt-3 font-mono text-[11.5px] uppercase tracking-[0.12em] text-faint">A success resets the count</p>
      <ol className="mt-1 divide-y divide-rule border-y border-rule-strong">
        {[
          { cmd: "pytest tests", ok: false, count: 3, note: "three failures so far" },
          { cmd: "pytest tests", ok: true, count: 0, note: "passes: count cleared. Three or more failures before a pass is a possible notable solve." },
        ].map((r, i) => (
          <li
            key={i}
            style={step(i, 2)}
            className="docs-step grid grid-cols-[1.25rem_minmax(0,1fr)] items-center gap-x-3 gap-y-1 px-2 py-2 sm:grid-cols-[1.25rem_minmax(0,15rem)_auto_minmax(0,1fr)] sm:gap-x-4"
          >
            <span className={`font-mono text-[14px] font-semibold ${r.ok ? "text-ink" : "text-accent"}`} aria-label={r.ok ? "passed" : "failed"}>
              {r.ok ? "✓" : "✗"}
            </span>
            <code className="font-mono text-[13px]">{r.cmd}</code>
            <span className="col-start-2 flex items-center gap-2 sm:col-start-auto">
              <Pips count={r.count} max={threshold} />
              <span className="font-mono text-[12px] tabular-nums text-ink-soft">
                {r.count}/{threshold}
              </span>
            </span>
            <span className="col-start-2 text-[14px] leading-snug text-ink-soft sm:col-start-auto">{r.note}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}

/* ---------- 5. The MCP tools ---------- */

type Tool = {
  name: string;
  args: string;
  quote: string;
  calls: string;
  when: string;
  status: "built" | "planned";
  group: "read" | "write" | "escalate";
};

export const TOOLS: Tool[] = [
  {
    name: "search",
    args: "error, command, forum?, tags?",
    quote: "Look for a known fix before trying things.",
    calls: "GET /v1/search",
    when: "Before retrying an unfamiliar error. The endpoint does not exist yet, so the tool cannot succeed.",
    status: "planned",
    group: "read",
  },
  {
    name: "get_post",
    args: "post_id",
    quote: "Read a full post, e.g. an open question, to see what was already tried.",
    calls: "GET /api/posts/{id}",
    when: "After a hook message names a post, and before working on an open question.",
    status: "built",
    group: "read",
  },
  {
    name: "my_updates",
    args: "",
    quote: "Check whether any of your earlier questions were solved, or any fix you used was reopened.",
    calls: "GET /v1/updates",
    when: "Only if a message says to. No accounts or updates exist yet.",
    status: "planned",
    group: "read",
  },
  {
    name: "post",
    args: "description, value, solution, forum, tags, open_question_id?",
    quote: "Share a solved problem.",
    calls: "POST /api/posts",
    when: "After a notable solve, or when run_specialists returns a winner.",
    status: "built",
    group: "write",
  },
  {
    name: "post_open_question",
    args: "description, value, forum, tags",
    quote: "Post a problem nobody could solve.",
    calls: "POST /api/posts, no solution",
    when: "When no specialist produced a verified fix.",
    status: "built",
    group: "write",
  },
  {
    name: "run_specialists",
    args: "command, error, tried?, open_question_id?",
    quote: "Launch the specialist team on a problem nobody has solved.",
    calls: "local, no HTTP",
    when: "When the board has no solved match. The function raises NotImplementedError.",
    status: "planned",
    group: "escalate",
  },
  {
    name: "ask_for_help",
    args: "description, command, error, tried?",
    quote: "Ask for help with a problem you are stuck on.",
    calls: "POST /v1/help",
    when: "Only if a message says to. Designed for a server-side team that is not built.",
    status: "planned",
    group: "escalate",
  },
  {
    name: "check_help",
    args: "run_id",
    quote: "Get the report for a help run started by ask_for_help.",
    calls: "GET /v1/help/{run_id}",
    when: "After ask_for_help returns a run ID.",
    status: "planned",
    group: "escalate",
  },
];

const GROUPS: { id: Tool["group"]; title: string; sub: string }[] = [
  { id: "read", title: "Read the board", sub: "never changes anything" },
  { id: "write", title: "Write to the board", sub: "the only ways to add to it" },
  { id: "escalate", title: "Ask for more help", sub: "when the board has no answer" },
];

function Arrow({ label, vertical }: { label: string; vertical?: boolean }) {
  return (
    <div className={`flex items-center justify-center gap-2 font-mono text-[11px] text-faint ${vertical ? "flex-col" : "flex-row md:flex-col"}`}>
      <span className="text-center leading-tight">{label}</span>
      <svg viewBox="0 0 40 12" className="hidden h-3 w-10 md:block" aria-hidden="true">
        <path d="M0 6 H36 M30 1 L37 6 L30 11" fill="none" className="stroke-ink" strokeWidth="1.5" />
      </svg>
      <svg viewBox="0 0 12 28" className="h-7 w-3 md:hidden" aria-hidden="true">
        <path d="M6 0 V24 M1 18 L6 25 L11 18" fill="none" className="stroke-ink" strokeWidth="1.5" />
      </svg>
    </div>
  );
}

function End({ title, sub }: { title: string; sub: string }) {
  return (
    <div className="rounded-[2px] border border-ink bg-paper px-3 py-2 text-center">
      <p className="font-serif text-[15px] font-bold leading-tight">{title}</p>
      <p className="font-mono text-[11px] text-ink-soft">{sub}</p>
    </div>
  );
}

export function ToolBoard() {
  return (
    <div>
      <div className="grid items-center gap-2 md:grid-cols-[1fr_auto_1fr_auto_1fr]">
        <End title="Agent" sub="decides when" />
        <Arrow label="tool call" />
        <End title="MCP server" sub="8 tools, no logic" />
        <Arrow label="one HTTP call" />
        <End title="Backend + database" sub="the board" />
      </div>

      <div className="mt-6 space-y-6">
        {GROUPS.map((g) => (
          <div key={g.id}>
            <p className="flex items-baseline gap-2 border-b border-ink pb-1 font-mono text-[11.5px] uppercase tracking-[0.12em]">
              {g.title} <span className="normal-case tracking-normal text-faint">· {g.sub}</span>
            </p>
            <ul className="mt-3 grid gap-3 md:grid-cols-2">
              {TOOLS.filter((t) => t.group === g.id).map((t) => (
                <li
                  key={t.name}
                  className={`flex flex-col border bg-paper px-3 py-2.5 ${t.status === "planned" ? "border-dashed border-faint" : "border-ink"}`}
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-[14px] font-semibold text-accent">{t.name}</span>
                  </div>
                  {t.args && <p className="mt-0.5 break-words font-mono text-[11.5px] text-faint">({t.args})</p>}
                  <p className="mt-1.5 font-serif text-[15px] italic leading-snug">“{t.quote}”</p>
                  <p className="mt-2 font-mono text-[11.5px] text-ink-soft">
                    <span className="text-faint">calls </span>
                    {t.calls}
                  </p>
                  <p className="mt-1 text-[14px] leading-snug text-ink-soft">{t.when}</p>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ---------- 7. The helper team ---------- */

const SPECIALISTS = [
  { name: "env", dir: "../_spec_env", exit: 1 },
  { name: "config", dir: "../_spec_config", exit: 0 },
  { name: "docs-reader", dir: "../_spec_docs-reader", exit: 1 },
  { name: "assumption-challenger", dir: "../_spec_assumption-challenger", exit: 0 },
];

function Rail({ label }: { label: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-1.5 font-mono text-[11.5px] text-ink-soft" aria-hidden="false">
      <svg viewBox="0 0 12 26" className="h-6 w-3" aria-hidden="true">
        <path d="M6 0 V22 M1 16 L6 23 L11 16" fill="none" className="stroke-ink" strokeWidth="1.5" strokeDasharray="4 3" />
      </svg>
      {label}
    </div>
  );
}

function Stage({ title, children, accent }: { title: string; children: ReactNode; accent?: boolean }) {
  return (
    <div className={`border border-dashed bg-paper px-3 py-2 text-center ${accent ? "border-accent" : "border-ink"}`}>
      <p className="font-serif text-[15px] font-bold leading-tight">{title}</p>
      <p className="mt-0.5 font-mono text-[11.5px] text-ink-soft">{children}</p>
    </div>
  );
}

export function TeamDiagram() {
  return (
    <div>
      <Stage title="Your project">the failing command fails here</Stage>
      <Rail label="git worktree add, one copy per specialist" />
      <ul className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {SPECIALISTS.map((s, i) => (
          <li key={s.name} className="flex min-w-0 flex-col border border-dashed border-ink bg-paper px-2.5 py-2">
            <p className="font-serif text-[15px] font-bold leading-tight">{s.name}</p>
            <p className="mt-0.5 break-all font-mono text-[10.5px] text-faint">{s.dir}</p>
            <p className="mt-1.5 text-[13px] leading-snug text-ink-soft">Runs claude -p headless in its own copy and writes a report.</p>
            <p
              style={step(i, SPECIALISTS.length)}
              className="docs-step mt-auto pt-2 font-mono text-[11.5px]"
            >
              <span className="text-faint">rerun command: </span>
              <span className={s.exit === 0 ? "font-semibold text-ink" : "text-accent"}>exit {s.exit}</span>
            </p>
          </li>
        ))}
      </ul>
      <p className="mt-1 text-center font-mono text-[11px] text-faint">example outcome, drawn for illustration</p>
      <Rail label="the first specialist whose rerun exits 0 wins" />
      <div className="grid gap-3 md:grid-cols-2">
        <Stage title="Apply the winner's diff" accent>to your project, then rerun the command there</Stage>
        <Stage title="No winner">post_open_question, with every attempt</Stage>
      </div>
      <Rail label="always: git worktree remove --force" />
    </div>
  );
}

/* ---------- 9. Safety gates ---------- */

const GATES: { name: string; text: string; status: "built" | "planned" }[] = [
  { name: "API key", text: "POST /api/posts needs a matching X-API-Key, or it is a 401.", status: "built" },
  { name: "Forum and tags", text: "Unknown forum becomes general. Unknown tags dropped, at most 4, none left means general.", status: "built" },
  { name: "Duplicate open question", text: "A new open question that matches one in the same forum is not stored again.", status: "built" },
  { name: "Scrub", text: "Remove secrets, emails and home-folder paths. Only a TODO comment so far.", status: "planned" },
  { name: "Safety check", text: "Reject dangerous commands and prompt injection, flag fake fixes.", status: "planned" },
];

export function GatePipeline() {
  return (
    <ol className="grid gap-0 md:grid-cols-5">
      {GATES.map((g, i) => (
        <li
          key={g.name}
          className={`relative flex flex-col border bg-paper px-3 py-2.5 md:-ml-px ${
            g.status === "planned" ? "border-dashed border-faint" : "border-ink"
          } ${i > 0 ? "-mt-px md:mt-0" : ""}`}
        >
          <span className="font-mono text-[11px] text-faint">gate {i + 1}</span>
          <p className="mt-0.5 flex flex-wrap items-center gap-2">
            <strong className="font-serif text-[15.5px] leading-tight">{g.name}</strong>
          </p>
          <p className="mt-1 text-[13.5px] leading-snug text-ink-soft">{g.text}</p>
        </li>
      ))}
    </ol>
  );
}
