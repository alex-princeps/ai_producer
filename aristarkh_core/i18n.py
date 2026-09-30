"""
UI strings in several languages.

The interface language is chosen per user from Telegram's `language_code`:
Russian for ru/uk/be/kk, English for everyone else. APP_LANGUAGE sets the
default when the user's language is unknown. (The persona itself answers in
the language the user writes in; that is a prompt rule, not a UI string.)
"""

import json
import os
from pathlib import Path

LOCALES_DIR = Path(__file__).parent.parent / "locales"
RU_LIKE = ("ru", "uk", "be", "kk")


def _load_all() -> dict[str, dict]:
    locales = {}
    for path in LOCALES_DIR.glob("*.json"):
        with open(path, "r", encoding="utf-8") as f:
            locales[path.stem] = json.load(f)
    return locales


_locales = _load_all()
DEFAULT_LANG = os.getenv("APP_LANGUAGE", "ru")


def user_lang(user) -> str:
    """Interface language for a Telegram user (aiogram User or None)."""
    code = (getattr(user, "language_code", None) or "").lower()
    if not code:
        return DEFAULT_LANG
    return "ru" if code.startswith(RU_LIKE) else "en"


def get_text(key: str, lang: str | None = None, **kwargs) -> str:
    messages = _locales.get(lang or DEFAULT_LANG) or _locales.get("ru") or {}
    text = messages.get(key) or _locales.get("ru", {}).get(key) or key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text


def variants(key: str) -> set[str]:
    """The same button label in every language, for matching incoming button presses."""
    return {m[key] for m in _locales.values() if key in m}
