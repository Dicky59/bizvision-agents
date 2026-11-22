import asyncio
import json
import uuid
from collections.abc import AsyncIterator

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agents.assistant.types import Debug, Done, Event, Meta, Token
from api.deps import ApiDeps
from api.errors import ApiError
from api.ratelimit import InMemoryLimiter
from api.sse import encode, stream_events
from core.db.models import Message as MessageRow
from tests.api_helpers import ORIGIN, api_tenant, client_for, tenant_config
from tests.test_assistant_graph import ANSWER, understand_json
from tests.test_assistant_service import Rig

SM = async_sessionmaker[AsyncSession]
VISITOR = "visitor-0001"


def parse_sse(raw: str) -> list[tuple[str, dict]]:
    out = []
    for block in raw.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def body(message: str = "Tarjoatteko SEO:ta?", **kw: object) -> dict:
    return {"visitor_id": VISITOR, "message": message, **kw}


async def post(c: httpx.AsyncClient, slug: str, payload: dict, origin: str | None = ORIGIN):
    headers = {"Origin": origin} if origin else {}
    return await c.post(f"/v1/assistant/{slug}/chat", json=payload, headers=headers)


async def count_messages(sm: SM, tid: uuid.UUID) -> int:
    async with sm() as s:
        stmt = select(func.count()).where(MessageRow.tenant_id == tid)
        return int((await s.execute(stmt)).scalar_one())


async def test_sse_event_sequence_and_persistence(sessionmaker: SM) -> None:
    async with api_tenant(sessionmaker) as t:
        rig = Rig(sessionmaker, t.id, understand_json(), ANSWER)
        deps = ApiDeps(sessionmaker=lambda: sessionmaker, chat_service=lambda: rig.service)
        async with client_for(deps) as c:
            r = await post(c, t.slug, body())
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"].startswith("no-cache")
        assert r.headers["access-control-allow-origin"] == ORIGIN
        events = parse_sse(r.text)
        kinds = [k for k, _ in events]
        assert kinds[0] == "meta" and kinds[-1] == "done"
        assert kinds[1:-2] == ["token"] * (len(kinds) - 3) and kinds[-2] == "sources"
        assert (
            "".join(d["text"] for k, d in events if k == "token")
            == "Kyllä, tarjoamme SEO-palvelua [1]."
        )
        assert events[-2][1]["items"] == [
            {"ns": [1], "title": "Page 1", "url": "https://acme.fi/seo"}
        ]
        assert uuid.UUID(events[0][1]["conversation_id"])
        assert uuid.UUID(events[-1][1]["message_id"])
        assert await count_messages(sessionmaker, t.id) == 2  # user + assistant


async def test_conversation_resumes_for_same_visitor_only(sessionmaker: SM) -> None:
    async with api_tenant(sessionmaker) as t:
        rig = Rig(
            sessionmaker,
            t.id,
            understand_json(), ANSWER, understand_json(), ANSWER, understand_json(), ANSWER,
        )  # fmt: skip
        deps = ApiDeps(sessionmaker=lambda: sessionmaker, chat_service=lambda: rig.service)
        async with client_for(deps) as c:
            first = parse_sse((await post(c, t.slug, body())).text)[0][1]["conversation_id"]
            same = parse_sse((await post(c, t.slug, body(conversation_id=first))).text)[0][1][
                "conversation_id"
            ]
            other = parse_sse(
                (
                    await post(
                        c, t.slug, {**body(conversation_id=first), "visitor_id": "someone-else-1"}
                    )
                ).text
            )[0][1]["conversation_id"]
    assert same == first and other != first


async def test_wrong_or_missing_origin_never_reaches_the_assistant(sessionmaker: SM) -> None:
    async with api_tenant(sessionmaker) as t:
        rig = Rig(sessionmaker, t.id, understand_json(), ANSWER)
        deps = ApiDeps(sessionmaker=lambda: sessionmaker, chat_service=lambda: rig.service)
        async with client_for(deps) as c:
            bad = await post(c, t.slug, body(), origin="https://evil.example.org")
            none = await post(c, t.slug, body(), origin=None)
    assert bad.status_code == none.status_code == 403
    assert bad.json() == {"error": {"code": "forbidden"}}
    assert rig.provider.calls == []  # no LLM call was made
    assert await count_messages(sessionmaker, t.id) == 0


