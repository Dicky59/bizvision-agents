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


FI_LONG = (
    "Käytämme 20–30 minuuttia prosessienne läpikäyntiin. Kerromme tarkalleen, missä kohdassa "
    "hukkaat aikaa ja miten se korjataan automaatiolla. Ei myyntipuheita, ei sitoumuksia. "
    "Jokainen yritys hukkaa aikaa eri kohdissa, ja usein ongelma ei ole työntekijöissä vaan "
    "siinä, että sama työ tehdään käsin joka kerta uudelleen."
)
EN_LONG = (
    "We spend 20-30 minutes finding exactly where the time is lost, then show you what to fix "
    "and what it is worth. No pitch, and no obligation for you or your team at all."
)


def test_confident_text_overrules_wrong_lang_attribute(caplog) -> None:
    import logging

    with caplog.at_level(logging.DEBUG, logger="core.ingest.language"):
        assert detect_language(FI_LONG, "en") == "fi"  # Finnish content, <html lang="en">
        assert detect_language(EN_LONG, "fi-FI") == "en"
    assert "overruled" in caplog.text
    assert "Käytämme" not in caplog.text  # never log page content


def test_matching_hint_is_unchanged_and_not_logged(caplog) -> None:
    import logging

    with caplog.at_level(logging.DEBUG, logger="core.ingest.language"):
        assert detect_language(FI_LONG, "fi") == "fi"
    assert "overruled" not in caplog.text


def test_unconfident_text_does_not_overrule_hint() -> None:
    short_fi = "Kerromme miten se toimii ja missä."  # too little text to overrule the hint
    assert detect_language(short_fi, "en") == "en"
    mixed = EN_LONG + " " + FI_LONG[:60]  # mostly English with a Finnish quote
    assert detect_language(mixed, "en") == "en"
