"""Static localized replies (no LLM call); `dont_know` and `decline` are tenant-overridable."""

from core.tenants.config import Lang, TenantConfig

_DEFAULTS: dict[str, dict[Lang, str]] = {
    "dont_know": {
        "fi": (
            "En valitettavasti löytänyt tähän vastausta sivuston sisällöstä. Haluatko, että "
            "joku meiltä ottaa sinuun yhteyttä? Voit jättää yhteystietosi alla olevalla "
            "lomakkeella."
        ),
        "en": (
            "Sorry, I couldn't find an answer to that in our content. Would you like someone "
            "from our team to get back to you? You can leave your details in the form below."
        ),
    },
    "decline": {
        "fi": (
            "Voin auttaa vain yrityksen {company} palveluihin ja toimintaan liittyvissä "
            "kysymyksissä. Kysy vaikka palveluistamme tai siitä, miten yhteistyö etenee."
        ),
        "en": (
            "I can only help with questions about {company}'s services and work. "
            "Feel free to ask about our services or how a project works."
        ),
    },
    "contact_offer": {
        "fi": (
            "Mielellämme! Jätä yhteystietosi alla olevalla lomakkeella, niin otamme sinuun "
            "yhteyttä."
        ),
        "en": "Happy to! Leave your details in the form below and we'll get back to you.",
    },
    "too_long": {
        "fi": "Viesti on liian pitkä. Voisitko lyhentää sitä?",
        "en": "Your message is too long. Could you shorten it?",
    },
    "conversation_limit": {
        "fi": (
            "Tämä keskustelu on saavuttanut viestirajan. Jätä yhteystietosi alla olevalla "
            "lomakkeella, niin jatkamme asiaa sähköpostitse."
        ),
        "en": (
            "This conversation has reached its message limit. Leave your details in the form "
            "below and we'll continue by email."
        ),
    },
    "unavailable": {
        "fi": (
            "Avustaja ei ole juuri nyt käytettävissä. Jätä yhteystietosi alla olevalla "
            "lomakkeella, niin otamme sinuun yhteyttä."
        ),
        "en": (
            "The assistant is not available right now. Leave your details in the form below "
            "and we'll get back to you."
        ),
    },
}

_OVERRIDABLE = ("dont_know", "decline")


def static_message(cfg: TenantConfig, key: str, lang: Lang) -> str:
    """Tenant override if configured (dont_know / decline only), else the default."""
    if key in _OVERRIDABLE:
        override = getattr(cfg.assistant.messages, key).get(lang)
        if override:
            return override
    return _DEFAULTS[key][lang].replace("{company}", cfg.name)
