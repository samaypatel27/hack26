"""Where team runs and their board-room events are kept.

Two stores with the same methods:
  PostgresStore  the tables in backend/schema_team_v1.sql (v1_team_runs, v1_team_events), used when
                 DATABASE_URL is set AND those tables already exist.
  MemoryStore    a dict in this process. Used when there is no DATABASE_URL or the tables are missing,
                 so local testing and the demo still work. Runs are lost on restart.

This module NEVER creates or changes tables. The backend owner applies schema_team_v1.sql by hand.
"""

import logging
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("team.store")

TABLES = ("v1_team_runs", "v1_team_events")
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


class PostgresStore:
    kind = "postgres"

    def __init__(self, url: str) -> None:
        self.url = url

    def _connect(self):  # noqa: ANN202
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self.url, row_factory=dict_row, prepare_threshold=None, connect_timeout=5)

    @staticmethod
    def _json(value: Any) -> Any:
        from psycopg.types.json import Jsonb

        return Jsonb(value) if value is not None else None

    def tables_exist(self) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT to_regclass('v1_team_runs') AS runs, to_regclass('v1_team_events') AS events"
                               ).fetchone()
        return bool(row and row["runs"] and row["events"])

    def create_run(self, run_id: str, *, case: dict[str, Any], models: dict[str, str], source: str,
                   deadline_s: float, open_question_id: int | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO v1_team_runs (id, status, source, fingerprint, project, open_question_id, models,"
                " case_file, deadline_at) VALUES (%s, 'running', %s, %s, %s, %s, %s, %s,"
                " now() + make_interval(secs => %s))",
                (run_id, source, case.get("fingerprint"), case.get("project"), open_question_id,
                 self._json(models), self._json(case), deadline_s),
            )

    def mark_started(self, run_id: str, deadline_s: float) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE v1_team_runs SET started_at = now(), deadline_at = now() + make_interval(secs => %s)"
                         " WHERE id = %s", (deadline_s, run_id))

    def finish_run(self, run_id: str, status: str, report: dict[str, Any] | None, error: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE v1_team_runs SET status = %s, report = %s, error = %s, cost_usd = %s, finished_at = now()"
                " WHERE id = %s AND status = 'running'",
                (status, self._json(report), error, float((report or {}).get("cost_usd") or 0.0), run_id),
            )

    def add_event(self, run_id: str, member: str, kind: str, text: str, data: dict[str, Any] | None) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO v1_team_events (run_id, member, type, text, data) VALUES (%s, %s, %s, %s, %s)",
                         (run_id, member, kind, (text or "")[:1000], self._json(data)))

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM v1_team_runs WHERE id = %s", (run_id,)).fetchone()

    def list_events(self, run_id: str, since: int = 0) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id, member, type, text, data, created_at FROM v1_team_events"
                                " WHERE run_id = %s AND id > %s ORDER BY id LIMIT %s",
                                (run_id, since, EVENT_LIMIT)).fetchall()
        return [{"id": r["id"], "member": r["member"], "type": r["type"], "text": r["text"], "data": r["data"],
                 "t": _iso(r["created_at"])} for r in rows]

    def count_runs_today(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT count(*) AS n FROM v1_team_runs WHERE created_at >= date_trunc('day', now()"
                               " AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'").fetchone()
        return int(row["n"])

    def find_active_run(self, fingerprint: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT id FROM v1_team_runs WHERE status = 'running' AND fingerprint = %s"
                               " AND deadline_at > now() ORDER BY created_at DESC LIMIT 1", (fingerprint,)).fetchone()
        return row["id"] if row else None

    def expire_stale(self) -> int:
        with self._connect() as conn:
            cur = conn.execute("UPDATE v1_team_runs SET status = 'failed', error = %s, finished_at = now()"
                               " WHERE status = 'running' AND deadline_at < now() - make_interval(secs => %s)",
                               (STALE_ERROR, STALE_GRACE_S))
            return cur.rowcount or 0


_store: Any = None
_store_lock = threading.Lock()


def choose_store() -> Any:
    """PostgresStore if DATABASE_URL is set and the team tables exist; otherwise MemoryStore (with a warning)."""
    url = os.environ.get("DATABASE_URL", "")
    if os.environ.get("TEAM_STORE", "").lower() == "memory" or not url:
        log.warning("team: no DATABASE_URL; runs are kept in memory and lost on restart")
        return MemoryStore()
    store = PostgresStore(url)
    try:
        if store.tables_exist():
            return store
        log.warning("team tables not found (%s); runs are kept in memory and lost on restart. "
                    "Apply backend/schema_team_v1.sql to store them in the database.", ", ".join(TABLES))
    except Exception as e:  # noqa: BLE001 - database down: still let the team work
        log.warning("team: database unreachable (%s); runs are kept in memory and lost on restart", e)
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
