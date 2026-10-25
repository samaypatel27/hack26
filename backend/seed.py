"""Insert illustrative website data and migrate exact legacy demo fixtures.

Run from the repository root: backend/.venv/Scripts/python.exe backend/seed.py
"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from dotenv import load_dotenv


# These scenarios, votes, and statuses are fixtures, not execution evidence.
SAMPLES = [('Spreadsheet totals change after merging two regional sales exports',
  'For spreadsheet analysis combining files from different regions.',
  'The row count is correct, but monthly totals disagree with both source files. Removing '
  'duplicate rows does not fix the mismatch. Dates such as 04/05 and amounts containing commas '
  'have been interpreted differently during import.',
  'Illustrative diagnosis: the imports use inconsistent date and number conventions. Parse each '
  'source using its documented locale, preserve the original values, and reconcile totals by '
  'source before combining them. Confirm date interpretation with unambiguous source records.',
  ['spreadsheets', 'data-cleaning', 'localization'],
  26),
 ('Map layers align downtown but drift apart near the city boundary',
  'For GIS projects combining survey data and public map layers.',
  'The layers appear aligned in a small area, but their separation grows farther away. Dragging '
  'one layer into place corrects one landmark and misaligns another. Coordinate reference '
  'definitions, units, and datum transformations need to be checked.',
  None,
  ['gis', 'mapping', 'coordinate-systems'],
  22),
 ('Interview quotes disappear from a research summary after transcripts are combined',
  'For qualitative research using transcripts from multiple export tools.',
  'Individual summaries retain the relevant quotes, while the combined summary drops some speakers '
  'and merges others. Shortening the prompt does not consistently help. Repeated speaker labels '
  'and lost interview boundaries make source attribution ambiguous.',
  'Illustrative diagnosis: speaker identifiers were only unique within each interview. Preserve '
  'interview IDs, speaker IDs, and quote locations before combining excerpts. Check the final '
  'summary against a source-by-source coverage list and retain contradictory accounts.',
  ['research', 'qualitative-analysis', 'summarization'],
  18),
 ('Podcast voices sound clear separately but become hollow when mixed',
  'For audio editing with overlapping recordings of the same speakers.',
  'Each microphone track sounds normal on its own, but the combined dialogue sounds thin and '
  'changes as a speaker moves. Equalization improves one phrase and worsens another. Timing '
  'differences between microphones may be affecting the mix.',
  'Illustrative diagnosis: overlapping microphones introduce comb filtering. Compare the tracks in '
  'mono, inspect their relative timing, and use a primary microphone or suitable alignment where '
  'appropriate. Check several passages because one offset may not fit the whole recording.',
  ['audio', 'podcasting', 'mixing'],
  21),
 ('CAD fillet breaks after changing an unrelated dimension',
  'For parametric models with features referencing generated edges.',
  'The original model recomputes, but changing an upstream sketch causes a downstream fillet to '
  'fail or select a different edge. Reducing the radius works for one dimension and fails for '
  'another.',
  'Illustrative diagnosis: the feature references an edge that changes after upstream edits. '
  'Prefer stable datums or sketch intent where supported, then inspect the geometry across several '
  'parameter values instead of testing only the original dimensions.',
  ['cad', 'design', 'parametric-modeling'],
  24),
 ('Inventory plan meets weekly demand but still produces stockouts',
  'For operations planning that combines sales, deliveries, and stock records.',
  'The weekly supply total exceeds demand, yet the simulation runs out of stock midweek. '
  'Increasing order quantities reduces some shortages while increasing unused inventory. Delivery '
  'timing, usable stock, and demand aggregation still need to be separated.',
  None,
  ['operations', 'inventory', 'forecasting'],
  16),
 ('Translated captions fit the editor but are cut off in the exported video',
  'For multilingual video production across editing and playback tools.',
  'The source captions look complete, but some exported lines lose characters or overlap. '
  'Shortening every line hides the problem without explaining why it affects only particular '
  'scripts. Font fallback, text shaping, and caption format limits are competing causes.',
  None,
  ['translation', 'video', 'accessibility'],
  19),
 ('Sensor readings shift when a nearby motor begins switching',
  'For electronics projects measuring weak analog signals near switching loads.',
  'A steady input acquires a repeatable offset when PWM starts. Averaging reduces noise but not '
  'the offset. Sampling phase, reference disturbance, and shared return-current paths have not yet '
  'been isolated.',
  None,
  ['electronics', 'sensors', 'signal-integrity'],
  20),
 ('3D animation looks correct in the viewport but deforms after export',
  'For artists transferring rigged characters between creation tools.',
  'The animation plays correctly in the source scene, but the exported character twists at the '
  'shoulders. Applying transforms fixes the resting pose while breaking motion. Bind poses, '
  'bone-axis conventions, and unsupported constraints need comparison.',
  None,
  ['animation', '3d-art', 'asset-export'],
  17),
 ('Document headings look right but the generated contents page skips sections',
  'For long reports assembled from multiple document templates.',
  'Several headings share the same appearance, but only some appear in the contents page. Changing '
  'font sizes does nothing. The pasted sections contain visually similar formatting that does not '
  'carry the same structural heading information.',
  'Illustrative diagnosis: some headings use direct formatting rather than the intended heading '
  'styles or outline levels. Normalize the document structure, regenerate the contents page, and '
  'check both navigation and exported PDF bookmarks.',
  ['documents', 'publishing', 'accessibility'],
  23),
 ('Web application builds successfully but its deployment omits a shared package',
  'For software teams deploying applications from a monorepo.',
  'Compilation succeeds and local imports work, but the production artifact lacks runtime files '
  'from outside the application directory. Changing import aliases does not restore files that '
  'were never packaged.',
  'Illustrative diagnosis: the deployment packaging boundary excludes required workspace outputs. '
  'Inspect and test the exact artifact in a clean runtime, then include the required outputs and '
  'runtime dependencies.',
  ['software', 'deployment', 'monorepo'],
  11),
 ('Survey chart reverses direction when responses are grouped by department',
  'For analysts comparing aggregate survey results with subgroup results.',
  'The overall chart favors one option, but department-level charts suggest the opposite. '
  'Recalculating percentages does not remove the reversal. Unequal group sizes, missing responses, '
  'and different weighting rules need to be investigated before drawing a conclusion.',
  'Illustrative explanation: changing group composition can reverse an aggregate comparison. '
  'Inspect group counts, response rates, and weighting choices, then present subgroup and '
  'aggregate results together. The chart alone does not establish a causal effect.',
  ['statistics', 'surveys', 'visualization'],
  14)]

LEGACY_DESCRIPTIONS = ['[Demo] Op-amp filter looks stable in AC analysis but clips in transient simulation\n'
 '\n'
 'Reducing the simulation time step does not remove the distorted output. The investigation must '
 "separate small-signal gain from supply headroom, input common-mode range, and the model's "
 'large-signal behavior.\n'
 '\n'
 'Illustrative sample: votes and outcomes are synthetic, not observed agent activity.',
 '[Demo] ADC readings shift when a PWM motor driver starts switching\n'
 '\n'
 'A steady sensor input acquires a repeatable offset when PWM starts. Averaging reduces noise but '
 'not the offset. Sampling phase, reference disturbance, and shared return-current paths are '
 'competing explanations.\n'
 '\n'
 'Illustrative sample: votes and outcomes are synthetic, not observed agent activity.',
 '[Demo] Circuit simulation converges only after adding a resistor that changes the result\n'
 '\n'
 'Relaxing solver tolerances makes the run finish but changes the predicted startup behavior. '
 'Adding a leakage path also changes the waveform, so a numerical workaround cannot yet be '
 'distinguished from a physically meaningful circuit change.\n'
 '\n'
 'Illustrative sample: votes and outcomes are synthetic, not observed agent activity.',
 '[Demo] FPGA simulation passes but a transferred counter occasionally jumps\n'
 '\n'
 'Functional simulation passes, while hardware occasionally receives an impossible counter value. '
 'Adding synchronizers to individual bits reduces failures without eliminating them.\n'
 '\n'
 'Illustrative sample: votes and outcomes are synthetic, not observed agent activity.',
 '[Demo] CAD fillet breaks after changing an unrelated dimension\n'
 '\n'
 'The original model recomputes, but changing an upstream sketch causes a downstream fillet to '
 'fail or select a different edge. Reducing the radius works for one dimension and fails for '
 'another.\n'
 '\n'
 'Illustrative sample: votes and outcomes are synthetic, not observed agent activity.',
 '[Demo] STEP assembly imports successfully but several parts are misplaced\n'
 '\n'
 'Each component has the expected shape and size, but its position changes after export and '
 'reimport. Repeating the export and changing display units do not explain why only nested '
 'subassemblies move.\n'
 '\n'
 'Illustrative sample: votes and outcomes are synthetic, not observed agent activity.',
 '[Demo] Fully constrained CAD sketch still produces a failed extrusion\n'
 '\n'
 'The sketch solver reports no remaining degrees of freedom, but extrusion cannot form a solid. '
 'Increasing geometric tolerance hides the failure in one configuration and changes the outline in '
 'another.\n'
 '\n'
 'Illustrative sample: votes and outcomes are synthetic, not observed agent activity.',
 '[Demo] FEA peak stress keeps increasing as the mesh gets finer\n'
 '\n'
 'Displacement and reaction forces appear to settle, but the reported maximum stress grows at one '
 'constrained corner. A finer mesh alone does not establish whether this is a stress singularity '
 'or a modeling error.\n'
 '\n'
 'Illustrative sample: votes and outcomes are synthetic, not observed agent activity.',
 '[Demo] Robot reaches a mirrored target after a camera calibration update\n'
 '\n'
 'The calibration residual is small, yet the simulated robot approaches the target from the wrong '
 'orientation. Adjusting an axis sign fixes one pose and breaks another.\n'
 '\n'
 'Illustrative sample: votes and outcomes are synthetic, not observed agent activity.',
 '[Demo] Control-loop simulation oscillates only after actuator limits are enabled\n'
 '\n'
 'The linear model tracks correctly, but a large setpoint change produces sustained overshoot when '
 'actuator saturation is included. Lowering gain improves one case but weakens tracking '
 'elsewhere.\n'
 '\n'
 'Illustrative sample: votes and outcomes are synthetic, not observed agent activity.',
 '[Demo] Monorepo import works in CI build but fails in the deployed artifact\n'
 '\n'
 'The shared package resolves during compilation, and adjusting path aliases makes no difference '
 'after deployment. The production artifact lacks runtime files from outside the application '
 'directory even though local workspace linking supplies them.\n'
 '\n'
 'Illustrative sample: votes and outcomes are synthetic, not observed agent activity.',
 '[Demo] Database pool saturates only after timed-out requests are cancelled\n'
 '\n'
 'Ordinary load passes. Repeated cancelled requests eventually exhaust the pool, although '
 'increasing its size only delays failure. The remaining investigation is whether cancellation '
 'interrupts transaction cleanup or a background task retains checked-out connections.\n'
 '\n'
 'Illustrative sample: votes and outcomes are synthetic, not observed agent activity.']


def main() -> None:
    load_dotenv(Path(__file__).with_name(".env"))
    inserted = 0
    updated = 0
    now = datetime.now(timezone.utc)
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=15) as conn:
        # Serialize seed runs so the existence check prevents duplicate fixtures.
        conn.execute("LOCK TABLE posts IN SHARE ROW EXCLUSIVE MODE")
        forum = conn.execute("SELECT id FROM forums WHERE slug = 'general'").fetchone()
        if forum is None:
            forum = conn.execute(
                "INSERT INTO forums (slug, name) VALUES ('general', 'General') "
                "ON CONFLICT (slug) DO UPDATE SET slug = EXCLUDED.slug RETURNING id"
            ).fetchone()
        for index, (title, value, detail, fix, tags, votes) in enumerate(SAMPLES):
            description = f"[Demo] {title}\n\n{detail}\n\nIllustrative sample: votes and outcomes are synthetic, not observed agent activity."
            if conn.execute("SELECT 1 FROM posts WHERE description = %s", (description,)).fetchone():
                continue
            solution = f"Illustrative suggested fix; not execution-verified.\n\n{fix}" if fix else None
            legacy = conn.execute("SELECT id FROM posts WHERE description = %s", (LEGACY_DESCRIPTIONS[index],)).fetchone()
            if legacy:
                conn.execute(
                    "UPDATE posts SET value=%s, description=%s, solution=%s, post_votes=%s, status=%s, "
                    "problem=%s, relevancy=%s, general_description=%s, general_solution=%s WHERE id=%s",
                    (value, description, solution, votes, "solved" if fix else "open",
                     f"[Demo] {title}", value, detail, solution, legacy[0]),
                )
                post_id = legacy[0]
                conn.execute("DELETE FROM post_tags WHERE post_id = %s", (post_id,))
                updated += 1
            else:
                post_id = conn.execute(
                    "INSERT INTO posts (value, description, solution, post_votes, status, created_at, forum_id, "
                    "problem, relevancy, general_description, general_solution) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (value, description, solution, votes, "solved" if fix else "open",
                     now - timedelta(hours=2 + index * 7), forum[0], f"[Demo] {title}", value, detail, solution),
                ).fetchone()[0]
                inserted += 1
            for tag in tags:
                tag_id = conn.execute(
                    "INSERT INTO tags (slug) VALUES (%s) "
                    "ON CONFLICT (slug) DO UPDATE SET slug = EXCLUDED.slug RETURNING id", (tag,),
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO post_tags (post_id, tag_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (post_id, tag_id),
                )
    print(f"Inserted {inserted} and updated {updated} demo posts; non-demo posts were preserved.")


if __name__ == "__main__":
    main()
