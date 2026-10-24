export type PostCard = {
  id: number;
  headline: string;
  value: string;
  tags: string[];
  upvotes: number;
  status: "open" | "solved";
  is_open: boolean;
  created_at: string;
};

export type PostDetail = PostCard & {
  description: string;
  solution: string | null;
};

export type Filter = "all" | "solved" | "open";

export type ConnectionState = "connecting" | "live" | "offline";

/** A step in a problem's journey (see CLAUDE.md "Workflows end to end"). */
export type StepId = "spotted" | "checked" | "specialists" | "confirmed" | "open";

export type Step = { id: StepId; at?: string };

/** A post that just appeared, or just went from Open to Solved, since the previous poll. */
export type Change = { kind: "new" | "solved"; at: number };
