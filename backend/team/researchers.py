"""The three researchers, behind one interface: `Researcher.run(case_text, ctx) -> MemberResult`.

  ClaudeResearcher   Claude + Anthropic's server web tools (web_search / web_fetch), domain-restricted
                     per seat. Used for docs (official docs only), issues (github.com only) and as the
                     community fallback (open web).
  OpenAIResearcher   OpenAI Responses API + its built-in `web_search` tool (community seat).
  FallbackResearcher Runs the OpenAI researcher; on any failure, runs a Claude researcher instead and
                     records that in the model name.

Each researcher sees only the case file. They never see each other's work. Each one returns
structured findings [{cause, evidence:[{url, source, quote, seen}], confidence, steps, check}]; `seen`
says whether that URL actually came back from the researcher's own search/fetch results.
"""

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import config, prompts
from .costs import CostMeter

MAX_FINDINGS = 3
MAX_EVIDENCE = 4
MAX_STEPS = 8
MAX_CONTINUATIONS = 4          # pause_turn resumptions per researcher
MIN_CALL_SECONDS = 3.0         # don't start an API call with less time than this left
SOURCES_EVENTS_PER_SEARCH = 3  # board-room "source found" events per search, so the feed stays readable
FALLBACK_MIN_SECONDS = 20.0    # only fall back to Claude if at least this much time is left

Emit = Callable[[str, str, dict[str, Any] | None], None]   # (type, text, data)


class ResearcherTimeout(Exception):
    pass


class CostCapReached(Exception):
    pass


@dataclass
class Context:
    deadline: float                     # time.monotonic() value this researcher must finish by
    meter: CostMeter
    emit: Emit
    settings: config.Settings
    min_call_s: float = MIN_CALL_SECONDS

    def remaining(self) -> float:
        return self.deadline - time.monotonic()


@dataclass
class MemberResult:
    seat: str
    model: str
    status: str = "ok"                  # ok | failed | timeout | skipped
    category: str | None = None
    findings: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""                     # the raw answer when it had no parseable JSON
    error: str | None = None
    elapsed_s: float = 0.0
    sources_seen: set[str] = field(default_factory=set)
    searches: int = 0


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _norm_url(url: str) -> str:
    return url.strip().rstrip("/").split("#")[0].lower()


# ---------------------------------------------------------------------------
# Parsing a researcher's answer
# ---------------------------------------------------------------------------

def _json_candidates(text: str) -> list[str]:
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    out = list(reversed(fenced))
    # Also try the last balanced {...} in the text (an answer without fences).
    end = text.rfind("}")
    if end != -1:
        depth = 0
        for i in range(end, -1, -1):
            if text[i] == "}":
                depth += 1
            elif text[i] == "{":
                depth -= 1
                if depth == 0:
                    out.append(text[i:end + 1])
                    break
    return out


def _clean_conf(value: Any) -> float:
    try:
        return round(min(1.0, max(0.0, float(value))), 2)
    except (TypeError, ValueError):
        return 0.0


def _short(value: Any, cap: int) -> str:
    text = " ".join(str(value or "").split()) if not isinstance(value, str) else value.strip()
    return text if len(text) <= cap else text[:cap].rstrip() + "..."


def normalize_finding(raw: Any, seen: set[str] | None = None) -> dict[str, Any] | None:
    """One finding in the shared shape, or None if it has no cause."""
    if not isinstance(raw, dict) or not str(raw.get("cause") or "").strip():
        return None
    evidence = []
    for ev in (raw.get("evidence") or [])[:MAX_EVIDENCE]:
        if isinstance(ev, str):
            ev = {"url": ev}
        if not isinstance(ev, dict) or not str(ev.get("url") or "").startswith(("http://", "https://")):
            continue
        item = {"url": _short(ev["url"], 500), "source": _short(ev.get("source") or "other", 30),
                "quote": _short(ev.get("quote") or "", 400)}
        if seen is not None:
            item["seen"] = _norm_url(item["url"]) in seen
        elif "seen" in ev:
            item["seen"] = bool(ev["seen"])
        evidence.append(item)
    steps = raw.get("steps") or []
    if isinstance(steps, str):
        steps = [steps]
    check = raw.get("check") if isinstance(raw.get("check"), dict) else {}
    command = check.get("command")
    return {
        "cause": _short(raw["cause"], 1000),
        "confidence": _clean_conf(raw.get("confidence")),
        "evidence": evidence,
        "steps": [_short(s, 1000) for s in steps if str(s).strip()][:MAX_STEPS],
        "check": {"command": _short(command, 500) if isinstance(command, str) and command.strip() else None,
                  "description": _short(check.get("description") or "", 500)},
    }


