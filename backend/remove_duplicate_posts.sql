-- Merge only identical question text; retain the lowest existing post ID.
BEGIN;
LOCK TABLE public.posts,public.post_tags,public.post_visits,public.post_useful,
    public.replies,public.reply_upvotes IN SHARE ROW EXCLUSIVE MODE;

WITH numbered AS (
    SELECT id,min(id) OVER (
        PARTITION BY lower(regexp_replace(btrim(description),'[[:space:]]+',' ','g'))
    ) AS keep_id FROM public.posts
), duplicate_map AS (
    SELECT id AS remove_id,keep_id FROM numbered WHERE id<>keep_id
)
INSERT INTO public.post_tags(post_id,tag_id)
SELECT m.keep_id,t.tag_id FROM duplicate_map m JOIN public.post_tags t ON t.post_id=m.remove_id
ON CONFLICT DO NOTHING;

WITH numbered AS (
    SELECT id,min(id) OVER (
        PARTITION BY lower(regexp_replace(btrim(description),'[[:space:]]+',' ','g'))
    ) AS keep_id FROM public.posts
), duplicate_map AS (
    SELECT id AS remove_id,keep_id FROM numbered WHERE id<>keep_id
)
INSERT INTO public.post_visits(post_id,agent_id,created_at)
SELECT m.keep_id,v.agent_id,min(v.created_at) FROM duplicate_map m
JOIN public.post_visits v ON v.post_id=m.remove_id GROUP BY m.keep_id,v.agent_id
ON CONFLICT DO NOTHING;

WITH numbered AS (
    SELECT id,min(id) OVER (
        PARTITION BY lower(regexp_replace(btrim(description),'[[:space:]]+',' ','g'))
    ) AS keep_id FROM public.posts
), duplicate_map AS (
    SELECT id AS remove_id,keep_id FROM numbered WHERE id<>keep_id
)
INSERT INTO public.post_useful(post_id,agent_id,created_at)
SELECT m.keep_id,u.agent_id,min(u.created_at) FROM duplicate_map m
JOIN public.post_useful u ON u.post_id=m.remove_id GROUP BY m.keep_id,u.agent_id
ON CONFLICT DO NOTHING;

WITH numbered AS (
    SELECT id,min(id) OVER (
        PARTITION BY lower(regexp_replace(btrim(description),'[[:space:]]+',' ','g'))
    ) AS keep_id FROM public.posts
), duplicate_map AS (
    SELECT id AS remove_id,keep_id FROM numbered WHERE id<>keep_id
)
UPDATE public.replies r SET post_id=m.keep_id
FROM duplicate_map m WHERE r.post_id=m.remove_id;

WITH numbered AS (
    SELECT id,min(id) OVER (
        PARTITION BY lower(regexp_replace(btrim(description),'[[:space:]]+',' ','g'))
    ) AS keep_id FROM public.posts
), duplicate_map AS (
    SELECT id AS remove_id,keep_id FROM numbered WHERE id<>keep_id
)
, vote_totals AS (
SELECT m.keep_id,max(d.post_votes) AS post_votes,max(d.solution_votes) AS solution_votes
FROM duplicate_map m JOIN public.posts d ON d.id=m.remove_id GROUP BY m.keep_id
)
UPDATE public.posts p SET post_votes=greatest(p.post_votes,v.post_votes),
    solution_votes=greatest(p.solution_votes,v.solution_votes)
FROM vote_totals v WHERE p.id=v.keep_id;

WITH numbered AS (
    SELECT id,min(id) OVER (
        PARTITION BY lower(regexp_replace(btrim(description),'[[:space:]]+',' ','g'))
    ) AS keep_id FROM public.posts
), duplicate_map AS (
    SELECT id AS remove_id,keep_id FROM numbered WHERE id<>keep_id
)
DELETE FROM public.posts p USING duplicate_map m WHERE p.id=m.remove_id RETURNING p.id;
COMMIT;
