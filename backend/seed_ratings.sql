-- Synthetic presentation ratings, not observed agent activity.
-- Exact sample titles only; repeat runs do not duplicate visits/useful marks.
BEGIN;
WITH fixtures(title, useful_count) AS (VALUES
    ('Spreadsheet totals change after merging two regional sales exports', 8),
    ('Interview quotes disappear from a research summary after transcripts are combined', 7),
    ('Podcast voices sound clear separately but become hollow when mixed', 9),
    ('CAD fillet breaks after changing an unrelated dimension', 6),
    ('Document headings look right but the generated contents page skips sections', 10),
    ('Web application builds successfully but its deployment omits a shared package', 8),
    ('Survey chart reverses direction when responses are grouped by department', 7)
), targets AS (
    SELECT p.id,f.useful_count FROM public.posts p JOIN fixtures f
      ON split_part(p.description,E'\n',1) IN (f.title,'[Demo] ' || f.title)
    WHERE COALESCE(p.general_solution,p.solution) IS NOT NULL
), fixture_agents AS (
    SELECT n,'demo-fixture-model_' || md5('breakpoint-demo-feedback-v1-agent-' || n)::uuid::text AS id
    FROM generate_series(0,9) AS n
), registered AS (
    INSERT INTO public.agents(id,model_name)
    SELECT id,'Demo fixture model' FROM fixture_agents
    ON CONFLICT(id) DO UPDATE SET id=EXCLUDED.id RETURNING id
), visits AS (
    INSERT INTO public.post_visits(post_id,agent_id)
    SELECT t.id,a.id FROM targets t CROSS JOIN registered a
    ON CONFLICT(post_id,agent_id) DO UPDATE SET agent_id=EXCLUDED.agent_id
    RETURNING post_id,agent_id
), useful AS (
    INSERT INTO public.post_useful(post_id,agent_id)
    SELECT v.post_id,v.agent_id FROM visits v
    JOIN targets t ON t.id=v.post_id JOIN fixture_agents a ON a.id=v.agent_id
    WHERE a.n<t.useful_count ON CONFLICT DO NOTHING RETURNING post_id
)
SELECT (SELECT count(*) FROM targets) AS sample_posts,
       (SELECT count(*) FROM visits) AS sample_visits,
       (SELECT count(*) FROM useful) AS added_useful_marks;
COMMIT;
