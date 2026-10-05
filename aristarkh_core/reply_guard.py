"""
Последний фильтр ответа в чате: ловит то, что промпт иногда пропускает.

- снисходительные обращения («милочка», «дорогуша», «барышня»…);
- имя Картозии: бот не называет людей и материалы, на которых учился
  (если собеседник сам заговорил о Картозии, упоминание допустимо);
- «я живой человек», «я не ИИ»: бот честно говорит, что он ИИ-продюсер;
- «образ основан на реальном человеке», «синтетическая личность», ссылки на «создателей».

Найденное правится одним вызовом Flash. Если правка не удалась,
обращения вырезаются, а остальные проблемные предложения выбрасываются.
"""

import re

from aristarkh_core.config import logger

ADDRESS = re.compile(r"\b(?:милочк|дорогуш|барышн|деточк|малышк|мальчонк|мамкин)\w*|\bмалец\b", re.IGNORECASE)
KARTOZIA = re.compile(r"картоз\w*|kartoz\w*", re.IGNORECASE)

CHECKS = [
    ("address", ADDRESS,
     "снисходительные обращения (милочка, дорогуша, барышня и т.п.): замени на нейтральное обращение или убери"),
    ("kartozia", KARTOZIA,
     "упоминание Картозии: убери имя и любые намёки на то, у кого или на чём ты учился"),
    ("human_claim", re.compile(
        r"\bя\s+(?:[—-]\s*)?(?:живой|реальный|настоящий)\s+человек|из\s+плоти\s+и\s+крови"
        r"|\bя\s+не\s+(?:ии|бот|нейросеть|программа|искусственный\s+интеллект)\b", re.IGNORECASE),
     "утверждение, что ты человек или не ИИ: ты ИИ-продюсер, скажи об этом честно или убери фразу"),
    ("real_person", re.compile(
        r"на\s+основе\s+реальн\w+\s+(?:человек|люд|продюсер)\w*|синтетическ\w+\s+личност\w*", re.IGNORECASE),
     "утверждение, что твой образ основан на реальном человеке, или слова «синтетическая личность»: убери"),
    ("creators", re.compile(r"\b(?:мои|моих|моим|мой)\s+(?:создател|разработчик|сценарист)\w*", re.IGNORECASE),
     "ссылки на своих «создателей», «разработчиков» или «сценаристов»: убери"),
]

EDIT_INSTRUCTION = (
    "Ниже ответ ИИ-продюсера Аристарха Градова клиенту. Отредактируй его минимально и исправь только это: {problems}. "
    "Сохрани смысл, структуру, язык и характерный стиль. Верни только исправленный текст, без пояснений."
)


def find_problems(text: str, user_text: str = "") -> list[tuple[str, re.Pattern, str]]:
    found = []
    for name, rx, fix in CHECKS:
        if name == "kartozia" and KARTOZIA.search(user_text or ""):
            continue  # собеседник сам заговорил о Картозии
        if rx.search(text or ""):
            found.append((name, rx, fix))
    return found


def _strip(text: str, problems: list[tuple[str, re.Pattern, str]]) -> str:
    """Запасной путь без LLM: вырезать обращения и выбросить предложения с остальными проблемами."""
    if any(name == "address" for name, _, _ in problems):
        text = re.sub(r"(?:,\s*)?(?:" + ADDRESS.pattern + r")(?:\s*,)?", "", text, flags=re.IGNORECASE)
    others = [rx for name, rx, _ in problems if name != "address"]
    if others:
        sentences = re.split(r"(?<=[.!?…])\s+", text)
        text = " ".join(s for s in sentences if not any(rx.search(s) for rx in others))
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    # После вырезания обращения предложение может начаться со строчной буквы
    return re.sub(r"(^|[.!?…]\s+)([a-zа-яё])", lambda m: m.group(1) + m.group(2).upper(), text)


async def polish_reply(llm, text: str, user_text: str = "") -> str:
    problems = find_problems(text, user_text)
    if not problems:
        return text
    logger.info(f"🧹 [ReplyGuard] Правим ответ: {[name for name, _, _ in problems]}")
    edited = await llm.edit_text(EDIT_INSTRUCTION.format(problems="; ".join(fix for _, _, fix in problems)), text)
    if edited and not find_problems(edited, user_text):
        return edited
    left = find_problems(edited or text, user_text)
    return _strip(edited or text, left or problems)