def parse_answer(text: str, seen: set[str] | None = None) -> tuple[str | None, list[dict[str, Any]] | None]:
    """(category, findings) from a researcher's final text. findings is None when no JSON was found."""
    for candidate in _json_candidates(text or ""):
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        raw = data.get("findings")
        if raw is None and "cause" in data:
            raw = [data]
        if not isinstance(raw, list):
            continue
        findings = [f for f in (normalize_finding(r, seen) for r in raw) if f]
        findings.sort(key=lambda f: -f["confidence"])
        category = data.get("category") if isinstance(data.get("category"), str) else None
        return category, findings[:MAX_FINDINGS]
    return None, None


# ---------------------------------------------------------------------------
# Domain restrictions
# ---------------------------------------------------------------------------

# keyword in the case file -> official docs domains (subdomains are covered automatically)
DOCS_DOMAINS: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("prisma",), ("prisma.io",)),
    (("docker", "dockerfile", "compose"), ("docs.docker.com",)),
    (("alpine", "musl", "apk add"), ("wiki.alpinelinux.org", "alpinelinux.org")),
    (("python", "pip ", ".py", "venv"), ("docs.python.org", "packaging.python.org", "pip.pypa.io")),
    (("pytest", "conftest"), ("docs.pytest.org",)),
    (("uv ", "uv.lock", "ruff"), ("docs.astral.sh",)),
    (("poetry",), ("python-poetry.org",)),
    (("django",), ("docs.djangoproject.com",)),
    (("flask",), ("flask.palletsprojects.com",)),
    (("fastapi",), ("fastapi.tiangolo.com",)),
    (("pydantic",), ("docs.pydantic.dev",)),
    (("sqlalchemy",), ("docs.sqlalchemy.org",)),
    (("node", "npm", "package.json", "esm", "commonjs"), ("nodejs.org", "docs.npmjs.com")),
    (("pnpm",), ("pnpm.io",)),
    (("yarn",), ("yarnpkg.com",)),
    (("next.js", "nextjs", "next build", "next dev"), ("nextjs.org",)),
    (("react",), ("react.dev",)),
    (("typescript", "tsconfig", ".ts"), ("typescriptlang.org",)),
    (("vite",), ("vite.dev", "vitejs.dev")),
    (("webpack",), ("webpack.js.org",)),
    (("jest",), ("jestjs.io",)),
    (("vitest",), ("vitest.dev",)),
    (("eslint",), ("eslint.org",)),
    (("tailwind",), ("tailwindcss.com",)),
    (("postgres", "psql", "psycopg"), ("postgresql.org", "psycopg.org")),
    (("supabase",), ("supabase.com",)),
    (("mysql",), ("dev.mysql.com",)),
    (("redis",), ("redis.io",)),
    (("mongodb", "mongoose"), ("mongodb.com", "mongoosejs.com")),
    (("github actions", ".github/workflows", "runs-on"), ("docs.github.com",)),
    (("gradle",), ("docs.gradle.org",)),
    (("kotlin",), ("kotlinlang.org",)),
    (("maven", "pom.xml"), ("maven.apache.org",)),
    (("java", "jdk", "jvm"), ("docs.oracle.com", "openjdk.org")),
    (("spring",), ("docs.spring.io",)),
    (("rust", "cargo"), ("doc.rust-lang.org", "docs.rs")),
    (("golang", "go.mod", "go build"), ("go.dev", "pkg.go.dev")),
    (("kubernetes", "kubectl"), ("kubernetes.io",)),
    (("aws", "lambda", "s3"), ("docs.aws.amazon.com",)),
    (("vercel",), ("vercel.com",)),
    (("terraform",), ("developer.hashicorp.com",)),
    (("nginx",), ("nginx.org",)),
    (("openssl",), ("openssl.org", "docs.openssl.org")),
    (("windows", "powershell"), ("learn.microsoft.com",)),
    (("ubuntu", "debian", "apt-get"), ("ubuntu.com", "debian.org")),
    (("javascript", "fetch(", "cors"), ("developer.mozilla.org",)),
]
COMMUNITY_SITES = ["stackoverflow.com", "stackexchange.com", "github.com", "reddit.com", "medium.com",
                   "dev.to", "quora.com", "geeksforgeeks.org", "w3schools.com"]