async def test_validation_and_message_limit(sessionmaker: SM) -> None:
    async with api_tenant(sessionmaker, limits={"max_message_chars": 50}) as t:
        deps = ApiDeps(sessionmaker=lambda: sessionmaker)
        async with client_for(deps) as c:
            long = await post(c, t.slug, body("x" * 51))
            blank = await post(c, t.slug, body("   "))
            short_id = await post(c, t.slug, {**body(), "visitor_id": "abc"})
            bad_chars = await post(c, t.slug, {**body(), "visitor_id": "bad id!!!!!!"})
            bad_lang = await post(c, t.slug, body(language="sv"))
    assert long.status_code == 413 and long.json() == {"error": {"code": "message_too_long"}}
    assert blank.status_code == short_id.status_code == bad_chars.status_code == 422
    assert bad_lang.status_code == 422


async def test_rate_limit_per_ip_returns_429(sessionmaker: SM) -> None:
    async with api_tenant(sessionmaker, limits={"max_message_chars": 5}) as t:
        deps = ApiDeps(sessionmaker=lambda: sessionmaker, limiter=InMemoryLimiter())
        async with client_for(deps) as c:
            statuses = [(await post(c, t.slug, body("too long!"))).status_code for _ in range(21)]
            last = await post(c, t.slug, body("too long!"))
    assert statuses[:20] == [413] * 20  # allowed through the limiter, rejected by the size check
    assert statuses[20] == 429
    assert last.status_code == 429 and int(last.headers["retry-after"]) >= 1


async def test_rate_limit_per_tenant_daily(sessionmaker: SM) -> None:
    async with api_tenant(
        sessionmaker, limits={"max_message_chars": 5, "chat_requests_per_day": 2}
    ) as t:
        deps = ApiDeps(sessionmaker=lambda: sessionmaker, limiter=InMemoryLimiter())
        async with client_for(deps) as c:
            codes = [(await post(c, t.slug, body("too long!"))).status_code for _ in range(3)]
    assert codes == [413, 413, 429]


async def test_service_unavailable_maps_to_503(sessionmaker: SM) -> None:
    def unavailable():
        raise ApiError(503, "unavailable")

    async with api_tenant(sessionmaker) as t:
        deps = ApiDeps(sessionmaker=lambda: sessionmaker, chat_service=unavailable)
        async with client_for(deps) as c:
            r = await post(c, t.slug, body())
    assert r.status_code == 503 and r.json() == {"error": {"code": "unavailable"}}


# ------------------------------------------------------------------ sse unit tests


def test_encode_skips_debug_and_serialises_unicode() -> None:
    assert encode(Debug({"x": 1})) is None
    assert encode(Token("Hyvää päivää")) == (
        'event: token\ndata: {"text": "Hyvää päivää"}\n\n'.encode()
    )


async def test_turn_finishes_after_client_disconnect() -> None:
    cfg = tenant_config("x")
    finished = asyncio.Event()
    release = asyncio.Event()
    cid = uuid.uuid4()

    async def turn() -> AsyncIterator[Event]:
        yield Meta(cid)
        await release.wait()  # the visitor has gone by now
        yield Token("answer")
        yield Done("answer", [], "question", "fi", True, 0.9, False, False, uuid.uuid4())
        finished.set()  # stands in for "assistant message persisted"

    stream = stream_events(turn(), cfg)
    first = await anext(stream)
    assert first.startswith(b"event: meta")
    await stream.aclose()  # client disconnects mid-answer
    release.set()
    await asyncio.wait_for(finished.wait(), timeout=2)


async def test_pump_failure_becomes_safe_error_frame() -> None:
    cfg = tenant_config("x")

    async def boom() -> AsyncIterator[Event]:
        yield Meta(uuid.uuid4())
        raise RuntimeError("secret detail: visitor@example.com")

    frames = [f async for f in stream_events(boom(), cfg)]
    assert frames[0].startswith(b"event: meta")
    last = parse_sse(frames[-1].decode())[0]
    assert last[0] == "error" and last[1]["code"] == "internal"
    assert b"secret" not in frames[-1] and b"visitor@" not in frames[-1]
