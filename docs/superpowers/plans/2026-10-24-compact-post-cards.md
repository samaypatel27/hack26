# Compact post cards

The user selected the main-card-and-sidebar layout after reviewing both alternatives.

Design: widen the detail page to 1120px. At desktop widths, use a flexible main column
and a 288px sidebar. The main card contains the problem, author, description, and
general solution, separated by subtle borders. Sidebar cards contain usage counts,
relevancy, and tags. Replies become individual compact cards under the question.
At smaller widths, retain the same reading order with the sidebar below the main card.
Keep all content visible without clipping or nested scroll areas. Preserve existing
colors, Markdown safety, data loading, links, pagination, and API behavior.

Implementation:
- [x] Restyle PostView into responsive cards with tighter spacing and smaller headings.
- [x] Restyle reply list cards; put explanatory text in an accessible details disclosure.
- [x] Scope compact Markdown spacing to the post page so other pages are unchanged.
- [x] Verify frontend lint, production build, and inspect the diff for preserved states.

This is a presentation-only change. No database, API, agent identity, or count changes.

Verification: frontend lint and production build pass, including TypeScript and
prerendering. Post page HTTP check returns 200. Browser visual verification is
unavailable in this environment. No commits made; prior feature work preserved.
