"""Token and dollar accounting per team member, shared by threads, with the per-run cost cap."""

import threading
from typing import Any

from . import config


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class CostMeter:
    """Adds up usage from every API call in one run. `exceeded` is True once the cap is reached."""

    def __init__(self, cap_usd: float):
        self.cap_usd = cap_usd
        self._lock = threading.Lock()
        self._members: dict[str, dict[str, Any]] = {}

    def _row(self, member: str, model: str) -> dict[str, Any]:
        row = self._members.get(member)
        if row is None:
            row = {"model": model, "calls": 0, "input_tokens": 0, "output_tokens": 0,
                   "cache_read_tokens": 0, "cache_write_tokens": 0, "web_searches": 0,
                   "web_fetches": 0, "cost_usd": 0.0}
            self._members[member] = row
        row["model"] = model
        return row

    def add_claude(self, member: str, model: str, usage: Any) -> float:
        """Record one Anthropic response's `usage`. Returns that call's cost in USD."""
        inp = int(_get(usage, "input_tokens", 0) or 0)
        out = int(_get(usage, "output_tokens", 0) or 0)
        cache_read = int(_get(usage, "cache_read_input_tokens", 0) or 0)
        cache_write = int(_get(usage, "cache_creation_input_tokens", 0) or 0)
        server = _get(usage, "server_tool_use")
        searches = int(_get(server, "web_search_requests", 0) or 0)
        fetches = int(_get(server, "web_fetch_requests", 0) or 0)
        p_in, p_out = config.price(model)
        cost = (inp * p_in + out * p_out + cache_read * p_in * config.CACHE_READ_FACTOR
                + cache_write * p_in * config.CACHE_WRITE_FACTOR) / 1_000_000
        cost += searches * config.CLAUDE_WEB_SEARCH_USD
        with self._lock:
            row = self._row(member, model)
            row["calls"] += 1
            row["input_tokens"] += inp
            row["output_tokens"] += out
            row["cache_read_tokens"] += cache_read
            row["cache_write_tokens"] += cache_write
            row["web_searches"] += searches
            row["web_fetches"] += fetches
            row["cost_usd"] += cost
        return cost

    def add_openai(self, member: str, model: str, usage: Any, searches: int) -> float:
        """Record one OpenAI Responses API call. Cached input is billed at the cached rate."""
        inp = int(_get(usage, "input_tokens", 0) or 0)
        out = int(_get(usage, "output_tokens", 0) or 0)
        cached = int(_get(_get(usage, "input_tokens_details"), "cached_tokens", 0) or 0)
        p_in, p_out = config.price(model)
        cost = ((inp - cached) * p_in + cached * p_in * config.CACHE_READ_FACTOR + out * p_out) / 1_000_000
        cost += searches * config.OPENAI_WEB_SEARCH_USD
        with self._lock:
            row = self._row(member, model)
            row["calls"] += 1
            row["input_tokens"] += inp
            row["output_tokens"] += out
            row["cache_read_tokens"] += cached
            row["web_searches"] += searches
            row["cost_usd"] += cost
        return cost

    @property
    def total(self) -> float:
        with self._lock:
            return sum(r["cost_usd"] for r in self._members.values())

    @property
    def exceeded(self) -> bool:
        return self.total >= self.cap_usd

    def member_cost(self, member: str) -> float:
        with self._lock:
            row = self._members.get(member)
            return row["cost_usd"] if row else 0.0

    def summary(self) -> dict[str, dict[str, Any]]:
        """{member: {model, calls, tokens..., cost_usd}} with costs rounded to 4 decimals."""
        with self._lock:
            return {m: {**r, "cost_usd": round(r["cost_usd"], 4)} for m, r in self._members.items()}
