"""Models per seat, prices, and the TEAM_* settings (read from the environment / backend/.env).

Settings are read every time `settings()` is called, so a changed env var (or a test's
monkeypatch) takes effect without restarting anything.
"""

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
except ImportError:  # python-dotenv is in requirements.txt; keep working without it
    pass

# ---------------------------------------------------------------------------
# Who sits in which seat (PROJECT_PLAN.md, "The backup team" > "Members")
# ---------------------------------------------------------------------------

DEFAULT_MODELS: dict[str, str] = {
    "gate": "claude-haiku-5-5",
    "docs": "claude-sonnet-5-5",
    "issues": "claude-sonnet-5-5",
    "community": "gpt-6.1-sol",                 # second provider: OpenAI Responses API + web_search
    "community_fallback": "claude-sonnet-5-5",  # used when OPENAI_API_KEY is missing or the call fails
    "chair": "claude-opus-5-5",
}
RESEARCHER_SEATS = ("docs", "issues", "community")


def is_openai_model(model: str) -> bool:
    return model.startswith(("gpt-", "o1", "o3", "o4", "o5", "chatgpt-"))


def models_for(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """The default seats with `overrides` applied (unknown seat names are ignored)."""
    models = dict(DEFAULT_MODELS)
    for seat, model in (overrides or {}).items():
        if seat in models and isinstance(model, str) and model.strip():
            models[seat] = model.strip()
    return models


def override_for(model: str) -> dict[str, str]:
    """Seat overrides for a cold-case re-run with one chosen model (POST /v1/rerun).

    A Claude model takes every Claude seat (docs, issues, community fallback, chair); an OpenAI
    model takes the community seat. The gate never runs on a re-run.
    """
    model = model.strip()
    if is_openai_model(model):
        return {"community": model}
    return {"docs": model, "issues": model, "community_fallback": model, "chair": model}


# ---------------------------------------------------------------------------
# Prices (USD per 1M tokens: input, output). Checked 2026-10-24.
# ---------------------------------------------------------------------------

PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-fable-5-1": (10.00, 50.00),
    "gpt-6.1-sol": (2.00, 10.00),
}
UNKNOWN_MODEL_PRICE = (10.00, 50.00)   # be pessimistic about models we have no price for
CACHE_READ_FACTOR = 0.10               # cache reads cost ~10% of the input price
CACHE_WRITE_FACTOR = 1.25              # 5-minute cache writes cost 1.25x the input price
CLAUDE_WEB_SEARCH_USD = 10.00 / 1000   # $10 per 1,000 searches; web fetch has no per-call fee
# OpenAI's pricing page (checked 2026-10-24): "Web search (all models): $10.00 / 1k calls + search
# content tokens billed at model rates". The content tokens already show up in usage.input_tokens.
# We count one call per `search` action; whether open_page/find_in_page actions are billed as
# calls is not stated, so the real bill can be slightly higher.
OPENAI_WEB_SEARCH_USD = 10.00 / 1000

# Server web tools with dynamic filtering need one of these models; older ones get the basic tools.
DYNAMIC_FILTER_MODELS = {
    "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
    "claude-sonnet-5-5", "claude-sonnet-5", "claude-sonnet-4-6", "claude-fable-5", "claude-fable-5-1",
}
# Models that take output_config.effort (Haiku 4.5 and older reject it).
EFFORT_MODELS = DYNAMIC_FILTER_MODELS | {"claude-haiku-5-5"}
# Models with server-side refusal fallback (`fallbacks: "default"`, beta server-side-fallback-2026-07-01).
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5", "claude-fable-5-1"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def price(model: str) -> tuple[float, float]:
    return PRICES.get(model, UNKNOWN_MODEL_PRICE)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class Settings:
    enabled: bool                 # TEAM_ENABLED: kill switch for the whole team
    daily_runs: int               # TEAM_DAILY_RUNS: team runs per UTC day, all users together
    run_cost_cap_usd: float       # TEAM_RUN_COST_CAP_USD: a run stops spending at this
    target_s: float               # TEAM_TARGET_S: what we aim for (shown to the agent)
    hard_stop_s: float            # TEAM_HARD_STOP_S: a run is over after this, whatever happens
    researcher_timeout_s: float   # TEAM_RESEARCHER_TIMEOUT_S: each researcher gets this long
    gate_timeout_s: float         # TEAM_GATE_TIMEOUT_S: the gate's Haiku call
    gate_search_timeout_s: float  # TEAM_GATE_SEARCH_TIMEOUT_S: the gate's forum search
    max_concurrent_runs: int      # TEAM_MAX_CONCURRENT_RUNS: team runs at once in this process
    researcher_effort: str        # TEAM_RESEARCHER_EFFORT: low | medium | high
    chair_effort: str             # TEAM_CHAIR_EFFORT
    search_max_uses: int          # TEAM_SEARCH_MAX_USES: web searches per researcher
    fetch_max_uses: int           # TEAM_FETCH_MAX_USES: page reads per Claude researcher
    min_confidence: float         # TEAM_MIN_CONFIDENCE: below this for every hypothesis -> no_answer


def settings() -> Settings:
    return Settings(
        enabled=_bool("TEAM_ENABLED", True),
        daily_runs=_int("TEAM_DAILY_RUNS", 50),
        run_cost_cap_usd=_float("TEAM_RUN_COST_CAP_USD", 1.50),
        target_s=_float("TEAM_TARGET_S", 60),
        hard_stop_s=_float("TEAM_HARD_STOP_S", 120),
        researcher_timeout_s=_float("TEAM_RESEARCHER_TIMEOUT_S", 60),
        gate_timeout_s=_float("TEAM_GATE_TIMEOUT_S", 3.0),
        gate_search_timeout_s=_float("TEAM_GATE_SEARCH_TIMEOUT_S", 2.0),
        max_concurrent_runs=max(1, _int("TEAM_MAX_CONCURRENT_RUNS", 4)),
        researcher_effort=os.environ.get("TEAM_RESEARCHER_EFFORT", "") or "low",
        chair_effort=os.environ.get("TEAM_CHAIR_EFFORT", "") or "high",
        search_max_uses=max(1, _int("TEAM_SEARCH_MAX_USES", 4)),
        fetch_max_uses=max(0, _int("TEAM_FETCH_MAX_USES", 3)),
        min_confidence=_float("TEAM_MIN_CONFIDENCE", 0.4),
    )


# ---------------------------------------------------------------------------
# API clients (keys only ever come from the environment / backend/.env)
# ---------------------------------------------------------------------------

@dataclass
class Clients:
    anthropic: object | None = None
    openai: object | None = None


def make_clients() -> Clients:
    """Real SDK clients for the keys that are set. A missing key leaves that client as None."""
    clients = Clients()
    if os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic

        clients.anthropic = anthropic.Anthropic(max_retries=1)
    if os.environ.get("OPENAI_API_KEY"):
        try:
            import openai

            clients.openai = openai.OpenAI(max_retries=1)
        except ImportError:   # `openai` not installed: the community seat falls back to Claude
            clients.openai = None
    return clients
