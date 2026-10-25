"""A stand-in for `claude -p`, so the local tester can be tested and demoed without real Claude sessions.

The tester runs it when env AGENTOVERFLOW_TESTER_CLAUDE points at it, e.g.
    AGENTOVERFLOW_TESTER_CLAUDE='["python", "plugin/hook/dev/fake_claude.py"]'

It reads the prompt on stdin (like `claude -p`), finds which job it is ("Hypothesis <n>" or the reproduce-it
test), and does what the plan file says. The plan file is env AGENTOVERFLOW_FAKE_PLAN, a JSON object:
    {"1": {"files": {"cart.py": "new text"}, "reply": "changed cart.py", "sleep": 0},
     "repro": {"files": {"tests/test_repro.py": "..."}, "reply": "wrote it\nCHECK: python -m pytest tests/test_repro.py"}}
Files are written relative to the working directory (the copy). If env AGENTOVERFLOW_FAKE_LOG is set, one JSON
line per call is appended to it: argv, cwd, the specialist flag, the prompt, and (plan key "peek": [paths])
what those files held in the copy before any change, plus which folders in the copy are links.
It prints a result shaped like `claude -p --output-format json`.
"""

import json
import os
import re
import sys
import time
from pathlib import Path


def main() -> int:
    prompt = sys.stdin.read()
    match = re.search(r"^Hypothesis (\d+)", prompt, re.MULTILINE)
    job = match.group(1) if match else ("repro" if "reproduces the problem" in prompt else "?")
    plan_path = os.environ.get("AGENTOVERFLOW_FAKE_PLAN")
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8")) if plan_path else {}
    step = plan.get(job, {})
    log = os.environ.get("AGENTOVERFLOW_FAKE_LOG")
    peek = {}
    for rel in plan.get("peek") or []:
        try:
            peek[rel] = Path(rel).read_text(encoding="utf-8")
        except OSError:
            peek[rel] = None
    links = {p.name: p.is_junction() or p.is_symlink() for p in Path(".").iterdir() if p.is_dir()}
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"job": job, "argv": sys.argv[1:], "cwd": os.getcwd(), "peek": peek, "links": links,
                                "specialist": os.environ.get("AGENTOVERFLOW_SPECIALIST"),
                                "project_dir_env": os.environ.get("CLAUDE_PROJECT_DIR"), "prompt": prompt}) + "\n")
    time.sleep(float(step.get("sleep", 0)))
    for rel, text in (step.get("files") or {}).items():
        path = Path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
                      "total_cost_usd": 0.0, "result": step.get("reply", "done")}))
    return int(step.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main())
