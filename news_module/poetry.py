"""
Цитаты любимых поэтов в постах канала (по умолчанию 5% постов, POETRY_PROB).

Банк цитат лежит рядом с файлом личности: persona_private/poetry_ru.json (приватно, не в git).
В нём только фрагменты, сверенные по двум источникам. В пост со стихами модель получает несколько
кандидатов и выбирает тот, что ложится в мысль, а код проверяет, что цитата приведена дословно.
Неточная строка Есенина для этого автора хуже, чем никакой: такой пост переписывается,
а если не вышло, пишется заново без стихов.

Формат банка: [{"id", "poet", "work", "year", "lines"}], строки фрагмента через \n.
Длинные тире в строках при загрузке заменяются на дефис: в канале длинных тире нет,
а постобработка иначе превратила бы их внутри стиха в «=» или «:».
"""

import json
import os
import random
import re
from difflib import SequenceMatcher

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BANK_PATH = os.path.join(ROOT, os.getenv("POETRY_BANK", "persona_private/poetry_ru.json"))

# Любимые поэты (библия, §13): Есенин №1, Лермонтов №2, ещё Пушкин
POET_WEIGHTS = {"Есенин": 0.45, "Лермонтов": 0.35, "Пушкин": 0.2}
POET_NAME_RE = re.compile(r"есенин|лермонтов|пушкин", re.IGNORECASE)
# Форматы, где цитате есть место (в «Уколе», «Ставке» и «Деньгах и цифрах» стихи лишние)
ELIGIBLE_FORMATS = {"producer_fix", "twist", "story", "against", "memory", "question"}
MISQUOTE_FIX = (
    "цитата из стихов приведена неточно: приведи её дословно, слово в слово, как в блоке [СТИХИ В ЭТОМ ПОСТЕ], "
    "каждую строку стиха с новой строки, или убери цитату и упоминание поэта совсем"
)
NO_VERSE_RULE = "Стихов не цитируй: ни строчки."


def _env_prob() -> float:
    try:
        return float(os.getenv("POETRY_PROB", "") or 0.05)
    except ValueError:
        return 0.05


POETRY_PROBABILITY = _env_prob()


def norm(text: str) -> str:
    """Для сравнения цитат: регистр, ё/е, пунктуация и переносы строк не важны, важны слова и их порядок."""
    return re.sub(r"[^а-яa-z0-9]+", " ", (text or "").lower().replace("ё", "е")).strip()


def poet_key(poet: str) -> str:
    return (poet or "").split()[-1] if poet else ""