MAX_DOMAINS = 64


def docs_domains(tech: str) -> list[str]:
    """Official docs domains for the technologies mentioned in the case file ([] = none recognized)."""
    found: list[str] = []
    for keywords, domains in DOCS_DOMAINS:
        if any(k in tech for k in keywords):
            found += [d for d in domains if d not in found]
    return found[:MAX_DOMAINS]


def web_tools(model: str, *, allowed: list[str] | None = None, blocked: list[str] | None = None,
              searches: int, fetches: int) -> list[dict[str, Any]]:
    """Anthropic server web tools for this model (dynamic-filtering versions where supported)."""
    dynamic = model in config.DYNAMIC_FILTER_MODELS
    search: dict[str, Any] = {"type": "web_search_20260209" if dynamic else "web_search_20250305",
                              "name": "web_search", "max_uses": searches}
    fetch: dict[str, Any] = {"type": "web_fetch_20260209" if dynamic else "web_fetch_20250910",
                             "name": "web_fetch", "max_uses": fetches}
    for tool in (search, fetch):
        if allowed:
            tool["allowed_domains"] = allowed
        elif blocked:
            tool["blocked_domains"] = blocked
    return [search, fetch] if fetches > 0 else [search]


# ---------------------------------------------------------------------------
# Researchers
# ---------------------------------------------------------------------------

class Researcher:
    seat: str
    model: str

    def run(self, case_text: str, ctx: Context) -> MemberResult:   # pragma: no cover - interface
        raise NotImplementedError

    def run_safe(self, case_text: str, ctx: Context) -> MemberResult:
        """`run`, with timing, events and every exception turned into a failed MemberResult."""
        start = time.monotonic()
        ctx.emit("started", f"{self.seat} researcher started ({self.model})", {"model": self.model})
        try:
            result = self.run(case_text, ctx)
        except ResearcherTimeout:
            result = MemberResult(self.seat, self.model, status="timeout", error="ran out of time")
        except CostCapReached:
            result = MemberResult(self.seat, self.model, status="skipped", error="run cost cap reached")
        except Exception as e:  # noqa: BLE001 - any SDK/network/parse error fails only this seat
            result = MemberResult(self.seat, self.model, status="failed", error=f"{type(e).__name__}: {e}"[:300])
        result.elapsed_s = round(time.monotonic() - start, 1)
        cost = ctx.meter.member_cost(self.seat)
        if result.status == "ok":
            ctx.emit("finished", f"{len(result.findings)} finding(s) in {result.elapsed_s}s, ${cost:.3f}",
                     {"findings": result.findings, "category": result.category, "elapsed_s": result.elapsed_s,
                      "cost_usd": round(cost, 4), "model": result.model})
        else:
            ctx.emit(result.status, f"{result.error} after {result.elapsed_s}s",
                     {"error": result.error, "elapsed_s": result.elapsed_s, "model": result.model})
        return result


