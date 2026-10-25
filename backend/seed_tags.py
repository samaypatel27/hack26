"""Sync starter tags and repair exact sample fixtures without changing other posts.

Run after seed.py: backend/.venv/Scripts/python.exe backend/seed_tags.py
Use --print-sql to apply the same data import through Supabase MCP.
"""
import json
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

if __package__:
    from .seed import SAMPLES
else:
    from seed import SAMPLES


def seed_sql() -> str:
    taxonomy = json.loads((Path(__file__).parents[1] / "taxonomy.json").read_text(encoding="utf-8"))
    samples = [{"description": f"[Demo] {title}\n\n{detail}\n\nIllustrative sample: votes and outcomes are synthetic, not observed agent activity.",
                "tags": tags} for title, _, detail, _, tags, _ in SAMPLES]
    forums_json = json.dumps(taxonomy["forums"]).replace("'", "''")
    samples_json = json.dumps(samples).replace("'", "''")
    return f"""BEGIN;
    WITH catalog AS (SELECT * FROM jsonb_to_recordset('{forums_json}'::jsonb)
        AS f(slug text, name text, description text, tags jsonb)),
    registered AS (
        INSERT INTO public.tags(slug)
        SELECT DISTINCT jsonb_array_elements_text(tags) FROM catalog
        ON CONFLICT(slug) DO UPDATE SET slug=EXCLUDED.slug RETURNING id,slug
    )
    INSERT INTO public.forum_tags(forum_id,tag_id)
    SELECT DISTINCT f.id,r.id FROM catalog c JOIN public.forums f ON f.slug=c.slug
    CROSS JOIN LATERAL jsonb_array_elements_text(c.tags) AS tag(slug)
    JOIN registered r ON r.slug=tag.slug ON CONFLICT DO NOTHING;

    -- Match entire fixture descriptions, not loosely similar user content.
    WITH fixtures AS (SELECT * FROM jsonb_to_recordset('{samples_json}'::jsonb)
        AS s(description text,tags jsonb))
    INSERT INTO public.post_tags(post_id,tag_id)
    SELECT p.id,t.id FROM fixtures s JOIN public.posts p ON p.description=s.description
    CROSS JOIN LATERAL jsonb_array_elements_text(s.tags) AS tag(slug)
    JOIN public.tags t ON t.slug=tag.slug ON CONFLICT DO NOTHING;

    -- Remove the placeholder only after meaningful tags exist on those exact demo posts.
    WITH fixtures AS (SELECT * FROM jsonb_to_recordset('{samples_json}'::jsonb)
        AS s(description text,tags jsonb))
    DELETE FROM public.post_tags pt USING public.posts p, public.tags t
    WHERE pt.post_id=p.id AND pt.tag_id=t.id AND t.slug='general'
      AND p.description IN (SELECT description FROM fixtures)
      AND EXISTS (SELECT 1 FROM public.post_tags specific JOIN public.tags st ON st.id=specific.tag_id
                  WHERE specific.post_id=p.id AND st.slug<>'general');

    -- Make every tag actually used in a forum available in its dropdown as well.
    INSERT INTO public.forum_tags(forum_id,tag_id)
    SELECT DISTINCT p.forum_id,pt.tag_id FROM public.posts p JOIN public.post_tags pt ON pt.post_id=p.id
    ON CONFLICT DO NOTHING;
    COMMIT;
    """


if __name__ == "__main__":
    query = seed_sql()
    if "--print-sql" in sys.argv:
        print(query)
    else:
        load_dotenv(Path(__file__).with_name(".env"))
        with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=15) as conn:
            conn.execute(query)
        print("Synced starter tags, forum suggestions, and exact demo-post tags.")
