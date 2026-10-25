# Broader, extensible tags

User requested tags covering the interdisciplinary sample topics and growth as new
posts arrive. Keep forums unchanged; expand starter taxonomy, restore exact sample
tags, and accept new valid tags from authenticated post creation.

- Normalize spaces/underscores to hyphens, lowercase, deduplicate, max four tags.
- Valid slugs: 1–48 characters, first letter/digit, then letters/digits/.-+#.
- General is only a fallback when no valid specific tags remain.
- Register tags and associate them to the new post and chosen forum in one transaction.
- Existing-post dedup/solution attachment does not retag the original question.
- Repair samples by full fixture description equality; remove only the general
  placeholder after adding specific tags. Preserve other existing tags and all content,
  usage, and replies. Seed script is idempotent and can print SQL for Supabase MCP.
- taxonomy.json is suggested starter vocabulary, not an exhaustive allowlist.
- Test normalization, cross-domain/technical tag names, caps, database uniqueness,
  forum links, replay, API contracts and plugin guidance. No schema changes needed.

Completed: 56 backend tests and 26 plugin tests pass. Four isolated database checks
pass, plus the separate seed repair/replay check. Applied seed SQL through Supabase
MCP and verified the live API exposes specific sample tags and updated forum filters.
Catalog now contains 95 tags. All 68 posts retain their content checksum
`8f5ac307fa744f4186986599560d6d6e`; 10 replies, 50 visits and 40 useful marks preserved.
No frontend edits, schema migrations, commits or pushes.
