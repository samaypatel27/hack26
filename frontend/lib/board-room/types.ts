// Shapes of GET /api/help/{run_id} (backend/team_api.py, backend/team/report.py, backend/team/runner.py).
// Board room only: nothing else in the site imports this file.

export type Seat = "docs" | "issues" | "community";
export type Member = Seat | "chair" | "runner";

export type TeamEvent = {
  id: number;
  member: Member | string;
  /** started | searching | fetching | source | fallback | tool_error | finished | failed | timeout | skipped |
   *  chair_started | chair_done | done */
  type: string;
  text: string;
  data: Record<string, unknown> | null;
  /** The in-memory store calls the timestamp `t`; other stores may call it `created_at`. */
  t?: string | null;
  created_at?: string | null;
};

export type Evidence = { url: string; source?: string; quote?: string; seen?: boolean };

export type Check = { command: string | null; description: string };

export type Hypothesis = {
  rank: number;
  cause: string;
  confidence: number;
  evidence: Evidence[];
  steps: string[];
  check: Check;
  agreed_by: string[];
};

export type Finding = {
  cause: string;
  confidence: number;
  evidence?: Evidence[];
  steps?: string[];
  check?: Check;
};

export type Flag = { rank?: number; step: string; reason: string; action: "removed" | "caution" };

export type TeamReport = {
  status: "report" | "no_answer" | "failed";
  hypotheses: Hypothesis[];
  if_none_work?: string | null;
  environment_check?: string | null;
  category?: string | null;
  models?: Partial<Record<Seat | "chair", string>>;
  cost_usd?: number;
  costs?: Record<string, { model?: string; cost_usd?: number; web_searches?: number; web_fetches?: number }>;
  timings?: Record<string, number>;
  flagged?: Flag[];
  notes?: string[];
  verification?: string;
  min_confidence?: number;
  chair_fallback?: boolean;
  error?: string | null;
};

export type RunStatus = "working" | "report" | "no_answer" | "failed";

export type HelpResponse = {
  run_id: string;
  status: RunStatus;
  message: string;
  events: TeamEvent[];
  report: TeamReport | null;
  source?: string | null;
  open_question_id?: number | null;
  elapsed_s?: number;
  store?: string;
  /** Not sent by the backend today; shown in the header if it ever is (the demo fixture has one). */
  case_file?: CaseSummary | null;
};

/** The few case-file fields the header shows. */
export type CaseSummary = {
  goal?: string;
  symptom?: string;
  environment?: string;
  check_command?: string;
  tried?: { attempt: string; result: string }[];
  project?: string;
  model?: string;
};

export type Connection = "connecting" | "live" | "offline" | "missing";

/** What the board-room view needs, from either the live poller or the demo player. */
export type BoardState = {
  runId: string;
  status: RunStatus;
  message: string;
  events: TeamEvent[];
  report: TeamReport | null;
  elapsedS: number;
  connection: Connection;
  caseFile: CaseSummary | null;
  error?: string | null;
  /** "help" (a stuck agent asked) or "rerun" (a cold-case re-run of an Open Question, no gate). */
  source?: string | null;
  openQuestionId?: number | null;
};