class ClaudeResearcher(Researcher):
    def __init__(self, seat: str, model: str, client: Any, *, allowed: list[str] | None = None,
                 blocked: list[str] | None = None, extra_instructions: str = ""):
        self.seat, self.model, self.client = seat, model, client
        self.allowed, self.blocked, self.extra = allowed, blocked, extra_instructions

    def _params(self, case_text: str, ctx: Context) -> dict[str, Any]:
        s = ctx.settings
        system = prompts.researcher_system(self.seat, int(s.researcher_timeout_s * 0.75),
                                           s.search_max_uses, s.fetch_max_uses)
        if self.extra:
            system += "\n" + self.extra
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 8000,
            "system": system,
            "tools": web_tools(self.model, allowed=self.allowed, blocked=self.blocked,
                               searches=s.search_max_uses, fetches=s.fetch_max_uses),
        }
        if self.model in config.EFFORT_MODELS:
            params["output_config"] = {"effort": s.researcher_effort}
        if self.model in config.FALLBACK_MODELS:
            params["betas"] = [config.FALLBACK_BETA]
            params["extra_body"] = {"fallbacks": "default"}
        return params

    def run(self, case_text: str, ctx: Context) -> MemberResult:
        result = MemberResult(self.seat, self.model)
        user = {"role": "user", "content": prompts.RESEARCHER_USER.format(case=case_text)}
        assistant_blocks: list[Any] = []
        params = self._params(case_text, ctx)
        emitted: set[str] = set()
        final = None
        for _ in range(MAX_CONTINUATIONS + 1):
            if ctx.meter.exceeded:
                raise CostCapReached()
            remaining = ctx.remaining()
            if remaining < ctx.min_call_s:
                raise ResearcherTimeout()
            messages = [user] + ([{"role": "assistant", "content": assistant_blocks}] if assistant_blocks else [])
            with self.client.beta.messages.stream(messages=messages, timeout=remaining, **params) as stream:
                try:
                    for event in stream:
                        # The HTTP timeout only bounds the gap between chunks, so enforce the deadline here.
                        if ctx.remaining() <= 0:
                            raise ResearcherTimeout()
                        if _get(event, "type") == "content_block_stop" and _get(event, "content_block") is not None:
                            self._observe(_get(event, "content_block"), result, ctx, emitted)
                except ResearcherTimeout:
                    # Count what was spent so far (input tokens and searches are known mid-stream).
                    snapshot = getattr(stream, "current_message_snapshot", None)
                    if snapshot is not None:
                        ctx.meter.add_claude(self.seat, self.model, _get(snapshot, "usage"))
                    raise
                final = stream.get_final_message()
            ctx.meter.add_claude(self.seat, self.model, _get(final, "usage"))
            for block in _get(final, "content") or []:
                self._observe(block, result, ctx, emitted)
            assistant_blocks += list(_get(final, "content") or [])
            stop = _get(final, "stop_reason")
            if stop == "refusal":
                details = _get(final, "stop_details")
                raise RuntimeError(f"model declined (refusal: {_get(details, 'category') or 'unknown'})")
            if stop == "pause_turn":
                continue   # server tool loop hit its limit: re-send so it resumes
            break
        text = "\n".join(_get(b, "text") or "" for b in assistant_blocks if _get(b, "type") == "text")
        category, findings = parse_answer(text, result.sources_seen)
        result.category = category
        if findings is None:
            result.notes = text[-6000:]
        result.findings = findings or []
        return result

    def _observe(self, block: Any, result: MemberResult, ctx: Context, emitted: set[str]) -> None:
        """Turn one content block into board-room events and remember every URL the tools returned."""
        kind = _get(block, "type")
        if kind == "server_tool_use":
            key = f"use:{_get(block, 'id')}"
            if key in emitted:
                return
            emitted.add(key)
            tool_input = _get(block, "input") or {}
            if _get(block, "name") == "web_search":
                result.searches += 1
                ctx.emit("searching", str(_get(tool_input, "query") or ""), {"query": _get(tool_input, "query")})
            elif _get(block, "name") == "web_fetch":
                ctx.emit("fetching", str(_get(tool_input, "url") or ""), {"url": _get(tool_input, "url")})
        elif kind == "web_search_tool_result":
            key = f"res:{_get(block, 'tool_use_id')}"
            content = _get(block, "content")
            if isinstance(content, list):
                for r in content:
                    if _get(r, "url"):
                        result.sources_seen.add(_norm_url(_get(r, "url")))
                if key not in emitted:
                    emitted.add(key)
                    for r in content[:SOURCES_EVENTS_PER_SEARCH]:
                        ctx.emit("source", str(_get(r, "title") or _get(r, "url")),
                                 {"url": _get(r, "url"), "title": _get(r, "title")})
            elif key not in emitted:   # an error object, not a list (server-tool errors don't raise)
                emitted.add(key)
                ctx.emit("tool_error", f"search failed: {_get(content, 'error_code') or 'unknown'}",
                         {"error_code": _get(content, "error_code")})
        elif kind == "web_fetch_tool_result":
            key = f"res:{_get(block, 'tool_use_id')}"
            content = _get(block, "content")
            if _get(content, "type") == "web_fetch_result" and _get(content, "url"):
                result.sources_seen.add(_norm_url(_get(content, "url")))
                if key not in emitted:
                    emitted.add(key)
                    ctx.emit("source", f"read {_get(content, 'url')}", {"url": _get(content, "url")})
            elif key not in emitted:
                emitted.add(key)
                ctx.emit("tool_error", f"page read failed: {_get(content, 'error_code') or 'unknown'}",
                         {"error_code": _get(content, "error_code")})
        elif kind == "text":
            for c in _get(block, "citations") or []:
                if _get(c, "url"):
                    result.sources_seen.add(_norm_url(_get(c, "url")))


