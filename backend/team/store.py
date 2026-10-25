"""Where team runs and their board-room events are kept: in memory, in this process.

The real database has no team tables (agent_team_runs / agent_team_events were never created), so runs are
kept in a dict and lost on restart. That is enough for the demo: a run lasts a few minutes and the website
polls GET /api/help/{run_id} while it is running. Nothing here touches the database.
"""

import logging
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("team.store")

STALE_GRACE_S = 15          # a "running" row this long past its deadline is marked failed
STALE_ERROR = "the run did not finish before its deadline (server restarted or timed out)"
EVENT_LIMIT = 500


def new_run_id() -> str:
    return "tr_" + secrets.token_hex(6)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _start_of_day(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


class MemoryStore:
    kind = "memory"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, dict[str, Any]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._next_event = 1

    def create_run(self, run_id: str, *, case: dict[str, Any], models: dict[str, str], source: str,
                   deadline_s: float, open_question_id: int | None = None) -> None:
        now = _now()
        with self._lock:
            self._runs[run_id] = {
                "id": run_id, "status": "running", "source": source, "fingerprint": case.get("fingerprint"),
                "project": case.get("project"), "open_question_id": open_question_id, "models": models,
                "case_file": case, "report": None, "cost_usd": 0.0, "error": None, "created_at": now,
                "started_at": None, "finished_at": None, "deadline_at": now + timedelta(seconds=deadline_s),
            }
            self._events[run_id] = []

    def mark_started(self, run_id: str, deadline_s: float) -> None:
        now = _now()
        with self._lock:
            run = self._runs.get(run_id)
            if run:
                run["started_at"] = now
                run["deadline_at"] = now + timedelta(seconds=deadline_s)

    def finish_run(self, run_id: str, status: str, report: dict[str, Any] | None, error: str | None) -> None:
        with self._lock:
            run = self._runs.get(run_id)
            if run and run["status"] == "running":
                run.update(status=status, report=report, error=error, finished_at=_now(),
                           cost_usd=float((report or {}).get("cost_usd") or 0.0))

    def add_event(self, run_id: str, member: str, kind: str, text: str, data: dict[str, Any] | None) -> None:
        with self._lock:
            if run_id not in self._events:
                return
            self._events[run_id].append({"id": self._next_event, "member": member, "type": kind,
                                         "text": (text or "")[:1000], "data": data, "t": _iso(_now())})
            self._next_event += 1

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self._runs.get(run_id)
            return dict(run) if run else None

    def list_events(self, run_id: str, since: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(e) for e in self._events.get(run_id, []) if e["id"] > since][:EVENT_LIMIT]

    def count_runs_today(self) -> int:
        start = _start_of_day(_now())
        with self._lock:
            return sum(1 for r in self._runs.values() if r["created_at"] >= start)

    def find_active_run(self, fingerprint: str) -> str | None:
        now = _now()
        with self._lock:
            for r in self._runs.values():
                if r["status"] == "running" and r["fingerprint"] == fingerprint and r["deadline_at"] > now:
                    return r["id"]
        return None

    def expire_stale(self) -> int:
        cutoff = _now() - timedelta(seconds=STALE_GRACE_S)
        count = 0
        with self._lock:
            for r in self._runs.values():
                if r["status"] == "running" and r["deadline_at"] < cutoff:
                    r.update(status="failed", error=STALE_ERROR, finished_at=_now())
                    count += 1
        return count


_store: Any = None
_store_lock = threading.Lock()


def choose_store() -> Any:
    """The in-memory store (the only one). Logs one line, once per process."""
    log.info("team: runs are kept in memory and lost on restart")
    return MemoryStore()


def get_store() -> Any:
    global _store
    with _store_lock:
        if _store is None:
            _store = choose_store()
        return _store


def set_store(store: Any) -> None:
    """Replace the process-wide store (tests)."""
    global _store
    with _store_lock:
        _store = store
