import type { CSSProperties, ReactNode } from "react";

export type Who = "hook" | "agent" | "tool" | "board" | "team";

export type FlowNode =
  | { kind?: "step"; who: Who; text: ReactNode; planned?: boolean }
  | { kind: "decide"; text: ReactNode; planned?: boolean; yes: Branch; no: Branch };

export type Branch = { label: string; nodes: FlowNode[] };

const WHO: Record<Who, { label: string; cls: string }> = {
  hook: { label: "Hook", cls: "border-ink bg-paper-deep text-ink" },
  agent: { label: "Agent", cls: "border-ink bg-card text-ink" },
  tool: { label: "MCP tool", cls: "border-accent bg-card text-accent" },
  board: { label: "Board", cls: "border-ink bg-ink text-paper" },
  team: { label: "Specialists", cls: "border-dashed border-ink bg-card text-ink" },
};

/** Reading order of every node, depth first, so the highlight walks the flow top to bottom. */
function order(nodes: FlowNode[], into = new Map<FlowNode, number>()): Map<FlowNode, number> {
  for (const x of nodes) {
    into.set(x, into.size);
    if (x.kind === "decide") {
      order(x.yes.nodes, into);
      order(x.no.nodes, into);
    }
  }
  return into;
}

function Chain({ nodes, index }: { nodes: FlowNode[]; index: Map<FlowNode, number> }) {
  const total = index.size;
  return (
    <ol className="relative space-y-3 pl-6 before:absolute before:bottom-3 before:left-[7px] before:top-3 before:w-px before:bg-rule-strong">
      {nodes.map((node, k) => {
        const i = index.get(node) ?? 0;
        const style = { "--i": i, "--n": total } as CSSProperties;
        if (node.kind === "decide") {
          return (
            <li key={k} className="relative">
              <span className="absolute -left-6 top-3.5 size-[9px] rotate-45 border border-accent bg-paper" aria-hidden="true" />
              <div
                style={style}
                className={`docs-step border border-l-[3px] border-rule-strong border-l-accent bg-card px-3 py-2 ${node.planned ? "border-dashed" : ""}`}
              >
                <p className="flex flex-wrap items-center gap-2 text-[15.5px] leading-snug">
                  <span className="font-mono text-[10.5px] font-medium uppercase tracking-[0.12em] text-accent">Decision</span>
                  {node.text}
                </p>
              </div>
              <div className="mt-3 grid gap-4 md:grid-cols-2">
                {[node.yes, node.no].map((b, bi) => (
                  <div key={bi} className="min-w-0">
                    <p className="mb-2 font-mono text-[11.5px] font-medium uppercase tracking-[0.12em] text-ink-soft">
                      {bi === 0 ? "yes" : "no"} <span className="normal-case tracking-normal text-faint">· {b.label}</span>
                    </p>
                    <Chain nodes={b.nodes} index={index} />
                  </div>
                ))}
              </div>
            </li>
          );
        }
        const w = WHO[node.who];
        return (
          <li key={k} className="relative">
            <span className="absolute -left-6 top-3.5 size-[9px] border border-ink bg-paper" aria-hidden="true" />
            <div
              style={style}
              className={`docs-step border border-rule-strong bg-card px-3 py-2 ${node.planned ? "border-dashed" : ""}`}
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className={`rounded-[2px] border px-1.5 py-px font-mono text-[10.5px] font-medium uppercase tracking-[0.1em] ${w.cls}`}>
                  {w.label}
                </span>
              </div>
              <p className="mt-1 text-[15.5px] leading-snug">{node.text}</p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

/** A vertical chain of steps; decisions split into a yes column and a no column. Steps light up in turn. */
export function Flow({ nodes }: { nodes: FlowNode[] }) {
  return <Chain nodes={nodes} index={order(nodes)} />;
}
