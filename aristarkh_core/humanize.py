"""
Детерминированная «очеловечивающая» постобработка текста Аристарха — без LLM и без токенов.

Длинное тире «—» в чате выдаёт машинный или редакторский текст: живые люди в мессенджерах
почти всегда ставят дефис, а автор проекта — ещё и «=» или «:». Здесь все тире
заменяются по этим правилам.
"""

import random
import re

# Как заменяется тире между словами (« — »): дефис чаще всего, иногда «=» или двоеточие
# «=» и «:» реже: «фастфуд = это конвейер» читается как сбой (аудит постов 25.10)
DASH_STYLES = ((" - ", 0.8), (" = ", 0.1), (": ", 0.1))
# Перед «это» только дефис: «X - это Y», а не «X = это Y» или «X: это Y»
_DASH_BEFORE_ETO = re.compile("[ \u00a0]+[\u2014\u2013\u2012\u2015][ \u00a0]+(?=это\\b)", re.IGNORECASE)

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
    text = _DASH_BEFORE_ETO.sub(" - ", text)
    used_eq = [False]

    def pick(m):
        # «=» не больше одного раза на текст: четыре «=» в одном посте выглядят как сбой
        style = rng.choices(styles, weights=weights, k=1)[0]
        if style == ": ":
            # Второе двоеточие в одном предложении («формата: сводится к одному:») читается как сбой
            left = re.split(r"[.!?…\n]", m.string[:m.start()])[-1]
            right = re.split(r"[.!?…\n]", m.string[m.end():])[0]
            if ":" in left or ":" in right:
                return " - "
        if style == " = ":
            if used_eq[0]:
                return " - "
            used_eq[0] = True
        return style

    text = _SPACED_DASH.sub(pick, text)
    # «=» модель пишет и сама (правило «дефис, двоеточие или =»): перед «это» - дефис, и не больше одного «=» на текст
    text = re.sub(r"[ \u00a0]+=[ \u00a0]+(?=это\b)", " - ", text)
    seen = [False]

    def one_eq(m):
        if seen[0]:
            return " - "
        seen[0] = True
        return m.group(0)

    text = re.sub(r"[ \u00a0]+=[ \u00a0]+", one_eq, text)
    text = _NUM_RANGE.sub("-", text)
    return _ANY_DASH.sub("-", text)
