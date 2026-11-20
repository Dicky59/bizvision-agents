from core.ingest.language import detect_language, normalize_lang_tag


def test_hint_wins_and_is_normalised() -> None:
    assert detect_language("anything", "fi-FI") == "fi"
    assert detect_language("anything", "EN_us") == "en"


def test_unsupported_hint_falls_back_to_detection() -> None:
    assert normalize_lang_tag("sv") is None
    text = "Tarjoamme palvelut yrityksille ja autamme sinua tekoälyn käyttöönotossa myös meille."
    assert detect_language(text, "sv") == "fi"


def test_detects_finnish_and_english() -> None:
    fi = "Meidän palvelut sopivat pienille yrityksille, ja voit ottaa yhteyttä meille milloin vain."
    en = "Our services are designed for small businesses and you can contact us whenever you want."
    assert detect_language(fi) == "fi"
    assert detect_language(en) == "en"


def test_too_short_or_ambiguous_is_none() -> None:
    assert detect_language("Hei") is None
    assert detect_language("") is None
    assert detect_language("xyz qrs tuv wxy abc def ghi jkl mno") is None
