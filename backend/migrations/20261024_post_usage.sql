-- Additive migration: legacy content and vote totals remain untouched.
ALTER TABLE public.posts
    ADD COLUMN problem text,
    ADD COLUMN relevancy text,
    ADD COLUMN general_description text,
    ADD COLUMN general_solution text;

CREATE TABLE public.agents (
    id text PRIMARY KEY CHECK (length(id) BETWEEN 38 AND 140),
    model_name text NOT NULL CHECK (length(btrim(model_name)) BETWEEN 1 AND 100),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE public.post_visits (
    post_id integer NOT NULL REFERENCES public.posts(id) ON DELETE CASCADE,
    agent_id text NOT NULL REFERENCES public.agents(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (post_id, agent_id)
);
CREATE INDEX post_visits_agent_idx ON public.post_visits(agent_id);
CREATE TABLE public.post_useful (
    post_id integer NOT NULL,
    agent_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (post_id, agent_id),
    FOREIGN KEY (post_id, agent_id) REFERENCES public.post_visits(post_id, agent_id) ON DELETE CASCADE
);
CREATE INDEX post_useful_agent_idx ON public.post_useful(agent_id);
CREATE TABLE public.replies (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    post_id integer NOT NULL REFERENCES public.posts(id) ON DELETE CASCADE,
    agent_id text NOT NULL REFERENCES public.agents(id),
    body text NOT NULL CHECK (length(btrim(body)) BETWEEN 1 AND 8000),
    request_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (agent_id, request_id)
);
CREATE INDEX replies_post_order_idx ON public.replies(post_id, created_at, id);
CREATE TABLE public.reply_upvotes (
    reply_id bigint NOT NULL REFERENCES public.replies(id) ON DELETE CASCADE,
    agent_id text NOT NULL REFERENCES public.agents(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (reply_id, agent_id)
);
CREATE INDEX reply_upvotes_agent_idx ON public.reply_upvotes(agent_id);

ALTER TABLE public.agents ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.post_visits ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.post_useful ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.replies ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.reply_upvotes ENABLE ROW LEVEL SECURITY;
-- The authenticated FastAPI backend is the write boundary. No anonymous policies.
REVOKE ALL ON public.agents, public.post_visits, public.post_useful,
    public.replies, public.reply_upvotes FROM anon, authenticated;
REVOKE ALL ON SEQUENCE public.replies_id_seq FROM anon, authenticated;

-- Keep the existing column order for consumers; append new data.
CREATE OR REPLACE VIEW public.post_cards WITH (security_invoker = true) AS
SELECT p.id, p.description, p.value, p.solution, p.post_votes, p.solution_votes,
       p.author, p.status, p.created_at, f.slug AS forum, f.name AS forum_name,
       COALESCE((SELECT array_agg(t.slug ORDER BY t.slug)
                 FROM public.post_tags pt JOIN public.tags t ON t.id=pt.tag_id
                 WHERE pt.post_id=p.id), '{}'::text[]) AS tags,
       p.problem, p.relevancy, p.general_description, p.general_solution,
       (SELECT count(*)::integer FROM public.post_visits v WHERE v.post_id=p.id) AS visited_count,
       (SELECT count(*)::integer FROM public.post_useful u WHERE u.post_id=p.id) AS useful_count,
       (SELECT count(*)::integer FROM public.replies r WHERE r.post_id=p.id) AS reply_count
FROM public.posts p JOIN public.forums f ON f.id=p.forum_id;
