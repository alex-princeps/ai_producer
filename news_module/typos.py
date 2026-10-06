"""
Редкая человеческая опечатка в посте канала.

Живой автор иногда публикует пост с опечаткой, а через несколько минут замечает её и правит
пост (в Telegram появляется пометка «изменено»). Здесь только сама опечатка: одна на пост,
в длинном слове из середины текста. Правку делает publisher_cron.py через edit_message_text.

Если правка не удалась (сбой сети, перезапуск сервера), followup_cron.py повторит её позже:
правильный HTML хранится в истории поста (typo.fix_html).

Чего опечатка не трогает:
- первую фразу (она видна в уведомлении и превью, а уведомление после правки не меняется);
- слова с заглавной буквы, цифры, @, ссылки, текст в «ёлочках» и строки цитаты из стихов;
- короткие слова (меньше 6 букв): в них опечатка выглядит неряшливо, а не случайно.
"""

import asyncio
import random
import re
from datetime import datetime, timezone

from news_module.state_io import update_json

# Соседние клавиши на русской раскладке ЙЦУКЕН: по ряду чаще, по вертикали реже
_ROWS = ["йцукенгшщзхъ", "фывапролджэ", "ячсмитьбю"]


def _neighbors() -> dict[str, list[str]]:
    near: dict[str, list[str]] = {}
    for r, row in enumerate(_ROWS):
        for i, ch in enumerate(row):
            same = [row[j] for j in (i - 1, i + 1) if 0 <= j < len(row)]
            above = [_ROWS[r - 1][j] for j in (i, i + 1) if r > 0 and j < len(_ROWS[r - 1])]
            below = [_ROWS[r + 1][j] for j in (i - 1, i) if r < 2 and 0 <= j < len(_ROWS[r + 1])]
            near[ch] = same * 3 + above + below  # соседи по ряду втрое вероятнее
    return near


NEAR = _neighbors()
KINDS = (("swap", 0.35), ("neighbor", 0.35), ("drop", 0.2), ("double", 0.1))
WORD_RE = re.compile(r"(?<![\w@#/.-])[а-яё]{6,}(?![\w@-])")
GUILLEMETS_RE = re.compile(r"«[^»]*»")


def _norm(s: str) -> str:
    return re.sub(r"[^а-яa-z0-9]+", " ", s.lower().replace("ё", "е")).strip()


def _protected_spans(text: str, protected_lines: list[str]) -> list[tuple[int, int]]:
    spans = [m.span() for m in GUILLEMETS_RE.finditer(text)]
    # Первая фраза: видна в уведомлении
    first = re.search(r"[.!?…](?:\s|$)", text)
    spans.append((0, first.end() if first else len(text)))
    # Строки, в которых стоит цитата из стихов
    quotes = [_norm(q) for q in protected_lines if len(_norm(q).split()) >= 2]
    pos = 0
    for line in text.split("\n"):
        nl = _norm(line)
        if nl and any(q in nl or nl in q for q in quotes):
            spans.append((pos, pos + len(line)))
        pos += len(line) + 1
    return spans


def _apply(word: str, kind: str, rng: random.Random) -> str | None:
    n = len(word)
    if kind == "swap":
        j = rng.randint(1, n - 2)
        if word[j] == word[j + 1]:
            return None
        return word[:j] + word[j + 1] + word[j] + word[j + 2:]
    if kind == "neighbor":
        j = rng.randint(1, n - 1)
        options = NEAR.get(word[j])
        if not options:
            return None
        return word[:j] + rng.choice(options) + word[j + 1:]
    if kind == "drop":
        j = rng.randint(1, n - 2)
        return word[:j] + word[j + 1:]
    if kind == "double":
        j = rng.randint(1, n - 2)
        return word[:j + 1] + word[j] + word[j + 1:]
    return None


def make_typo(text: str, protected_lines: list[str] | None = None,
              rng: random.Random | None = None) -> tuple[str, dict] | None:
    """Одна опечатка в тексте: (текст с опечаткой, {"word", "typo", "kind"}) или None, если ставить некуда."""
    rng = rng or random
    spans = _protected_spans(text, protected_lines or [])
    end_limit = int(len(text) * 0.9)  # и не в самом конце: последнюю фразу автор перечитывает
    words = [
        m for m in WORD_RE.finditer(text)
        if m.end() <= end_limit and not any(a <= m.start() < b or a < m.end() <= b for a, b in spans)
    ]
    if not words:
        return None
    m = rng.choice(words)
    kinds, weights = zip(*KINDS)
    for _ in range(6):
        kind = rng.choices(kinds, weights=weights, k=1)[0]
        typo = _apply(m.group(0), kind, rng)
        if typo and typo != m.group(0):
            return text[:m.start()] + typo + text[m.end():], {"word": m.group(0), "typo": typo, "kind": kind}
    return None


async def edit_back(bot, chat_id, message_id: int, correct_html: str, attempts: int = 3) -> bool:
    """Правка поста обратно на текст без опечатки. Повторяет при сетевых сбоях и просьбе Telegram подождать."""
    from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
    from aiogram.types import LinkPreviewOptions

    for attempt in range(attempts):
        try:
            await bot.edit_message_text(
                text=correct_html, chat_id=chat_id, message_id=message_id, parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            return True
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramBadRequest as e:
            # «not modified»: текст уже правильный (например, поправили руками). Остальное (пост удалён) не лечится повтором
            return "not modified" in str(e).lower()
        except Exception:
            await asyncio.sleep(30 * (attempt + 1))
    return False


def mark_typo_fixed(news_path: str, channel: str, message_id: int):
    """Отметить в истории постов, что опечатка исправлена (правильный HTML больше не нужен)."""
    fixed_at = datetime.now(timezone.utc).isoformat()

    def mark(data: dict):
        for h in data.get("post_history", []):
            if h.get("message_id") == message_id and h.get("channel") == channel and isinstance(h.get("typo"), dict):
                h["typo"].update(fixed=True, fixed_at=fixed_at)
                h["typo"].pop("fix_html", None)

    update_json(news_path, mark)
