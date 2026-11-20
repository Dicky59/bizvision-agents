"""CLI integration tests: bv ingest / faq import / search against the test database.

External dependencies (HTTP, embeddings) are replaced through `cli.deps.DEPS`.
"""

import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete
from typer.testing import CliRunner

from cli import deps
from cli.deps import Deps
from cli.main import app
from cli.tenants import run_with_session
from core.db.models import Tenant
from core.embeddings.client import EmbeddingClient
from tests.fakes import FakeEmbeddingProvider, topic_vector
from tests.test_crawler import Site, resolve_public
from tests.test_ingest_files import FI_TEXT
from tests.test_ingest_service import site_with

runner = CliRunner()

CONFIG = """\
slug: {slug}
name: CLI Test
allowed_origins: ["https://example.com"]
handoff: {{email: info@example.com, privacy_notice_version: "1"}}
ingest:
  start_urls: ["https://example.com"]
  max_pages: {max_pages}
"""


@dataclass
class Env:
    slug: str
    provider: FakeEmbeddingProvider
    site: Site
    tmp: Path

    def create_tenant(self, max_pages: int = 200) -> None:
        path = self.tmp / "tenant.yaml"
        path.write_text(CONFIG.format(slug=self.slug, max_pages=max_pages), encoding="utf-8")
        r = runner.invoke(app, ["tenants", "create", self.slug, "--config", str(path)])
        assert r.exit_code == 0, r.output

    def run(self, *args: str, ok: bool = True):
        r = runner.invoke(app, list(args))
        if ok:
            assert r.exit_code == 0, r.output
        return r


def cost_of(output: str) -> float:
    m = re.search(r"Embedding cost: EUR ([0-9.]+)", output)
    assert m, output
    return float(m.group(1))


@pytest.fixture
def env(cli_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Env]:
    slug = f"cli-{uuid.uuid4().hex[:8]}"
    site = site_with("/", "/palvelut")
    provider = FakeEmbeddingProvider(vector_for=topic_vector)
    monkeypatch.setattr(
        deps,
        "DEPS",
        Deps(
            make_embedder=lambda sm: EmbeddingClient(provider, sm, 0.92),
            make_client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(site.handler)),
            resolver=resolve_public,
            delay=0,
        ),
    )
    e = Env(slug, provider, site, tmp_path)
    e.create_tenant()
    yield e

    async def cleanup(session):  # type: ignore[no-untyped-def]
        await session.execute(delete(Tenant).where(Tenant.slug == slug))
        await session.commit()

    run_with_session(cleanup)


# ------------------------------------------------------------------ bv ingest


def test_ingest_crawls_then_skips_unchanged(env: Env) -> None:
    first = env.run("ingest", env.slug)
    assert "2 new, 0 updated, 0 unchanged, 0 removed, 0 errors" in first.output
    assert "new       https://example.com/palvelut" in first.output
    assert cost_of(first.output) > 0
    calls = len(env.provider.calls)

    second = env.run("ingest", env.slug)
    assert "0 new, 0 updated, 2 unchanged" in second.output
    assert len(env.provider.calls) == calls  # nothing re-embedded
    assert cost_of(second.output) == 0.0


def test_ingest_warns_when_crawl_is_truncated(env: Env) -> None:
    env.create_tenant(max_pages=1)
    r = env.run("ingest", env.slug)
    assert (
        "WARNING: crawl truncated at 1 pages — removed pages won't be detected; "
        "consider raising ingest.max_pages"
    ) in r.output


def test_ingest_exit_code_is_1_when_nothing_could_be_indexed(env: Env) -> None:
    env.site.routes.clear()  # every page 404s
    r = env.run("ingest", env.slug, ok=False)
    assert r.exit_code == 1 and "0 new" in r.output


def test_ingest_dry_run_prints_pages_and_writes_nothing(env: Env) -> None:
    r = env.run("ingest", env.slug, "--dry-run")
    assert "[page] https://example.com/" in r.output and "no embeddings" in r.output
    assert env.provider.calls == []
    assert "No results" in env.run("search", env.slug, "hinta").output  # nothing was stored


def test_ingest_files_new_unchanged_and_errors(env: Env, tmp_path: Path) -> None:
    good = tmp_path / "hinnasto.md"
    good.write_text(f"# Hinnasto\n\n{FI_TEXT}\n", encoding="utf-8")
    bad = tmp_path / "taulukko.xlsx"
    bad.write_bytes(b"not supported")

    r = env.run("ingest", env.slug, "--files", str(good), "--files", str(bad), ok=False)
    assert r.exit_code == 1  # one file failed...
    assert "new       hinnasto.md" in r.output  # ...but the good one was indexed
    assert "error     taulukko.xlsx" in r.output and "UnsupportedFile" in r.output

    again = env.run("ingest", env.slug, "--files", str(good))
    assert "unchanged hinnasto.md" in again.output and cost_of(again.output) == 0.0
    assert "WARNING" not in again.output  # files never trigger removal logic


def test_ingest_files_option_validation(env: Env, tmp_path: Path) -> None:
    missing = env.run("ingest", env.slug, "--files", str(tmp_path / "nope.pdf"), ok=False)
    assert missing.exit_code == 2  # click: path does not exist
    f = tmp_path / "a.txt"
    f.write_text("x")
    both = env.run("ingest", env.slug, "--files", str(f), "--dry-run", ok=False)
    assert both.exit_code == 1 and "--dry-run" in both.output


