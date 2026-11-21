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


def _sanitize_deterministic(text: str, user_text: str = "") -> str:
    """Детерминированная очистка самореферентного IT-жаргона и анахронизмов дат (0 токенов)."""
    if not text:
        return text
    # 1. Замена самореферентного IT-жаргона на язык закрытого продюсерского кабинета
    replacements = [
        (r"(?i)\bв\s+рамках\s+(?:нашего\s+|одного\s+|единого\s+)?треда\b", "в рамках вашего закрытого досье"),
        (r"(?i)\bедин\w+\s+тред\w*\b", "закрытого досье проекта"),
        (r"(?i)\bконтекстн\w+\s+окн\w*\b", "памяти закрытого досье"),
        (r"(?i)\bсистемн\w+\s+промпт\w*\b", "внутреннего регламента кабинета"),
        (r"(?i)\bпамят\w+\s+системы\b", "память закрытого досье"),
        (r"(?i)\bцифров\w+\s+разум\w*\b", "ИИ-продюсер"),
        (r"(?i)\bсистемн\w+\s+сч[её]тчик\w*\b", "регламент кабинета"),
        (r"(?i)\bмашина\s+фиксирует\b", "регламент кабинета учитывает"),
        (r"(?i)\bканцелярск\w+\s+крыс\w*\b", "кабинетных бюрократов"),
        (r"(?i)\bкафедральн\w+\s+крыс\w*\b", "кафедральных теоретиков"),
    ]
    for pat, repl in replacements:
        text = re.sub(pat, repl, text)

    # 2. Если пользователь сам не упоминал 2023/2024/2025 год, не даём модели называть их текущим годом
    for old_year in ("2023", "2024", "2025"):
        if old_year not in (user_text or ""):
            text = re.sub(rf"\b{old_year}\s+(год(?:а|у|ом|е)?)\b", rf"2026 \1", text)
            text = re.sub(rf"(?i)\bв\s+{old_year}\b", "в 2026", text)
    return text


RE_STALO_HEADER = re.compile(
    r"(?m)^(?:#{1,4}\s*|\*\*|\[|>?\s*)?(?:БЫЛО\s*(?:->|→|—|-)\s*)?СТАЛО\b",
    re.IGNORECASE,
)
RE_QUOTED_PHRASE = re.compile(r"[«\"']([^«»\"'\n]{3,45})[»\"']")
RE_FORBID_RULE_HINT = re.compile(r"(?i)(запре[тщ]|не\s+использ|нельзя|без\s+слов|убер|стоп-слов|избега)")


def extract_forbidden_phrases_from_rules(editorial_rules: list[str] | None) -> list[str]:
    """Извлекает запрещённые клиентом фразы из правил редполитики (0 токенов)."""
    phrases: list[str] = []
    for rule in editorial_rules or []:
        if not RE_FORBID_RULE_HINT.search(rule):
            continue
        for m in RE_QUOTED_PHRASE.finditer(rule):
            p = m.group(1).strip()
            if len(p) >= 4 and p.lower() not in {x.lower() for x in phrases}:
                phrases.append(p)
        # Также ловим перечисление через двоеточие: «запрещены слова: инвестиция в себя, последний вагон»
        m_list = re.search(r"(?i)(?:запрещен\w*\s+слова|стоп-слова|не\s+используй\s+слова)\s*[:—-]\s*([^.\n;]+)", rule)
        if m_list:
            for item in m_list.group(1).split(","):
                clean = item.strip(" «»\"'-")
                if 4 <= len(clean) <= 45 and clean.lower() not in {x.lower() for x in phrases}:
                    phrases.append(clean)
    return phrases


def find_editorial_stopwords_in_stalo(text: str, editorial_rules: list[str] | None) -> list[str]:
    """Ловит «эффект розового слона» (упоминание запретного слова даже через отрицание внутри блока СТАЛО)."""
    if not text or not editorial_rules:
        return []
    m = RE_STALO_HEADER.search(text)
    target = text[m.end():] if m else text
    # Отрезаем финальный мостик после визуальной отбивки • • •
    if "• • •" in target:
        target = target.split("• • •", 1)[0]
    target_norm = target.lower().replace("ё", "е")
    hits = []
    for phrase in extract_forbidden_phrases_from_rules(editorial_rules):
        p_norm = phrase.lower().replace("ё", "е")
        # Сравниваем по корням слов (без последних 1-2 букв окончания для 2+ словных фраз)
        words = [w for w in re.findall(r"[а-яa-z0-9]+", p_norm) if len(w) >= 3]
        if not words:
            continue
        pat = r"\s+".join(re.escape(w[:-2] if len(w) >= 6 else (w[:-1] if len(w) >= 5 else w)) + r"[а-яa-z]*" for w in words)
        if re.search(pat, target_norm):
            hits.append(phrase)
    return hits


async def polish_reply(
    llm,
    text: str,
    user_text: str = "",
    editorial_rules: list[str] | None = None,
) -> str:
    text = _sanitize_deterministic(text, user_text)
    problems = find_problems(text, user_text)
    fix_instructions = [fix for _, _, fix in problems]

    stop_hits = find_editorial_stopwords_in_stalo(text, editorial_rules)
    if stop_hits:
        joined_stops = ", ".join(f"«{s}»" for s in stop_hits)
        fix_instructions.append(
            f"внутри блока СТАЛО обнаружены запрещённые редполитикой клиента фразы ({joined_stops}): "
            "перепиши только эти 1-2 предложения так, чтобы запрещённых слов НЕ было даже через отрицание («я не буду говорить про...»)"
        )

    if not fix_instructions:
        return text

    logger.info(f"🧹 [ReplyGuard] Правим ответ: {[name for name, _, _ in problems]} + stops={stop_hits}")
    edited = await llm.edit_text(EDIT_INSTRUCTION.format(problems="; ".join(fix_instructions)), text)
    if edited and not find_problems(edited, user_text) and not find_editorial_stopwords_in_stalo(edited, editorial_rules):
        return edited
    left = find_problems(edited or text, user_text)
    return _strip(edited or text, left or problems) if (left or problems) else (edited or text)
