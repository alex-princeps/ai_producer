"""
Детерминированная «очеловечивающая» постобработка текста Аристарха — без LLM и без токенов.

Длинное тире «—» в чате выдаёт машинный или редакторский текст: живые люди в мессенджерах
почти всегда ставят дефис, а автор проекта — ещё и «=» или «:». Здесь все тире
заменяются по этим правилам.
"""

import random
import re

# Как заменяется тире между словами (« — »): короткое тире с пробелами чаще всего, иногда двоеточие.
# Знак «=» между словами отключён: в диалогах чата «юристы не кричали = они говорили» считывается как баг.
DASH_STYLES = ((" - ", 0.9), (": ", 0.1))
# Перед «это» только дефис: «X - это Y», а не «X: это Y»
_DASH_BEFORE_ETO = re.compile("[ \u00a0]+[\u2014\u2013\u2012\u2015][ \u00a0]+(?=это\\b)", re.IGNORECASE)
_WORD_EQ_WORD = re.compile(r"(?<=[а-яА-ЯёЁa-zA-Z»\")])[ \u00a0]*=[ \u00a0]*(?=[«\"(а-яА-ЯёЁa-zA-Z])")

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
    # Вычищаем стилистический штамп «слово = слово» (не трогая URL-параметры без пробелов и арифметику с цифрами)
    text = re.sub(r"[ \u00a0]+=[ \u00a0]+(?=это\b)", " - ", text)
    text = re.sub(r"(?<=[а-яА-ЯёЁa-zA-Z»\")])[ \u00a0]+=[ \u00a0]+(?=[«\"(а-яА-ЯёЁa-zA-Z])", " - ", text)
    # Зачистка сырой Markdown-разметки (### заголовки, обратные кавычки `, цитаты > и звёздочки *)
    text = re.sub(r"(?m)^[ \t]*#{1,6}[ \t]+", "", text)
    text = re.sub(r"(?m)^[ \t]*>[ \t]+", "", text)
    text = text.replace("`", "").replace("*", "")
    text = _NUM_RANGE.sub("-", text)
    return _ANY_DASH.sub("-", text)
