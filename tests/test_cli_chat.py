"""bv chat against the test database with a scripted LLM and fake embeddings."""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import delete, select
from typer.testing import CliRunner

from cli import deps
from cli.deps import Deps
from cli.main import app
from cli.tenants import run_with_session
from core.db.models import Chunk, Message, Source, Tenant
from core.embeddings.client import EmbeddingClient
from core.llm.budget import BudgetGuard
from core.llm.client import LLMClient
from tests.fakes import FakeEmbeddingProvider, FakeLLMProvider, topic_vector
from tests.test_assistant_graph import understand_json

runner = CliRunner()

CONFIG = """\
slug: {slug}
name: Chat Test
languages: [fi, en]
handoff: {{email: info@example.com, privacy_notice_version: "1"}}
assistant:
  greeting: {{fi: "Hei! Kysy minulta.", en: "Hi!"}}
  retrieval: {{min_relevance: 0.5}}
"""

ANSWER = ['{"answerable": true}\n', "Tervetuloa! ", "Hinnat löytyvät sivulta."]


@pytest.fixture
def slug(cli_env: None, tmp_path) -> Iterator[str]:  # type: ignore[no-untyped-def]
    slug = f"chat-{uuid.uuid4().hex[:8]}"
    path = tmp_path / "t.yaml"
    path.write_text(CONFIG.format(slug=slug), encoding="utf-8")
    assert runner.invoke(app, ["tenants", "create", slug, "--config", str(path)]).exit_code == 0
    yield slug

    async def cleanup(session):  # type: ignore[no-untyped-def]
        await session.execute(delete(Tenant).where(Tenant.slug == slug))
        await session.commit()

    run_with_session(cleanup)


def seed_price_chunk(slug: str) -> None:
    """One web page about prices, embedded on the same topic axis the fake embedder uses."""

    async def seed(session):  # type: ignore[no-untyped-def]
        tid = (await session.execute(select(Tenant.id).where(Tenant.slug == slug))).scalar_one()
        src = Source(
            tenant_id=tid,
            type="web",
            uri="https://example.com/hinnat",
            title="Hinnat",
            language="fi",
        )
        session.add(src)
        await session.flush()
        session.add(
            Chunk(
                tenant_id=tid,
                source_id=src.id,
                ordinal=0,
                content="Hinnat alkavat sovitusti.",
                token_count=5,
                language="fi",
                embedding=topic_vector("hinta"),
            )
        )
        await session.commit()

    run_with_session(seed)


def install(monkeypatch: pytest.MonkeyPatch, provider: FakeLLMProvider) -> None:
    monkeypatch.setattr(
        deps,
        "DEPS",
        Deps(
            make_embedder=lambda sm: EmbeddingClient(
                FakeEmbeddingProvider(vector_for=topic_vector), sm, 0.92
            ),
            make_client=deps.DEPS.make_client,
            resolver=deps.DEPS.resolver,
            delay=0,
            make_llm=lambda sm: LLMClient(provider, sm, BudgetGuard(sm), 0.92),
        ),
    )


def test_chat_unknown_tenant_fails(cli_env: None) -> None:
    r = runner.invoke(app, ["chat", "no-such-tenant-xyz"])
    assert r.exit_code == 1 and "not found" in r.output


def test_chat_rejects_bad_lang(slug: str) -> None:
    r = runner.invoke(app, ["chat", slug, "--lang", "de"])
    assert r.exit_code == 1 and "--lang" in r.output


def test_chat_turn_streams_shows_debug_cost_and_persists(
    slug: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_price_chunk(slug)
    provider = FakeLLMProvider(understand_json(query="hinta price"), ANSWER)
    install(monkeypatch, provider)

    r = runner.invoke(app, ["chat", slug, "--show-debug"], input="Paljonko maksaa?\n\n")

    assert r.exit_code == 0, r.output
    assert "bot> Hei! Kysy minulta." in r.output  # tenant greeting
    assert "Tervetuloa! Hinnat löytyvät sivulta." in r.output
    assert "intent='question'" in r.output and "answerable=True" in r.output
    assert "relevance=1.0" in r.output
    assert "ttft=" in r.output
    assert "Session cost: EUR" in r.output and "EUR 0.000000" not in r.output

    async def stored(session):  # type: ignore[no-untyped-def]
        rows = await session.execute(
            select(Message.role)
            .join(Tenant, Tenant.id == Message.tenant_id)
            .where(Tenant.slug == slug)
        )
        return sorted(rows.scalars().all())

    assert run_with_session(stored) == ["assistant", "user"]


def test_chat_shows_lead_form_marker_when_nothing_is_found(
    slug: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeLLMProvider(understand_json(query="tuntematon aihe"))  # nothing ingested
    install(monkeypatch, provider)
    r = runner.invoke(app, ["chat", slug], input="Mikä on Plutonin kuu?\n/quit\n")
    assert r.exit_code == 0, r.output
    assert "[lead form would be shown: no_answer]" in r.output
    assert "en valitettavasti löytänyt" in r.output.lower()


def test_chat_without_api_key_fails_cleanly(slug: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    from core.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(
        deps,
        "DEPS",
        Deps(
            make_embedder=lambda sm: EmbeddingClient(FakeEmbeddingProvider(), sm, 0.92),
            make_client=deps.DEPS.make_client,
            resolver=deps.DEPS.resolver,
            delay=0,
        ),
    )
    r = runner.invoke(app, ["chat", slug], input="\n")
    get_settings.cache_clear()
    assert r.exit_code == 1 and "ANTHROPIC_API_KEY" in r.output
