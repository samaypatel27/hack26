"""Fake Anthropic and OpenAI clients with canned answers, so the team can run with no keys and no cost.

Used by the tests and by `python -m backend.team.cli --offline`. They copy only the parts of the
SDKs the team uses: client.beta.messages.stream / .create, client.with_options().messages.create,
and client.responses.create. Each call goes through `handler(kind, kwargs)`; the default handler
answers like a plausible researcher/chair/gate. Pass your own handler (or `delays`) to script
failures and slow members.
"""

import json
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

NS = SimpleNamespace


def usage(inp: int = 3000, out: int = 800, searches: int = 0, fetches: int = 0) -> NS:
    return NS(input_tokens=inp, output_tokens=out, cache_read_input_tokens=0, cache_creation_input_tokens=0,
              server_tool_use=NS(web_search_requests=searches, web_fetch_requests=fetches))


def text_block(text: str) -> NS:
    return NS(type="text", text=text, citations=None)


def message(content: list[Any], stop_reason: str = "end_turn", u: NS | None = None) -> NS:
    return NS(content=content, stop_reason=stop_reason, usage=u or usage(), stop_details=None)


def seat_of(kwargs: dict[str, Any]) -> str:
    system = str(kwargs.get("system") or kwargs.get("instructions") or "")
    for seat in ("docs", "issues", "community"):
        if f"{seat} researcher" in system:
            return seat
    if "chair of a team" in system:
        return "chair"
    if "You sort problems" in system:
        return "gate"
    return "unknown"


CANNED = {
    "docs": {"cause": "The Prisma query engine binary was built for glibc, but the Alpine image uses musl, so "
                      "binaryTargets must include linux-musl-openssl-3.0.x.",
             "url": "https://www.prisma.io/docs/orm/reference/prisma-schema-reference#binarytargets-options",
             "quote": "linux-musl-openssl-3.0.x: Alpine Linux 3.17+", "conf": 0.8},
    "issues": {"cause": "Prisma Client generated on the host for the native platform only; on Alpine it needs the "
                        "musl binary target (binaryTargets = [\"native\", \"linux-musl-openssl-3.0.x\"]).",
               "url": "https://github.com/prisma/prisma/issues/16232",
               "quote": "adding linux-musl-openssl-3.0.x to binaryTargets fixed it", "conf": 0.7},
    "community": {"cause": "OpenSSL 3 is missing in the Alpine image, so the Prisma engine can't load.",
                  "url": "https://stackoverflow.com/questions/74868364",
                  "quote": "apk add openssl fixed the engine error", "conf": 0.5},
}


def researcher_answer(seat: str) -> str:
    c = CANNED.get(seat, CANNED["community"])
    data = {"category": "environment_platform", "findings": [{
        "cause": c["cause"], "confidence": c["conf"],
        "evidence": [{"url": c["url"], "source": "docs" if seat == "docs" else ("github" if seat == "issues" else "stackoverflow"),
                      "quote": c["quote"]}],
        "steps": ["In prisma/schema.prisma, set binaryTargets = [\"native\", \"linux-musl-openssl-3.0.x\"] in the "
                  "generator block", "Run npx prisma generate, then rebuild the image"],
        "check": {"command": "docker compose up --build api", "description": "the API starts without the engine error"},
    }]}
    return "Here is what I found.\n```json\n" + json.dumps(data) + "\n```"


def chair_answer() -> str:
    return json.dumps({
        "category": "environment_platform",
        "environment_check": "The image is node:20-alpine (musl, OpenSSL 3); Prisma needs the matching engine.",
        "hypotheses": [
            {"cause": CANNED["docs"]["cause"], "confidence": 0.85,
             "evidence": [{"url": CANNED["docs"]["url"], "source": "docs", "quote": CANNED["docs"]["quote"]},
                          {"url": CANNED["issues"]["url"], "source": "github", "quote": CANNED["issues"]["quote"]}],
             "steps": ["In prisma/schema.prisma, add binaryTargets = [\"native\", \"linux-musl-openssl-3.0.x\"] to the "
                       "generator client block", "Run npx prisma generate", "Rebuild: docker compose build --no-cache api"],
             "check": {"command": "docker compose up --build api", "description": "API starts, no engine error"},
             "agreed_by": ["docs", "issues"], "already_tried": False},
            {"cause": CANNED["community"]["cause"], "confidence": 0.4,
             "evidence": [{"url": CANNED["community"]["url"], "source": "stackoverflow", "quote": CANNED["community"]["quote"]}],
             "steps": ["Add RUN apk add --no-cache openssl to the Dockerfile before npm ci"],
             "check": {"command": "docker compose up --build api", "description": "API starts"},
             "agreed_by": ["community"], "already_tried": False},
        ],
        "if_none_work": "Print `npx prisma -v` inside the container and compare the engine platform with the image.",
    })


