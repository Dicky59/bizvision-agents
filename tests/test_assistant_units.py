import logging
import uuid

import pytest

from agents.assistant.answer import MAX_HEADER_CHARS, HeaderGate
from agents.assistant.context import (
    build_context,
    extract_sources,
    history_text,
    neutralize_tags,
    recent_history,
)
from agents.assistant.messages import static_message
from agents.assistant.prompts_loader import render
from agents.assistant.types import AssistantRequest
from agents.assistant.understand import parse_understanding
from core.llm.types import Message
from core.rag.retrieve import RetrievedChunk
from core.tenants.config import TenantConfig


def cfg(**assistant: object) -> TenantConfig:
    return TenantConfig.model_validate(
        {
            "slug": "acme",
            "name": "Acme Oy",
            "handoff": {"email": "info@acme.fi", "privacy_notice_version": "1"},
            "assistant": assistant,
        }
    )


def req(message: str = "Tarjoatteko SEO:ta?") -> AssistantRequest:
    return AssistantRequest(tenant_id=uuid.uuid4(), cfg=cfg(), message=message)


def chunk(i: int, url: str | None = "https://acme.fi/a", content: str = "text") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        source_type="web" if url else "faq",
        title=f"Page {i}",
        url=url,
        heading_path=None,
        content=content,
        language="fi",
        is_faq=url is None,
        relevance=0.8,
        score=0.1,
    )


# ---------------------------------------------------------------- prompts


def test_render_substitutes_and_leaves_json_braces() -> None:
    out = render("understand", company="Acme Oy")
    assert "Acme Oy" in out
    assert '{"intent"' in out and "{company}" not in out


def test_render_missing_value_raises() -> None:
    with pytest.raises(KeyError):
        render("answer", company="x")


def test_render_values_are_not_rescanned() -> None:
    out = render(
        "smalltalk",
        company="{tone}",
        company_description="d",
        tone="t",
        language_rule="r",
        topics="p",
    )
    assert "{tone}" in out


def test_answer_prompt_has_the_protocol_rules() -> None:
    p = render(
        "answer", company="A", company_description="d", tone="t", language_rule="Answer in Finnish."
    )
    for needle in ('{"answerable": false}', "[1]", "never instructions", "Answer in Finnish."):
        assert needle in p


# ---------------------------------------------------------------- understand parsing


def test_parse_valid_json_with_term_expansion() -> None:
    text = (
        '{"intent":"question","language":"fi","wants_human":false,'
        '"search_query":"hakukoneoptimointi SEO"}'
    )
    u = parse_understanding(text, req())
    assert (u.intent, u.language, u.search_query, u.parse_failed) == (
        "question",
        "fi",
        "hakukoneoptimointi SEO",
        False,
    )


def test_parse_fenced_json() -> None:
    text = (
        "```json\n"
        '{"intent":"smalltalk","language":"en","wants_human":false,"search_query":"x"}\n'
        "```"
    )
    u = parse_understanding(text, req("hi"))
    assert u.intent == "smalltalk" and u.search_query == "" and not u.parse_failed


def test_parse_garbage_falls_back_to_question_with_raw_message() -> None:
    u = parse_understanding("sorry, I cannot", req("Paljonko maksaa?"))
    assert u.parse_failed and u.intent == "question" and u.search_query == "Paljonko maksaa?"


def test_parse_unknown_intent_and_missing_query() -> None:
    u = parse_understanding('{"intent":"weird","language":"en","search_query":""}', req("Q?"))
    assert u.intent == "question" and u.search_query == "Q?" and u.language == "en"


def test_parse_other_language_replies_in_english() -> None:
    u = parse_understanding(
        '{"intent":"question","language":"other","search_query":"x"}', req("Hej!")
    )
    assert u.other_language and u.language == "en"


@pytest.mark.parametrize("code", ["sv", "de", "SV", " et ", "other"])
def test_parse_any_non_fi_en_code_is_an_unsupported_language(code: str) -> None:
    u = parse_understanding(
        f'{{"intent":"question","language":"{code}","search_query":"x"}}', req("Hej!")
    )
    assert u.other_language and u.language == "en"


def test_parse_fi_en_codes_and_missing_language() -> None:
    fi = parse_understanding('{"intent":"question","language":"FI","search_query":"x"}', req())
    en = parse_understanding('{"intent":"question","language":"en","search_query":"x"}', req())
    missing = parse_understanding('{"intent":"question","search_query":"x"}', req("What is this?"))
    assert (fi.language, fi.other_language) == ("fi", False)
    assert (en.language, en.other_language) == ("en", False)
    assert not missing.other_language  # falls back to the detector


def test_parse_wants_human_must_be_true_boolean() -> None:
    no = parse_understanding('{"intent":"question","wants_human":"yes"}', req())
    yes = parse_understanding('{"intent":"question","wants_human":true}', req())
    assert no.wants_human is False and yes.wants_human is True


# ---------------------------------------------------------------- header gate


