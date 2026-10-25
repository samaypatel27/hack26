export type PostCard = {
  id: number;
  headline: string;
  preview: string;
  value: string;
  forum: string; // slug, e.g. "devops"
  forum_name: string; // display name, e.g. "DevOps"
  tags: string[]; // slugs
  author: string;
  post_votes: number;
  solution_votes: number;
  is_open: boolean;
  created_at: string;
};

export type PostDetail = PostCard & {
  body: string;
  solution: string | null;
};

export type Forum = {
  slug: string;
  name: string;
  description: string;
  tags: string[];
  post_count: number;
};

export type Sort = "top" | "newest";

export type StatusFilter = "all" | "solved" | "open";

export type VoteTarget = "post" | "solution";

export type ConnectionState = "connecting" | "live" | "offline";

/** A post that just appeared, or just went from Open to Solved, since the previous poll. */
export type Change = { kind: "new" | "solved"; at: number };
