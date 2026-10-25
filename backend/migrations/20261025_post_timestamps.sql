-- Keep original posting times; older solve times are unknown and stay null.
ALTER TABLE public.posts ADD COLUMN IF NOT EXISTS solved_at timestamptz;

CREATE OR REPLACE FUNCTION public.track_post_resolution()
RETURNS trigger LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
    NEW.status := CASE WHEN COALESCE(NEW.general_solution, NEW.solution) IS NULL
                       THEN 'open' ELSE 'solved' END;
    IF TG_OP = 'UPDATE' THEN
        IF NEW.status = 'open' THEN
            NEW.solved_at := NULL;
        ELSIF COALESCE(OLD.general_solution, OLD.solution) IS NULL THEN
            NEW.solved_at := clock_timestamp();
        ELSE
            NEW.solved_at := OLD.solved_at;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER posts_track_resolution
BEFORE INSERT OR UPDATE OF solution, general_solution, status, solved_at
ON public.posts FOR EACH ROW EXECUTE FUNCTION public.track_post_resolution();

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
       p.solved_at
FROM public.posts p JOIN public.forums f ON f.id=p.forum_id;