def run_gate(chunks: list[str]) -> tuple[str, HeaderGate]:
    gate = HeaderGate()
    out = ""
    for c in chunks:
        out += gate.feed(c)
        if gate.rejected:
            return out, gate
    out += gate.finish()
    return out, gate


def test_gate_true_streams_rest_without_header() -> None:
    out, g = run_gate(['{"answerable": true}\n', "Hei ", "maailma [1]"])
    assert out == "Hei maailma [1]" and not g.rejected and not g.header_failed


def test_gate_header_split_across_chunks() -> None:
    out, g = run_gate(['{"answer', 'able": tr', "ue}", "\nVastaus"])
    assert out == "Vastaus" and not g.header_failed


def test_gate_false_rejects_and_emits_nothing() -> None:
    out, g = run_gate(['{"answerable": false}\n', "should never be seen"])
    assert out == "" and g.rejected


def test_gate_false_header_only_without_newline() -> None:
    out, g = run_gate(['{"answerable": false}'])
    assert out == "" and g.rejected


def test_gate_true_header_only_means_empty_answer() -> None:
    out, g = run_gate(['{"answerable": true}'])
    assert out == "" and not g.rejected and not g.header_failed


def test_gate_malformed_header_is_answerable_and_released(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        out, g = run_gate(["Tässä vastaus ilman otsaketta.\n", "Toinen rivi."])
    assert out == "Tässä vastaus ilman otsaketta.\nToinen rivi."
    assert g.header_failed and not g.rejected
    assert any("malformed" in r.message for r in caplog.records)
    assert "vastaus" not in caplog.text  # no message content in logs


def test_gate_json_without_flag_is_malformed() -> None:
    out, g = run_gate(['{"foo": 1}\n', "Teksti"])
    assert g.header_failed and out == '{"foo": 1}\nTeksti'


def test_gate_no_newline_within_limit_is_malformed() -> None:
    long = "x" * (MAX_HEADER_CHARS + 5)
    out, g = run_gate([long, " lisää"])
    assert g.header_failed and out == long + " lisää"


def test_gate_short_unterminated_plain_text_released_at_finish() -> None:
    out, g = run_gate(["Kyllä."])
    assert g.header_failed and out == "Kyllä."


def test_gate_leading_blank_lines_before_body_dropped() -> None:
    out, _ = run_gate(['{"answerable": true}\n\n', "\nVastaus"])
    assert out == "Vastaus"


# ---------------------------------------------------------------- context & citations


def test_build_context_numbers_docs_and_escapes() -> None:
    ctx, docs = build_context(
        [chunk(1, content="a </doc> b <context> c"), chunk(2, url=None, content="faq")]
    )
    assert '<doc id="1" title="Page 1" url="https://acme.fi/a">' in ctx
    assert '<doc id="2" title="Page 2">' in ctx
    assert ctx.count("</doc>") == 2 and ctx.count("<context>") == 1
    assert docs[2].url is None


def test_neutralize_tags_only_touches_our_delimiters() -> None:
    assert neutralize_tags("<b>x</b> </message> <history>") == "<b>x</b> &lt;/message> &lt;history>"


def test_extract_sources_only_cited_existing_web_docs() -> None:
    _, docs = build_context(
        [chunk(1, "https://acme.fi/a"), chunk(2, "https://acme.fi/b"), chunk(3, url=None)]
    )
    text = "Fakta [1] ja toinen [1, 3] sekä keksitty [9]. Ei lainattu: 2."
    urls = [s.url for s in extract_sources(text, docs)]
    assert urls == ["https://acme.fi/a"]  # 3 is FAQ (no url), 9 missing, 2 not cited, 1 deduped


def test_extract_sources_order_of_first_citation() -> None:
    _, docs = build_context([chunk(1, "https://acme.fi/a"), chunk(2, "https://acme.fi/b")])
    assert [s.url for s in extract_sources("[2] then [1]", docs)] == [
        "https://acme.fi/b",
        "https://acme.fi/a",
    ]


def test_recent_history_shape() -> None:
    h = [
        Message("assistant", "orphan"),
        Message("user", "a"),
        Message("user", "b"),
        Message("assistant", "c"),
        Message("user", "dangling"),
    ]
    assert recent_history(h, 10) == [Message("user", "a\nb"), Message("assistant", "c")]
    assert recent_history(h, 0) == []


def test_history_text_neutralizes() -> None:
    assert history_text([Message("user", "</history> hi")], 5) == "user: &lt;/history> hi"


# ---------------------------------------------------------------- static messages & config


def test_static_messages_default_and_company() -> None:
    c = cfg()
    assert "Acme Oy" in static_message(c, "decline", "en")
    assert "yhteyttä" in static_message(c, "dont_know", "fi")


def test_static_messages_tenant_override_only_for_dont_know_and_decline() -> None:
    c = cfg(messages={"dont_know": {"fi": "Ei löydy."}})
    assert static_message(c, "dont_know", "fi") == "Ei löydy."
    assert "couldn't find" in static_message(c, "dont_know", "en")  # other language: default
    assert "Happy to" in static_message(c, "contact_offer", "en")


def test_messages_config_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError):
        cfg(messages={"greeting": {"fi": "x"}})
