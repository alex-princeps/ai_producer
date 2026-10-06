"""
Детерминированная «очеловечивающая» постобработка текста Аристарха — без LLM и без токенов.

Длинное тире «—» в чате выдаёт машинный или редакторский текст: живые люди в мессенджерах
почти всегда ставят дефис, а автор проекта — ещё и «=» или «:». Здесь все тире
заменяются по этим правилам.
"""

import random
import re

# Как заменяется тире между словами (« — »): дефис чаще всего, иногда «=» или двоеточие
DASH_STYLES = ((" - ", 0.7), (" = ", 0.15), (": ", 0.15))

# Длинное (—), среднее (–), цифровое (‒) тире и горизонтальная черта (―)
_SPACED_DASH = re.compile(r"[  ]+[—–‒―][  ]+")
_LINE_START_DASH = re.compile(r"(?m)^([ \t]*)[—–‒―][  ]*")
_NUM_RANGE = re.compile(r"(?<=\d)[—–‒―](?=\d)")
_ANY_DASH = re.compile(r"[—–‒―]")


def humanize_punctuation(text: str, rng: random.Random | None = None) -> str:
    """Убирает длинные и средние тире: « — » → « - » / « = » / «: », «—» в начале строки → «- »."""
    if not text:
        return text
    rng = rng or random
    styles, weights = zip(*DASH_STYLES)

    text = _LINE_START_DASH.sub(lambda m: f"{m.group(1)}- ", text)
    text = _SPACED_DASH.sub(lambda m: rng.choices(styles, weights=weights, k=1)[0], text)
    text = _NUM_RANGE.sub("-", text)
    return _ANY_DASH.sub("-", text)
