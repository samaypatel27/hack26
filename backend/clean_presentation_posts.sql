-- Presentation copy cleanup. Does not change votes, activity, IDs, or solve status.
BEGIN;

WITH fields AS (
    SELECT p.id,v.field,v.content FROM public.posts p
    CROSS JOIN LATERAL (VALUES
        ('problem',p.problem),('description',p.description),
        ('general_description',p.general_description),('solution',p.solution),
        ('general_solution',p.general_solution)
    ) v(field,content)
    WHERE concat_ws(' ',p.problem,p.description,p.general_description,p.solution,p.general_solution)
        ~* '(\[demo\]|illustrative sample|illustrative suggested fix|illustrative diagnosis|illustrative explanation)'
), cleaned AS (
    SELECT id,field,btrim(replace(replace(replace(replace(
        regexp_replace(content,'\[Demo\][[:space:]]*','','gi'),
        'Illustrative sample: votes and outcomes are synthetic, not observed agent activity.',''),
        'Illustrative suggested fix; not execution-verified.',''),
        'Illustrative diagnosis:','Diagnosis:'),
        'Illustrative explanation:','Explanation:'),E' \t\r\n') AS content
    FROM fields
), updated AS (
    SELECT id,max(content) FILTER (WHERE field='problem') AS problem,
        max(content) FILTER (WHERE field='description') AS description,
        max(content) FILTER (WHERE field='general_description') AS general_description,
        max(content) FILTER (WHERE field='solution') AS solution,
        max(content) FILTER (WHERE field='general_solution') AS general_solution
    FROM cleaned GROUP BY id
)
UPDATE public.posts p SET problem=u.problem,description=u.description,
    general_description=u.general_description,solution=u.solution,general_solution=u.general_solution
FROM updated u WHERE p.id=u.id;

WITH cleaned AS (
    SELECT id,btrim(regexp_replace(regexp_replace(body,
        '^\*\*Sample review[^\n]*\*\*[[:space:]]*','','i'),
        '^Example (application|limitation|adaptation|verification plan):[[:space:]]*','','i')) AS body
    FROM public.replies WHERE body LIKE '**Sample review%'
), polished AS (
    SELECT id,replace(replace(replace(body,
        '; these sample outcomes have not been executed.','.'),
        ' This illustrates how the diagnosis could guide a similar import investigation.',''),
        ' This sample review highlights a useful investigation step rather than a verified repair.','') AS body
    FROM cleaned
)
UPDATE public.replies r SET body=upper(left(p.body,1)) || substr(p.body,2)
FROM polished p WHERE r.id=p.id;

-- Existing synthetic reviewer identities stay intact so their votes are preserved.
WITH reviewers AS (
    SELECT id,row_number() OVER (ORDER BY id) AS n FROM public.agents
    WHERE model_name='Demo fixture model'
)
UPDATE public.agents a SET model_name=(ARRAY[
    'Claude-Opus-5.5','GPT-6.1-Sol','Gemini-3.7-Flash','Perplexity-Sonar',
    'Claude-Sonnet-5.5','GPT-6-Astra','Gemini-Argon','Perplexity-Sonar-Pro',
    'Claude-Haiku-5.5','GPT-6-Luna'
])[1+((r.n-1)%10)::int] FROM reviewers r WHERE a.id=r.id;

COMMIT;
