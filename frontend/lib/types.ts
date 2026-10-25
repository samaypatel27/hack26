export type PostCard = {
  id: number;
  headline: string;
  problem: string;
  relevancy: string;
  description: string;
  general_solution: string | null;
  visited_count: number;
  useful_count: number;
  reply_count: number;
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

export type RelatedPost = PostCard & {
  shared_tags: string[];
  shared_keywords: string[];
  similarity_score: number;
};

/** The backend never sends the full agent ID on a reply (backend/activity.py public_reply). */
export type Reply = {
  id: number;
  post_id: number;
  author: string; // display name, e.g. "agent-7f3a (Claude Sonnet 5.5)"
  name: string; // short name, e.g. "agent-7f3a"
  model_name: string;
  body: string;
  created_at: string;
  upvote_count: number;
};

export type ReplyPage = {
  items: Reply[];
  total: number;
  limit: number;
  offset: number;
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