class OpenAIResearcher(Researcher):
    def __init__(self, seat: str, model: str, client: Any):
        self.seat, self.model, self.client = seat, model, client

    def run(self, case_text: str, ctx: Context) -> MemberResult:
        if ctx.meter.exceeded:
            raise CostCapReached()
        remaining = ctx.remaining()
        if remaining < ctx.min_call_s:
            raise ResearcherTimeout()
        s = ctx.settings
        response = self.client.responses.create(
            model=self.model,
            instructions=prompts.researcher_system(self.seat, int(s.researcher_timeout_s * 0.75),
                                                   s.search_max_uses, None),
            input=prompts.RESEARCHER_USER.format(case=case_text),
            tools=[{"type": "web_search", "search_context_size": "medium"}],
            include=["web_search_call.action.sources"],
            max_tool_calls=s.search_max_uses + 2,
            max_output_tokens=8000,
            timeout=remaining,
        )
        result = MemberResult(self.seat, self.model)
        searches = 0
        for item in _get(response, "output") or []:
            kind = _get(item, "type")
            if kind == "web_search_call":
                action = _get(item, "action")
                atype = _get(action, "type")
                if atype == "search":
                    searches += 1
                    queries = _get(action, "queries") or [_get(action, "query")]
                    ctx.emit("searching", "; ".join(q for q in queries if q), {"queries": queries})
                    sources = _get(action, "sources") or []
                    for src in sources:
                        if _get(src, "url"):
                            result.sources_seen.add(_norm_url(_get(src, "url")))
                    for src in sources[:SOURCES_EVENTS_PER_SEARCH]:
                        ctx.emit("source", str(_get(src, "url")), {"url": _get(src, "url")})
                elif atype in ("open_page", "find_in_page") and _get(action, "url"):
                    result.sources_seen.add(_norm_url(_get(action, "url")))
                    ctx.emit("fetching", str(_get(action, "url")), {"url": _get(action, "url")})
            elif kind == "message":
                for part in _get(item, "content") or []:
                    for ann in _get(part, "annotations") or []:
                        if _get(ann, "type") == "url_citation" and _get(ann, "url"):
                            result.sources_seen.add(_norm_url(_get(ann, "url")))
        result.searches = searches
        ctx.meter.add_openai(self.seat, self.model, _get(response, "usage"), searches)
        if _get(response, "status") == "failed":
            raise RuntimeError(f"OpenAI response failed: {_get(_get(response, 'error'), 'message') or 'unknown'}")
        text = _get(response, "output_text") or ""
        category, findings = parse_answer(text, result.sources_seen)
        result.category = category
        if findings is None:
            result.notes = text[-6000:]
        result.findings = findings or []
        return result


