import os
import json
from pathlib import Path

def load_locale():
    lang = os.getenv("APP_LANGUAGE", "ru")  # персона русскоязычная: по умолчанию ru
    locale_path = Path(__file__).parent.parent / "locales" / f"{lang}.json"
    if not locale_path.exists():
        locale_path = Path(__file__).parent.parent / "locales" / "en.json"
        if not locale_path.exists():
            return {}
            
    with open(locale_path, "r", encoding="utf-8") as f:
        return json.load(f)

_messages = load_locale()

def get_text(key, **kwargs):
    text = _messages.get(key, key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except KeyError:
            return text
    return text
