"""Run the whole team on one case file and print what happened, how long each stage took, and the cost.

From the repo root:
    python -m backend.team.cli backend/team/samples/prisma_alpine_docker.json
    python -m backend.team.cli case.json --offline          # fake models: no keys, no cost
    python -m backend.team.cli case.json --models chair=claude-sonnet-5-5 --json out.json

Keys come from the environment or backend/.env (ANTHROPIC_API_KEY, optional OPENAI_API_KEY).
This calls the real APIs and costs real money (about $0.30-1.00 per run; capped by TEAM_RUN_COST_CAP_USD).
The gate's forum search runs only with --search (it needs a reachable DATABASE_URL).
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from . import case_file, config, gate, report, runner
from .store import MemoryStore


def _parse_models(text: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (text or "").split(","):
        if "=" in part:
            seat, model = part.split("=", 1)
            out[seat.strip()] = model.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m backend.team.cli", description=__doc__.split("\n")[0])
    ap.add_argument("case", help="path to a case file (JSON, docs/api-endpoints.md #9)")
    ap.add_argument("--offline", action="store_true", help="use fake models (no API calls, no cost)")
    ap.add_argument("--no-gate", action="store_true", help="skip the gate and go straight to the team")
    ap.add_argument("--search", action="store_true", help="let the gate search the forum (needs DATABASE_URL)")
    ap.add_argument("--models", help="seat overrides, e.g. docs=claude-opus-5-5,chair=claude-sonnet-5-5")
    ap.add_argument("--json", help="also write the full report as JSON to this path")
    ap.add_argument("--quiet", action="store_true", help="don't print events as they happen")
    args = ap.parse_args(argv)

    raw = json.loads(Path(args.case).read_text(encoding="utf-8"))
    case = case_file.normalize(raw)
    s = config.settings()
    if args.offline:
        from .fakes import FakeAnthropic, FakeOpenAI

        clients = config.Clients(anthropic=FakeAnthropic(delays={"docs": 0.4, "issues": 0.6, "chair": 0.3}),
                                 openai=FakeOpenAI(delay=0.5))
    else:
        clients = config.make_clients()
        if clients.anthropic is None:
            print("ANTHROPIC_API_KEY is not set (environment or backend/.env). Use --offline for a dry run.",
                  file=sys.stderr)
            return 2
    models = _parse_models(args.models)
    print(f"Case: {Path(args.case).name} ({len(case_file.render(case))} chars). "
          f"Seats: {json.dumps(config.models_for(models))}")
    if clients.openai is None:
        print("OPENAI_API_KEY not set: the community seat will run on "
              f"{config.models_for(models)['community_fallback']} instead.")

    t0 = time.monotonic()
    gate_s = None
    if not args.no_gate:
        g0 = time.monotonic()
        decision = gate.decide(case, store=MemoryStore(), clients=clients, search_fn=gate.forum_search if args.search else None,
                               settings=s, models=models)
        gate_s = round(time.monotonic() - g0, 2)
        print(f"\n[gate {gate_s}s] {decision.decision}: {decision.reason} (checks: {', '.join(decision.checks)})")
        if decision.decision != "team":
            print(decision.message)
            return 0

    def on_event(member: str, kind: str, text: str, data: dict[str, Any] | None = None) -> None:
        if not args.quiet:
            print(f"  {time.monotonic() - t0:6.1f}s  {member:<9} {kind:<13} {text[:110]}")

    print("\nTeam run:")
    rep = runner.run_team(case, on_event, models, clients=clients, settings=s)
    print("\n" + "=" * 78)
    print(report.render_report("cli", rep))
    print("=" * 78)

    t = rep.get("timings", {})
    print("\nTiming (seconds):")
    if gate_s is not None:
        print(f"  gate          {gate_s:>6}")
    for seat in config.RESEARCHER_SEATS:
        if f"{seat}_s" in t:
            print(f"  {seat:<13} {t[f'{seat}_s']:>6}   ({rep.get('models', {}).get(seat, '?')})")
    print(f"  researchers   {t.get('researchers_s', '-'):>6}   (parallel: the slowest one)")
    print(f"  chair         {t.get('chair_s', '-'):>6}   ({rep.get('models', {}).get('chair', '?')})")
    print(f"  team total    {t.get('total_s', '-'):>6}   (target {int(s.target_s)}s, hard stop {int(s.hard_stop_s)}s)")
    print("\nCost (USD):")
    for member, row in (rep.get("costs") or {}).items():
        print(f"  {member:<13} ${row['cost_usd']:<8} {row['model']:<20} in={row['input_tokens']} "
              f"out={row['output_tokens']} searches={row['web_searches']} fetches={row['web_fetches']}")
    print(f"  total         ${rep.get('cost_usd')}   (cap ${s.run_cost_cap_usd})")
    print(f"\nStatus: {rep['status']}" + (f" ({rep.get('error')})" if rep.get("error") else ""))
    if args.json:
        Path(args.json).write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
        print(f"Report written to {args.json}")
    return 0 if rep["status"] != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