class FallbackResearcher(Researcher):
    """Try `primary`; if it fails (or can't run), run `make_fallback()` and say so in the model name."""

    def __init__(self, seat: str, primary: Researcher | None, make_fallback: Callable[[], Researcher | None],
                 missing_reason: str = ""):
        self.seat = seat
        self.primary = primary
        self.make_fallback = make_fallback
        self.missing_reason = missing_reason
        self.model = primary.model if primary else "fallback"

    def run(self, case_text: str, ctx: Context) -> MemberResult:
        reason = self.missing_reason
        if self.primary is not None:
            try:
                result = self.primary.run(case_text, ctx)
                return result
            except (ResearcherTimeout, CostCapReached):
                raise
            except Exception as e:  # noqa: BLE001
                reason = f"{self.primary.model} failed: {type(e).__name__}: {e}"[:200]
        fallback = self.make_fallback()
        if fallback is None:
            raise RuntimeError(reason or "no model available for this seat")
        # A late failure leaves too little time for a second researcher to do anything useful.
        if self.primary is not None and ctx.remaining() < min(FALLBACK_MIN_SECONDS, ctx.settings.researcher_timeout_s / 3):
            raise ResearcherTimeout()
        ctx.emit("fallback", f"switching to {fallback.model}: {reason}", {"reason": reason, "model": fallback.model})
        result = fallback.run(case_text, ctx)
        result.model = f"{fallback.model} (fallback: {reason})"
        return result


def build_researchers(case: dict[str, Any], models: dict[str, str], clients: config.Clients) -> list[Researcher]:
    """The three seats for this case file. Seats with no usable client fail at run time."""
    from .case_file import tech_text

    seats: list[Researcher] = []
    anthropic_client = clients.anthropic

    def claude(seat: str, model: str, **kw: Any) -> Researcher:
        if anthropic_client is None:
            return _Unavailable(seat, model, "ANTHROPIC_API_KEY is not set")
        if config.is_openai_model(model):
            return _Unavailable(seat, model, "this seat needs a Claude model")
        return ClaudeResearcher(seat, model, anthropic_client, **kw)

    domains = docs_domains(tech_text(case))
    if domains:
        seats.append(claude("docs", models["docs"], allowed=domains))
    else:   # no technology recognized: keep community sites out and restrict by prompt
        seats.append(claude("docs", models["docs"], blocked=COMMUNITY_SITES,
                            extra_instructions="Only use official documentation sites of the tools involved."))
    seats.append(claude("issues", models["issues"], allowed=["github.com"]))

    community_model = models["community"]
    fallback_model = models["community_fallback"]

    def make_fallback() -> Researcher | None:
        if anthropic_client is None:
            return None
        return ClaudeResearcher("community", fallback_model, anthropic_client)

    if config.is_openai_model(community_model):
        if clients.openai is None:
            seats.append(FallbackResearcher("community", None, make_fallback,
                                            missing_reason="OPENAI_API_KEY missing"))
        else:
            seats.append(FallbackResearcher("community", OpenAIResearcher("community", community_model, clients.openai),
                                            make_fallback))
    else:
        seats.append(claude("community", community_model))
    return seats


class _Unavailable(Researcher):
    def __init__(self, seat: str, model: str, reason: str):
        self.seat, self.model, self.reason = seat, model, reason

    def run(self, case_text: str, ctx: Context) -> MemberResult:
        raise RuntimeError(self.reason)