def default_handler(kind: str, kwargs: dict[str, Any]) -> Any:
    seat = seat_of(kwargs)
    if kind == "openai":
        return openai_response(researcher_answer("community"))
    if seat == "gate":
        return message([text_block(json.dumps({"kind": "external", "category": "environment_platform",
                                                "reason": "Prisma engine and Alpine image mismatch"}))],
                       u=usage(400, 60))
    if seat == "chair":
        return message([text_block(chair_answer())], u=usage(6000, 1500))
    search = NS(type="server_tool_use", id=f"srv_{seat}_1", name="web_search",
                input={"query": f"{seat}: PrismaClientInitializationError linux-musl"})
    url = CANNED.get(seat, CANNED["community"])["url"]
    result = NS(type="web_search_tool_result", tool_use_id=f"srv_{seat}_1",
                content=[NS(type="web_search_result", url=url, title=f"{seat} source", page_age=None)])
    return message([search, result, text_block(researcher_answer(seat))], u=usage(5000, 900, searches=1))


def openai_response(text: str, url: str = CANNED["community"]["url"]) -> NS:
    call = NS(type="web_search_call", id="ws_1", status="completed",
              action=NS(type="search", query="prisma alpine", queries=["prisma alpine musl"],
                        sources=[NS(type="url", url=url)]))
    msg = NS(type="message", content=[NS(type="output_text", text=text, annotations=[
        NS(type="url_citation", url=url, title="SO")])])
    return NS(output=[call, msg], output_text=text, status="completed", error=None,
              usage=NS(input_tokens=4000, output_tokens=700, input_tokens_details=NS(cached_tokens=0)))


class _Stream:
    def __init__(self, msg: Any):
        self._msg = msg
        self.current_message_snapshot = msg

    def __enter__(self) -> "_Stream":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def __iter__(self):  # noqa: ANN204
        for block in self._msg.content:
            yield NS(type="content_block_stop", content_block=block)

    def get_final_message(self) -> Any:
        return self._msg


class FakeAnthropic:
    def __init__(self, handler: Callable[[str, dict[str, Any]], Any] | None = None,
                 delays: dict[str, float] | None = None):
        self.handler = handler or default_handler
        self.delays = delays or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        outer = self

        class _Messages:
            def stream(self, **kw: Any) -> _Stream:
                return _Stream(outer._call("stream", kw))

            def create(self, **kw: Any) -> Any:
                return outer._call("create", kw)

        self.beta = NS(messages=_Messages())
        self.messages = _Messages()

    def with_options(self, **_: Any) -> "FakeAnthropic":
        return self

    def _call(self, kind: str, kw: dict[str, Any]) -> Any:
        self.calls.append((kind, kw))
        delay = self.delays.get(seat_of(kw), 0.0)
        if delay:
            time.sleep(delay)
        result = self.handler(kind, kw)
        if isinstance(result, Exception):
            raise result
        return result


class FakeOpenAI:
    def __init__(self, handler: Callable[[str, dict[str, Any]], Any] | None = None, delay: float = 0.0):
        self.handler = handler or default_handler
        self.delay = delay
        self.calls: list[dict[str, Any]] = []
        outer = self

        class _Responses:
            def create(self, **kw: Any) -> Any:
                outer.calls.append(kw)
                if outer.delay:
                    time.sleep(outer.delay)
                result = outer.handler("openai", kw)
                if isinstance(result, Exception):
                    raise result
                return result

        self.responses = _Responses()