def test_ingest_without_api_key_fails_cleanly(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        deps, "DEPS", Deps(deps._default_embedder, deps.DEPS.make_client, resolve_public, 0)
    )
    monkeypatch.setenv("OPENAI_API_KEY", "")
    deps.get_settings.cache_clear()
    r = env.run("ingest", env.slug, ok=False)
    assert r.exit_code == 1 and "OPENAI_API_KEY" in r.output


@pytest.mark.parametrize("command", ["ingest", "faq-import", "search"])
def test_unknown_tenant_is_reported(env: Env, command: str, tmp_path: Path) -> None:
    faq = write_faq(tmp_path / "faq.yaml", '- {q: "A?", a: B}\n')
    argv = {
        "ingest": ["ingest", "no-such-tenant"],
        "faq-import": ["faq", "import", "no-such-tenant", str(faq)],
        "search": ["search", "no-such-tenant", "hinta"],
    }[command]
    r = env.run(*argv, ok=False)
    assert r.exit_code == 1 and "not found" in r.output


# ------------------------------------------------------------------ bv faq import


def write_faq(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_faq_import_lifecycle(env: Env, tmp_path: Path) -> None:
    f = write_faq(
        tmp_path / "faq.yaml",
        "- q: Paljonko AI-asiakaspalvelija maksaa?\n  a: Hinta riippuu laajuudesta.\n"
        "- question: Miten projekti etenee?\n  answer: Aloitamme kartoituksella.\n",
    )
    first = env.run("faq", "import", env.slug, str(f))
    assert "2 new, 0 updated, 0 unchanged, 0 removed, 0 errors" in first.output
    assert cost_of(first.output) > 0

    second = env.run("faq", "import", env.slug, str(f))
    assert "0 new, 0 updated, 2 unchanged" in second.output and cost_of(second.output) == 0.0

    f = write_faq(
        tmp_path / "faq.yaml",
        "faq:\n  - q: Paljonko AI-asiakaspalvelija maksaa?\n    a: Alkaen 990 euroa.\n",
    )  # top-level `faq:` key; one changed, one dropped
    third = env.run("faq", "import", env.slug, str(f))
    assert "0 new, 1 updated, 0 unchanged, 1 removed" in third.output


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("", "file is empty"),
        ("just text", "expected a YAML list"),
        ("- not a mapping\n", "entry 1"),
        ("- {q: only a question}\n", "q and a must both be text"),
        ('- {q: "Kysymys?", a: 5}\n', "q and a must both be text"),
        ('- {q: "Sama?", a: yksi}\n- {q: "sama?", a: kaksi}\n', "duplicate FAQ question"),
        ('- {q: "Tyhjä?", a: "  "}\n', "non-empty question and answer"),
        ("a: [unclosed", "cannot read"),
    ],
)
def test_faq_import_rejects_bad_files_without_touching_the_faq(
    env: Env, tmp_path: Path, body: str, message: str
) -> None:
    good = write_faq(tmp_path / "good.yaml", '- {q: "Yksi?", a: Vastaus}\n')
    env.run("faq", "import", env.slug, str(good))
    calls = len(env.provider.calls)

    r = env.run("faq", "import", env.slug, str(write_faq(tmp_path / "bad.yaml", body)), ok=False)
    assert r.exit_code == 1 and message in r.output
    assert len(env.provider.calls) == calls
    again = env.run("faq", "import", env.slug, str(good))
    assert "0 new, 0 updated, 1 unchanged, 0 removed" in again.output  # FAQ intact


def test_faq_explicit_empty_list_clears_the_faq(env: Env, tmp_path: Path) -> None:
    one = write_faq(tmp_path / "f.yaml", '- {q: "Yksi?", a: A}\n')
    env.run("faq", "import", env.slug, str(one))
    r = env.run("faq", "import", env.slug, str(write_faq(tmp_path / "e.yaml", "[]\n")))
    assert "0 new, 0 updated, 0 unchanged, 1 removed" in r.output


# ------------------------------------------------------------------ bv search


def test_search_shows_ranked_results_and_confidence(env: Env, tmp_path: Path) -> None:
    env.run("ingest", env.slug)
    env.run(
        "faq",
        "import",
        env.slug,
        str(write_faq(tmp_path / "f.yaml", '- {q: "Montako euroa?", a: "Alkaen 990 euroa."}\n')),
    )

    r = env.run("search", env.slug, "hinta")
    assert "Query: 'hinta'  (language=fi, top_k=6)" in r.output  # default top_k from tenant config
    assert re.search(r"1\. relevance 1\.000 .*\[(web|faq)\]", r.output)
    assert "https://example.com/" in r.output
    assert "Best relevance 1.000 vs min_relevance 0.35 -> answer" in r.output

    low = env.run("search", env.slug, "joku aivan muu asia", "--lang", "en", "--top-k", "1")
    assert "(language=en, top_k=1)" in low.output
    assert "-> don't know" in low.output


def test_search_with_nothing_ingested_and_bad_options(env: Env) -> None:
    assert "No results" in env.run("search", env.slug, "hinta").output
    bad = env.run("search", env.slug, "hinta", "--lang", "de", ok=False)
    assert bad.exit_code == 1 and "--lang" in bad.output
    assert env.run("search", env.slug, "hinta", "--top-k", "0", ok=False).exit_code == 2


def test_faq_import_refuses_unfilled_placeholders(env: Env, tmp_path: Path) -> None:
    body = '- q: "Paljonko maksaa?"\n  a: "Hinta on {{PRICE_ASSISTANT}}."\n'
    r = env.run("faq", "import", env.slug, str(write_faq(tmp_path / "d.yaml", body)), ok=False)
    assert r.exit_code == 1 and "unfilled placeholders: {{PRICE_ASSISTANT}}" in r.output
    assert env.provider.calls == []  # nothing was embedded or stored