def load_bank() -> list[dict]:
    try:
        with open(BANK_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    bank = []
    for q in data:
        lines = "\n".join(re.sub(r"\s*[—–]\s*", " - ", ln).rstrip() for ln in (q.get("lines") or "").split("\n")).strip()
        if q.get("id") and lines:
            bank.append({**q, "lines": lines})
    return bank


def by_id(bank: list[dict], quote_id: str | None) -> dict | None:
    return next((q for q in bank if q["id"] == quote_id), None)


def pick_candidates(bank: list[dict], exclude_ids: set[str], n: int = 6, rng=random) -> list[dict]:
    """Кандидаты для поста: доля каждого поэта по POET_WEIGHTS, без недавно процитированных фрагментов."""
    pool = [q for q in bank if q["id"] not in exclude_ids] or list(bank)
    chosen = []
    while pool and len(chosen) < n:
        per_poet = {}
        for q in pool:
            per_poet[poet_key(q["poet"])] = per_poet.get(poet_key(q["poet"]), 0) + 1
        weights = [POET_WEIGHTS.get(poet_key(q["poet"]), 0.1) / per_poet[poet_key(q["poet"])] for q in pool]
        q = rng.choices(pool, weights=weights, k=1)[0]
        chosen.append(q)
        pool.remove(q)
    return chosen


def prompt_block(candidates: list[dict]) -> str:
    items = "\n\n".join(
        f"{i}) {poet_key(q['poet'])}, «{q['work']}»:\n{q['lines']}" for i, q in enumerate(candidates, 1)
    )
    return (
        "[СТИХИ В ЭТОМ ПОСТЕ]\n"
        "В этом посте к месту процитируй любимого поэта: одну цитату из списка ниже, ту, что точнее всего ложится "
        "в твою мысль. Цитируй дословно, слово в слово, как в списке: весь фрагмент или одну-две строки подряд. "
        "Каждую строку стиха пиши с новой строки, как в книге, без кавычек. Поэта назови по-человечески, как в разговоре "
        "(«как у Есенина», «Лермонтов это знал ещё тогда»), или не называй, если строки и так узнаваемы. "
        "Не объясняй цитату и не пересказывай её прозой. Цитата не входит в лимит длины.\n\n" + items
    )


def _lines(entry: dict) -> list[list[str]]:
    return [ln.split() for ln in (norm(x) for x in entry["lines"].split("\n")) if ln]


def _post_lines(text: str) -> list[list[str]]:
    """Строки поста (по переносам и « / ») плюс отдельно всё, что стоит в «ёлочках»: так цитата внутри прозы тоже строка."""
    parts = re.split(r"\n| / ", text or "") + re.findall(r"«([^»]*)»", text or "")
    return [ln.split() for ln in (norm(x) for x in parts) if ln]


def _find(seq: list[str], sub: list[str]) -> int:
    n = len(sub)
    return next((i for i in range(len(seq) - n + 1) if seq[i:i + n] == sub), -1)


def _same_word(a: str, b: str) -> bool:
    """Одно слово в другой форме: «расстояньи» и «расстоянии», «платят» и «платить»."""
    if a == b:
        return True
    k = 0
    while k < min(len(a), len(b)) and a[k] == b[k]:
        k += 1
    return k >= 4 and k >= min(len(a), len(b)) - 2


def _near(line: list[str], post: list[str]) -> bool:
    """Строка стиха в посте почти есть, но не дословно: по порядку совпали все слова, кроме одного, с точностью до формы."""
    n = len(line)
    if n < 3:
        return False
    need = max(3, n - 1)
    for w in (n - 1, n, n + 1):
        for i in range(len(post) - w + 1):
            window = post[i:i + w]
            if window == line or "|" in window:
                continue
            canon = [next((e for e in line if _same_word(e, x)), x) for x in window]
            matched = sum(b.size for b in SequenceMatcher(None, line, canon, autojunk=False).get_matching_blocks())
            if matched >= need:
                return True
    return False


def check(text: str, entries: list[dict], near_entries: list[dict] | None = None) -> tuple[str, dict | None]:
    """
    Цитата из стихов в тексте: ("ok", фрагмент), ("misquote", фрагмент) или ("absent", None).
    - ok: хотя бы одна строка фрагмента из entries стоит в тексте дословно (регистр, ё и пунктуация не важны).
    - misquote: строка фрагмента из near_entries (по умолчанию entries) почти есть в стихоподобной строке поста
      (не длиннее строки стиха больше чем на три слова): по порядку совпали все слова, кроме одного,
      или слово стоит в другой форме («на расстоянии» вместо «на расстояньи»).
      Дословные строки перед этой проверкой вырезаются, чтобы точная цитата одного стиха
      не выглядела неточной цитатой другого.
    """
    near_entries = entries if near_entries is None else near_entries
    posts = _post_lines(text)
    # Цитата поста - фрагмент с наибольшим числом дословных строк: короткая строка вроде «Лицом к лицу»
    # может случайно стоять в прозе рядом с настоящей цитатой другого стиха
    best, found = 0, None
    for entry in entries:
        hits = sum(any(_find(post, line) >= 0 for post in posts) for line in _lines(entry))
        if hits > best:
            best, found = hits, entry
    masked = []
    for post in posts:
        post = list(post)
        for entry in entries + [e for e in near_entries if e not in entries]:
            for line in _lines(entry):
                i = _find(post, line)
                while i >= 0:
                    post[i:i + len(line)] = ["|"]
                    i = _find(post, line)
        masked.append(post)
    # Неточность ищется только в стихоподобных строках: отдельная короткая строка или текст в «ёлочках».
    # Длинная фраза прозы с тремя словами из стиха («Не каждому дано держать сериал...») - аллюзия, а не цитата
    for entry in near_entries:
        if any(_near(line, post) for line in _lines(entry) for post in masked if len(post) <= len(line) + 3):
            return "misquote", entry
    return ("ok", found) if found else ("absent", None)


def verse_dashes(text: str, bank: list[dict]) -> str:
    """Тире в строках текста, где стоит цитата из банка, - дефисом, детерминированно."""
    quotes = [" ".join(line) for entry in bank for line in _lines(entry) if len(line) >= 2]
    if not quotes:
        return text
    out = []
    for ln in (text or "").split("\n"):
        nl = f" {norm(ln)} "
        if any(f" {q} " in nl for q in quotes):
            ln = re.sub(r"[ \u00a0]*[—–‒―][ \u00a0]*", " - ", ln).rstrip()
        out.append(ln)
    return "\n".join(out)


def mentions_poet(text: str) -> bool:
    return bool(POET_NAME_RE.search(text or ""))


def recent_ids(history: list[dict]) -> set[str]:
    return {h["poetry"]["id"] for h in history if isinstance(h.get("poetry"), dict) and h["poetry"].get("id")}
