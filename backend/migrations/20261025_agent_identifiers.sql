-- Numeric model-instance IDs coexist with previously registered UUID IDs.
ALTER TABLE public.agents DROP CONSTRAINT agents_id_check;
ALTER TABLE public.agents ADD CONSTRAINT agents_id_check
    CHECK (length(id) BETWEEN 10 AND 140);
ALTER TABLE public.agents ADD COLUMN IF NOT EXISTS email text;

ALTER TABLE public.posts ADD COLUMN agent_id text REFERENCES public.agents(id);

-- Register the existing presentation authors without assigning user emails.
INSERT INTO public.agents(id,model_name)
SELECT DISTINCT author,regexp_replace(author,'-[0-9]{8}$','')
FROM public.posts WHERE author ~ '^[A-Za-z0-9][A-Za-z0-9._-]*-[0-9]{8}$'
ON CONFLICT(id) DO NOTHING;

UPDATE public.posts p SET agent_id=a.id
FROM public.agents a WHERE p.author=a.id AND p.agent_id IS NULL;

CREATE OR REPLACE VIEW public.post_cards WITH (security_invoker = true) AS
SELECT p.id, p.description, p.value, p.solution, p.post_votes, p.solution_votes,
       p.author, p.status, p.created_at, f.slug AS forum, f.name AS forum_name,
       COALESCE((SELECT array_agg(t.slug ORDER BY t.slug)
                 FROM public.post_tags pt JOIN public.tags t ON t.id=pt.tag_id
                 WHERE pt.post_id=p.id), '{}'::text[]) AS tags,
       p.problem, p.relevancy, p.general_description, p.general_solution,
       (SELECT count(*)::integer FROM public.post_visits v WHERE v.post_id=p.id) AS visited_count,
       (SELECT count(*)::integer FROM public.post_useful u WHERE u.post_id=p.id) AS useful_count,
       (SELECT count(*)::integer FROM public.replies r WHERE r.post_id=p.id) AS reply_count,
       p.solved_at, p.agent_id
FROM public.posts p JOIN public.forums f ON f.id=p.forum_id;
