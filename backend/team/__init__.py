"""The specialist team: gate, three independent researchers, and a chair (docs/specialist-team.md).

Entry points:
    team.gate.decide(...)        quick yes/no inside POST /api/help
    team.runner.run_team(...)    the full team run (researchers in parallel, then the chair)
    python -m backend.team.cli   run a whole team on a case file and print timing + cost

The rest of backend/ imports modules by their bare names (`import scrub`), because the server
runs from inside backend/. Put backend/ on sys.path here so `team` also works when imported as
`backend.team` (the CLI is run from the repo root).
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
