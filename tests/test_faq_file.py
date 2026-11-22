"""Structural checks for the committed demo FAQ (faq/bizvision.yaml).

These keep passing after the {{PLACEHOLDERS}} have been replaced with real values; they check
shape, not content.
"""

from pathlib import Path

import pytest

from cli.faq import parse_faq_yaml
from core.ingest.service import FAQ_PLACEHOLDER, MAX_FAQ_CHARS, faq_key

FAQ_FILE = Path(__file__).resolve().parent.parent / "faq" / "bizvision.yaml"


@pytest.fixture(scope="module")
def entries():
    return parse_faq_yaml(FAQ_FILE)


def test_file_parses_and_has_entries(entries) -> None:
    assert len(entries) >= 10
    assert all(e.question.strip() and e.answer.strip() for e in entries)
    assert all(len(e.question) + len(e.answer) <= MAX_FAQ_CHARS for e in entries)


def test_questions_are_unique_after_normalisation(entries) -> None:
    keys = [faq_key(e.question) for e in entries]
    assert len(keys) == len(set(keys))


def test_every_entry_exists_in_both_languages(entries) -> None:
    half = len(entries) // 2
    assert len(entries) % 2 == 0
    fi, en = entries[:half], entries[half:]
    # Finnish block first, English block second, same order: crude but catches a forgotten entry
    finnish = {"ja", "tai", "on", "ei", "kun"}
    assert all("ä" in e.answer or "ö" in e.answer or finnish & set(e.answer.split()) for e in fi)
    english = {"the", "and", "is", "you", "your", "we", "our"}
    assert all(english & set(e.answer.lower().split()) for e in en)


def test_placeholders_are_well_formed(entries) -> None:
    for e in entries:
        leftover = FAQ_PLACEHOLDER.sub("", e.question + e.answer)
        assert "{{" not in leftover and "}}" not in leftover, e.question  # e.g. "{ {X}}" typo


def test_no_prices_are_invented(entries) -> None:
    # every euro amount must come from a placeholder, never be hard-coded in the draft
    for e in entries:
        text = FAQ_PLACEHOLDER.sub("", e.answer)
        assert "€" not in text and " eur" not in text.lower(), e.question
