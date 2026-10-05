"""
Сборка авторского поста Аристарха для канала.

Что здесь решается (в отличие от прежнего единого промпта):
- формат поста выбирается случайно из нескольких и не повторяется подряд,
  у каждого формата своя длина — посты перестают быть одинаковыми «простынями»;
- тон выбирается из палитры, а не всегда «презрение»;
- стоп-лист клише: если пост их содержит, он один раз переписывается;
- начала последних постов передаются в промпт, чтобы не повторяться;
- байки из базы знаний — только в форматах, где они уместны, и без присвоения
  чужого опыта себе;
- в начале поста — ссылка на новость и свёрнутая цитата оригинала без служебных хвостов источника;
- в конце поста — вывод для читателя своими словами, без рубрик-меток: канал — голос живого автора;
- ссылка на бота в постах по умолчанию выключена (канал — витрина личности), включается CHANNEL_BOT_CTA;
- длинные тире заменяются на человеческие «-», «=», «:» (детерминированно, без LLM);
- в «Ставке» прогноз не может уйти в прошлое: годы раньше текущего → переписывание;
- «добивка»: через время Аристарх отвечает на собственный пост короткой фразой;
- «Разбор подписчика»: публичный ответ на вопрос читателя (generate_razbor).
"""

import html
import os
import random
import re
from datetime import datetime

import pytz

from aristarkh_core.config import logger
from aristarkh_core.humanize import humanize_punctuation
from aristarkh_core.prompts import Prompts
from news_module import columnist

TELEGRAM_LIMIT = 4096
QUOTE_MAX_CHARS = 500
HISTORY_SIZE = 10

# Для кого канал: от этого зависят примеры в выводе для читателя
AUDIENCE = (
    "авторы Telegram-каналов и начинающие блогеры, копирайтеры и маркетологи, "
    "владельцы небольших продакшенов и малого бизнеса (салоны красоты, юридические фирмы, небольшие стройки)"
)

# Форматы поста: инструкция, диапазон длины (символы), можно ли опираться на байку из базы, вес
POST_FORMATS = [
    {
        "id": "sting",
        "name": "Укол",
        "instruction": "Два–четыре предложения. Одна мысль, один хлёсткий вывод. Никаких вступлений и разгона.",
        "length": (200, 500), "story": False, "weight": 3,
    },
    {
        "id": "producer_fix",
        "name": "Разбор продюсера",
        "instruction": "Как продюсер: что здесь сделано не так и что бы ты сделал на месте героев новости — конкретные ходы, как на летучке.",
        "length": (800, 1500), "story": False, "weight": 4,
    },
    {
        "id": "twist",
        "name": "Докрутка",
        "instruction": "Мысленный эксперимент продюсера: возьми инфоповод (формат, скандал, бизнес-модель, чей-то личный бренд) и докрути его до хита. Что бы ты добавил, убрал, перевернул, чтобы это стало сильнее, дороже, громче? Конкретные ходы, а не общие слова.",
        "length": (700, 1400), "story": False, "weight": 4,
    },
    {
        "id": "story",
        "name": "Байка к месту",
        "instruction": "Одна история из профессиональной базы знаний, которая неожиданно объясняет новость. История — чужая: рассказывай её как услышанную от коллеги, никогда от первого лица. Затем короткий вывод.",
        "length": (800, 1500), "story": True, "weight": 2,
    },
    {
        "id": "bet",
        "name": "Ставка",
        "instruction": "Прогноз: чем это закончится и когда. Конкретно, с цифрой или сроком, как человек, готовый поспорить на деньги.",
        "length": (350, 800), "story": False, "weight": 2,
    },
    {
        "id": "against",
        "name": "Против течения",
        "instruction": "Займи неожиданную позицию: защити того, кого все ругают, или найди в новости то, что все проглядели. Без показного цинизма.",
        "length": (500, 1100), "story": False, "weight": 2,
    },
    {
        "id": "question",
        "name": "Вопрос залу",
        "instruction": "Короткое мнение и один острый вопрос к подписчикам, на который хочется ответить в комментариях.",
        "length": (250, 600), "story": False, "weight": 2,
    },
    {
        "id": "numbers",
        "name": "Деньги и цифры",
        "instruction": "Холодно, почти без эмоций: деньги, аудитория, охваты, кто на этом заработает и сколько. Если точных цифр нет — прикинь порядок и скажи, что это прикидка.",
        "length": (450, 1000), "story": False, "weight": 2,
    },
    {
        "id": "memory",
        "name": "Отзвук",
        "instruction": "Как эта новость отозвалась лично в тебе: ассоциация, настроение, воспоминание из твоей жизни (только из того, что ты можешь рассказывать о себе) — без имён реальных людей и без выдуманных встреч с ними.",
        "length": (500, 1100), "story": False, "weight": 1,
    },
]

TONES = [
    "холодная ирония", "азарт", "усталость и скука", "неожиданное уважение",
    "злорадство", "брезгливость", "ностальгия", "тревога", "весёлое хулиганство",
]

# Клише, которые превращали каждый пост в копию предыдущего
BANNED_PHRASES = [
    "саркастическ", "ходит байка", "ходят байки", "ходит отличная байка", "ходит эталонная",
    "в индустрии ходит", "кулуар", "жесткая правда", "жёсткая правда", "жесткую правду",
    "жёсткую правду", "вежливая ложь", "вежливой лжи", "вежливую ложь", "никакой морали",
    "индустрия переполнена", "святая наивность", "творческая импотенция",
    "продюсерская импотенция", "давайте будем честны", "на секундочку",
    "вызывает у меня лишь", "брезгливое разочарование",
    # обороты, по которым читатель узнаёт текст нейросети
    "давайте разбер", "давайте посмотрим", "важно понимать", "стоит отметить", "в заключение", "подводя итог",
]
MAX_DILETANT = 1  # слово-маркер допустимо не больше одного раза за пост

# Выдуманное личное знакомство с реальными людьми («я имел с ним дело», «мы с ней снимали»).
# Срабатывание только отправляет пост на переписывание, поэтому шаблоны намеренно широкие.
CONTACT_CLAIMS = re.compile(
    r"\bя\s+(?:лично\s+)?имела?\s+(?:с\s+\w+\s+)?дело\b"
    r"|\bя\s+(?:лично\s+)?(?:работал|снимал|продюсировал|знал|встречал|общался|дружил|пересекался)\w*"
    r"\s+(?:с\s+)?(?:ним|ней|ними|его|её|ее|их)\b"
    r"|\bмы\s+с\s+(?:ним|ней|ними)\b"
    r"|\bлично\s+(?:знал|знаком|работал|общался)\w*"
    r"|\bбыла?\s+(?:с\s+ним|с\s+ней|с\s+ними)\s+знаком\w*",
    re.IGNORECASE,
)
CONTACT_FIX = (
    "выдуманное личное знакомство с реальным человеком (встречи, совместная работа, разговоры) — "
    "убери, оставь только публичные факты"
)

URL_RE = re.compile(r"https?://\S+")


def choose_format(recent_format_ids: list[str]) -> dict:
    """Случайный формат по весам, без повтора двух последних."""
    blocked = set(recent_format_ids[-2:])
    pool = [f for f in POST_FORMATS if f["id"] not in blocked] or POST_FORMATS
    return random.choices(pool, weights=[f["weight"] for f in pool], k=1)[0]


def choose_tone(last_tone: str | None = None) -> str:
    return random.choice([t for t in TONES if t != last_tone] or TONES)


_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
           "августа", "сентября", "октября", "ноября", "декабря"]


def today_ru() -> str:
    now = datetime.now(pytz.timezone("Europe/Moscow"))
    return f"{now.day} {_MONTHS[now.month - 1]} {now.year} года"


def find_cliches(text: str) -> list[str]:
    low = text.lower()
    hits = [p for p in BANNED_PHRASES if p in low]
    if low.count("дилетант") > MAX_DILETANT:
        hits.append("дилетант (чаще одного раза)")
    return hits


def find_contact_claims(text: str) -> list[str]:
    return [m.group(0) for m in CONTACT_CLAIMS.finditer(text)]


HOOK_PROBABILITY = 0.25  # доля постов (кроме «Вопроса залу»), которые задают вопрос подписчикам
DAY_MENTION_PROBABILITY = 0.3  # в скольких постах можно мимоходом упомянуть свой день или погоду
CALLBACK_PROBABILITY = 0.15    # в скольких постах можно сослаться на свой недавний пост
PERSONAL_HOOK_PROBABILITY = 0.3  # личная заметка заканчивается вопросом к читателям

# Вывод для читателя в конце поста: своими словами, без рубрик-меток (канал — голос живого автора)
TAKEAWAY_RULE = (
    "Закончи пост выводом для читателя: одна-две фразы о том, что конкретно он может сделать у себя "
    "(в Telegram-канале, блоге, салоне, юрфирме, небольшой стройке или маленьком продакшене) с этим уроком. "
    "Формулируй каждый раз по-новому и своим голосом, как продолжение мысли, без рубрик и меток "
    "вроде «Что забрать себе:», «Вывод:», «Мораль:», «Итог:». Действие, а не мораль."
)
RUBRIC_RE = re.compile(r"(?mi)^[ \t]*(?:что забрать себе|продюсерский вывод|выводы?|мораль|итог|резюме)[ \t]*:[ \t]*(\w?)")
RUBRIC_FIX = "рубрика-метка в начале абзаца («Что забрать себе:», «Вывод:» и т.п.): перепиши этот вывод живой фразой без метки"

# Ссылка на бота в постах. Канал — витрина личности, поэтому по умолчанию ссылки нет.
BOT_CTA_ENABLED = os.getenv("CHANNEL_BOT_CTA", "").strip().lower() in ("1", "true", "yes")

# Ссылка на бота для личного разбора (если включена): каждый 4-й или 5-й пост
CTA_EVERY = 4
CTA_TEXTS = [
    "Разобрать ваш проект: {bot}",
    "Хотите такой же разбор своего канала или бизнеса? Пишите: {bot}",
    "Ваш проект на мой стол: {bot}",
]
RAZBOR_TITLE = "Разбор подписчика"
RAZBOR_CTA = "Ваш вопрос или проект на разбор: {bot}"
RAZBOR_INVITE = "Свой вопрос для следующего разбора оставьте в комментариях."


def needs_cta(history: list[dict], rng=random) -> bool:
    """Пора ли ставить ссылку на бота. Посты старого формата (без поля cta) начинают отсчёт заново."""
    since = 0  # постов без ссылки после последней
    for h in reversed(history):
        if "cta" not in h or h["cta"]:
            break
        since += 1
    if since < CTA_EVERY - 1:
        return False
    return since >= CTA_EVERY or rng.random() < 0.5


def cta_line(bot_handle: str, rng=random) -> str:
    return rng.choice(CTA_TEXTS).format(bot=bot_handle)


# Служебные хвосты каналов-источников, которым не место в цитате (проверяются только короткие строки)
QUOTE_JUNK = re.compile(
    r"запрещ[её]нн\w*\s+(?:в\s+(?:России|РФ)\s+)?(?:экстремистск\w*\s+)?(?:социальн\w*\s+сет\w*|соцсет\w*|организац\w*)"
    r"|\b(?:TG|Telegram|ТГ)\s*\|\s*(?:VK|ВК)\b"
    r"|^\s*(?:подписаться|подпишись|подписывайтесь|наш канал|читать полностью|читать далее)\b"
    r"|^\s*(?:@\w+|https?://\S+|(?:#\w+\s*)+)\s*$",
    re.IGNORECASE,
)


def clean_quote(text: str) -> str:
    kept = [ln for ln in (text or "").splitlines() if not (len(ln.strip()) <= 120 and QUOTE_JUNK.search(ln))]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def build_system_prompt(
    fmt: dict, tone: str, core_beliefs: list, rag_context: str, recent_openings: list[str], hook: bool = False,
    persona: str | None = None, day_block: str = "", recent_block: str = "",
) -> str:
    beliefs_text = "\n".join(f"- {b}" for b in core_beliefs) if core_beliefs else "- нет"
    openings = "\n".join(f"- «{o}»" for o in recent_openings) if recent_openings else "- (постов ещё не было)"
    lo, hi = fmt["length"]
    if fmt["story"] and rag_context:
        knowledge = (
            "[ПРОФЕССИОНАЛЬНАЯ БАЗА ЗНАНИЙ — МЕТОДОЛОГИЯ И ЧУЖИЕ ИСТОРИИ]\n"
            f"{rag_context}\n\n"
            "Правила: возьми из базы максимум ОДНУ историю, которая действительно подходит. "
            "Истории от первого лица в базе — это ЧУЖОЙ опыт: никогда не пересказывай их как свои. "
            "Подавай историю каждый раз по-разному (например: «коллега с одного канала рассказывал», "
            "«на одной летучке мне рассказали», «есть известный случай»), не повторяй одну и ту же вводную."
        )
    elif rag_context:
        knowledge = (
            "[ПРОФЕССИОНАЛЬНАЯ БАЗА ЗНАНИЙ — ТОЛЬКО ДЛЯ ФОНА]\n"
            f"{rag_context}\n\n"
            "В этом формате НЕ пересказывай истории из базы. Используй её только как свою профессиональную "
            "насмотренность: принципы, механику форматов, цифры."
        )
    else:
        knowledge = ""

    banned = ", ".join(f"«{p}»" for p in BANNED_PHRASES)
    hook_rule = (
        "12. Последней строкой задай подписчикам один острый вопрос — такой, "
        "на который хочется ответить в комментариях.\n"
        if hook else ""
    )
    # С файлом личности взгляды берутся из его КРЕДО; без файла — из «эволюции убеждений»
    views = "" if persona else (
        "ТВОИ ВЗГЛЯДЫ (фон для суждений — НЕ цитируй эти формулировки и не пересказывай их):\n" + beliefs_text
    )
    voice_rule = (
        "13. Пиши голосом из блока ГОЛОС. О себе — только то, что есть в блоке «ЧТО Я МОГУ РАССКАЗЫВАТЬ О СЕБЕ», "
        "и никогда то, что в блоке «ЧЕГО Я В КАНАЛЕ НЕ КАСАЮСЬ НИКОГДА».\n" if persona else ""
    )
    return f"""
{persona or Prompts.LORE}

Ты пишешь пост в свой авторский Telegram-канал — комментарий к новости.
ТВОИ ЧИТАТЕЛИ: {AUDIENCE}. Им интересно, как устроены медиа, и что из этого можно применить у себя.
Сегодня {today_ru()}. Все сроки и прогнозы считай от этой даты.

{day_block}

{recent_block}

ФОРМАТ ЭТОГО ПОСТА: «{fmt['name']}». {fmt['instruction']}
ДЛИНА: {lo}–{hi} символов без финального вывода для читателя. Это жёсткое требование: не длиннее.
ТОН ЭТОГО ПОСТА: {tone}. Если этот тон совсем не ложится на новость, выбери ближайший уместный.

{views}

{knowledge}

КАК ПИСАТЬ:
1. Это живой пост живого человека, а не колонка по шаблону. Каждый пост строится по-своему.
2. Начни сразу с мысли. Не начинай пост так же, как последние посты:
{openings}
3. Запрещённые обороты (они превратили прошлые посты в копии друг друга): {banned}.
4. Слово «дилетант» — максимум один раз, лучше ни разу.
5. Мат — максимум одно слово и только если оно бьёт точнее любого другого. Чаще обходись без него.
6. Не ссылайся на «статью», «новость из канала», «базу знаний». Ты не агрегатор: ты человек, у которого есть мнение.
7. О реальных людях — только то, что есть в новости или общеизвестно. Не выдумывай встреч, разговоров, совместной работы и цитат с ними: никаких «я имел с ним дело», «мы с ней снимали», «я его лично знал».
8. Никаких обращений к конкретному собеседнику и личной переписки. О своём дне и погоде — только если это разрешено в блоке «ТВОЙ ДЕНЬ», мимоходом.
9. Без заголовков, без списков, без markdown и звёздочек. Обычный текст, абзацы по смыслу.
10. Не пиши «Цитата», не повторяй текст новости — он будет показан над постом отдельно.
11. {TAKEAWAY_RULE}
{hook_rule}{voice_rule}"""


def build_user_prompt(news: dict) -> str:
    parts = [f"НОВОСТЬ: {news.get('content', '').strip()}"]
    if news.get("original_text"):
        parts.append(f"ОРИГИНАЛЬНЫЙ ТЕКСТ ПОСТА-ИСТОЧНИКА:\n{news['original_text'].strip()[:3000]}")
    if news.get("channel"):
        parts.append(f"ИСТОЧНИК: @{news['channel']}")
    return "\n\n".join(parts)


def _trim(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # обрезаем по границе предложения или слова
    for sep in (". ", "! ", "? ", "\n", " "):
        pos = cut.rfind(sep)
        if pos > limit * 0.6:
            cut = cut[: pos + 1]
            break
    return cut.rstrip() + " …"


def extract_url(text: str) -> str | None:
    m = URL_RE.search(text or "")
    return m.group(0).rstrip(").,") if m else None


def _body_html(text: str, cta: str | None) -> str:
    """Текст поста в HTML; приписка (ссылка на бота или приглашение в комментарии) курсивом в конце."""
    return html.escape(text.strip()) + (f"\n\n<i>{html.escape(cta)}</i>" if cta else "")


def render_post_html(comment: str, news: dict, cta: str | None = None) -> str:
    """Шапка со ссылкой и свёрнутой цитатой + сам комментарий (HTML для Telegram)."""
    url = news.get("url") or extract_url(news.get("content", ""))
    channel = news.get("channel")
    # Старый формат новости (до выбора по номеру): «суть + URL: ссылка» — чистим хвост
    quote_src = news.get("original_text") or re.sub(r"\bURL:\s*$", "", URL_RE.sub("", news.get("content", "")).strip()).strip()
    quote_src = clean_quote(quote_src)

    header = []
    if url:
        label = f"Новость · @{channel}" if channel else "Новость"
        header.append(f'🔗 <a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>')
    body = _body_html(comment, cta)

    quote_limit = QUOTE_MAX_CHARS
    while True:
        quote = html.escape(_trim(quote_src, quote_limit)) if quote_src else ""
        # Свёрнутая цитата: в ленте видно начало, остальное раскрывается по тапу
        block = header + ([f"<blockquote expandable>{quote}</blockquote>"] if quote else [])
        result = "\n".join(block) + ("\n\n" if block else "") + body
        if len(result) <= TELEGRAM_LIMIT or quote_limit <= 100:
            return result[:TELEGRAM_LIMIT]
        quote_limit -= 150


def render_razbor_html(question: str, answer: str, cta: str | None = None) -> str:
    """Рубрика «Разбор подписчика»: заголовок, вопрос цитатой (длинный — свёрнутой), ответ."""
    q = html.escape(_trim(question, 1200))
    quote_tag = "blockquote expandable" if len(q) > 400 else "blockquote"
    head = f"<b>{RAZBOR_TITLE}</b>\n<{quote_tag}>{q}</blockquote>\n\n"
    return (head + _body_html(answer, cta))[:TELEGRAM_LIMIT]


_YEAR = re.compile(r"\b(20\d{2})\b")


def past_years(text: str, current_year: int) -> list[int]:
    return sorted({int(y) for y in _YEAR.findall(text) if int(y) < current_year})


def _ensure_generated(comment: str) -> None:
    """generate_standalone возвращает служебные строки вместо исключений — не публикуем их."""
    if not comment or comment.startswith(("⛔️", "⚠️")):
        raise ValueError(f"Генерация не удалась: {comment[:200]!r}")


def _opening(text: str) -> str:
    first = re.split(r"(?<=[.!?…])\s", text.strip(), maxsplit=1)[0]
    return first[:120]


def _drop_rubrics(text: str) -> str:
    """Запасной путь без LLM: убрать метку вроде «Вывод:» и начать фразу с заглавной."""
    return RUBRIC_RE.sub(lambda m: m.group(1).upper(), text)


async def _polish(llm, system_prompt: str, user_prompt: str, text: str, extra_problems: list[str] | None = None) -> str:
    """
    Проверки после генерации: клише, выдуманные знакомства с реальными людьми, рубрики-метки вроде «Вывод:».
    Если что-то не так — один проход переписывания. Выдуманное знакомство, пережившее переписывание,
    останавливает публикацию; оставшаяся метка вырезается.
    """
    problems = find_cliches(text) + list(extra_problems or [])
    if find_contact_claims(text):
        problems.append(CONTACT_FIX)
    if RUBRIC_RE.search(text):
        problems.append(RUBRIC_FIX)
    if problems:
        logger.info(f"♻️ [Post] Переписываем: {problems}")
        rewrite_prompt = (
            user_prompt
            + "\n\nТВОЙ ЧЕРНОВИК:\n" + text
            + "\n\nПерепиши черновик, сохранив мысль, формат и длину, но исправь: "
            + "; ".join(problems) + "."
        )
        rewritten = (await llm.generate_standalone(system_prompt, rewrite_prompt)).replace("*", "").strip()
        if rewritten and not rewritten.startswith(("⛔️", "⚠️")):
            text = rewritten
        left = find_cliches(text)
        if left:
            logger.warning(f"⚠️ [Post] После переписывания остались клише: {left}")
    claims = find_contact_claims(text)
    if claims:
        # Выдуманное знакомство с реальным человеком не публикуем: лучше пропустить слот
        raise ValueError(f"После переписывания осталось выдуманное знакомство: {claims}")
    return humanize_punctuation(_drop_rubrics(text))


async def generate_post(
    llm, rag_service, core_beliefs: list, news: dict, history: list[dict], force_format: str | None = None,
    cta_handle: str | None = None,
) -> tuple[str, dict]:
    """
    Генерирует пост. Возвращает (html для отправки, метаданные для истории).
    history — последние посты: [{"format": id, "opening": "...", "cta": bool}].
    cta_handle — @username бота; если передан (CHANNEL_BOT_CTA), раз в 4–5 постов в конце ставится ссылка на него.
    """
    forced = [f for f in POST_FORMATS if f["id"] == force_format]
    fmt = forced[0] if forced else choose_format([h.get("format", "") for h in history])
    last_tone = history[-1].get("tone") if history else None
    query = news.get("content", "") or news.get("original_text", "")
    rag_ctx = await rag_service.search(query, top_k=3)
    recent_openings = [h["opening"] for h in history[-6:] if h.get("opening")]

    # Живой колумнист: личность из приватного файла, настроение и сцена дня, память о своих постах
    persona, day, day_block, recent_block = columnist.load_persona(), None, "", ""
    if persona:
        now = datetime.now(columnist.MSK)
        day = columnist.get_day(now)
        tone = columnist.pick_tone(day, last_tone)
        day_block = columnist.describe_day(day, columnist.slot_of(now), random.random() < DAY_MENTION_PROBABILITY)
        recent_block = columnist.recent_posts_block(history, random.random() < CALLBACK_PROBABILITY)
    else:
        tone = choose_tone(last_tone)

    hook = fmt["id"] != "question" and random.random() < HOOK_PROBABILITY
    system_prompt = build_system_prompt(fmt, tone, core_beliefs, rag_ctx, recent_openings, hook,
                                        persona=persona, day_block=day_block, recent_block=recent_block)
    user_prompt = build_user_prompt(news)
    logger.info(f"✍️ [Post] Формат: {fmt['name']}, тон: {tone}, настроение: {day['mood'] if day else '-'}, "
                f"база знаний: {'да' if rag_ctx else 'нет'}")

    comment = (await llm.generate_standalone(system_prompt, user_prompt)).replace("*", "").strip()
    _ensure_generated(comment)
    extra = []
    if fmt["id"] == "bet":
        years = past_years(comment, datetime.now(pytz.timezone("Europe/Moscow")).year)
        if years:
            extra.append(f"сроки в прошлом ({', '.join(map(str, years))}): сегодня {today_ru()}, прогноз должен быть в будущем")
    comment = await _polish(llm, system_prompt, user_prompt, comment, extra)

    cta = bool(cta_handle) and needs_cta(history)
    meta = {
        "format": fmt["id"], "tone": tone, "mood": day["mood"] if day else None, "hook": hook, "cta": cta,
        "opening": _opening(comment), "chars": len(comment), "text": comment,
    }
    return render_post_html(comment, news, cta_line(cta_handle) if cta else None), meta


async def generate_personal(llm, history: list[dict]) -> tuple[str, dict]:
    """«Личное»: короткая заметка о своём дне или неделе, без новости. Только при файле личности."""
    persona = columnist.load_persona()
    if not persona:
        raise ValueError("Нет файла личности колумниста: личные заметки не пишем")
    now = datetime.now(columnist.MSK)
    day = columnist.get_day(now)
    tone = columnist.pick_tone(day, history[-1].get("tone") if history else None)
    told = "\n".join(f"- {e['date']}: {e['text'][:300]}" for e in columnist.journal_recent(4)) \
        or "- (личных заметок ещё не было)"
    ending = ("Закончи коротким вопросом к читателям." if random.random() < PERSONAL_HOOK_PROBABILITY
              else "Без вопроса к читателям в конце.")
    banned = ", ".join(f"«{p}»" for p in BANNED_PHRASES)
    system_prompt = f"""
{persona}

Ты пишешь в свой Telegram-канал короткую личную заметку: не про новость, а про себя — момент из своего дня
или недели и мысль, которая из него выросла. Так живые авторы иногда пишут вне рубрик.
Сегодня {today_ru()}.

{columnist.describe_day(day, columnist.slot_of(now), True)}

[ЧТО ТЫ УЖЕ РАССКАЗЫВАЛ О СЕБЕ В КАНАЛЕ — не повторяйся и не противоречь]
{told}

{columnist.recent_posts_block(history, False)}

КАК ПИСАТЬ:
1. 350–800 символов. Один момент, одна мысль. Без морали в конце и без рубрик-меток.
   Не начинай с кофе и погоды: войди в заметку через сам момент или мысль.
2. Тема одна, на выбор: работа над чужим проектом в общих чертах, книга или фильм, которые перечитываешь
   или пересматриваешь, солдатики и диорама, город и погода, Петербург, профессия и время, возраст
   и самоирония, путешествия. Бери только то, что есть в «ЧТО Я МОГУ РАССКАЗЫВАТЬ О СЕБЕ», и мелкие бытовые детали дня.
3. Не касайся того, что в блоке «ЧЕГО Я В КАНАЛЕ НЕ КАСАЮСЬ НИКОГДА». Не выдумывай встреч, разговоров
   и совместной работы с реальными людьми.
4. Тон: {tone}. Настроение дня: {day['mood']}.
5. Голос из блока ГОЛОС. Без заголовков, списков, markdown и звёздочек.
6. {ending}
7. Запрещённые обороты: {banned}.
"""
    user_prompt = "Напиши личную заметку для канала."
    text = (await llm.generate_standalone(system_prompt, user_prompt)).replace("*", "").strip()
    _ensure_generated(text)
    text = await _polish(llm, system_prompt, user_prompt, text)
    meta = {"format": "personal", "tone": tone, "mood": day["mood"], "hook": False, "cta": False,
            "opening": _opening(text), "chars": len(text), "text": text}
    return html.escape(text), meta


async def generate_razbor(
    llm, rag_service, core_beliefs: list, question: str, cta_handle: str | None = None
) -> tuple[str, dict]:
    """Рубрика «Разбор подписчика»: публичный ответ на вопрос читателя. Возвращает (html, метаданные)."""
    rag_ctx = await rag_service.search(question, top_k=3)
    beliefs_text = "\n".join(f"- {b}" for b in core_beliefs) if core_beliefs else "- нет"
    knowledge = (
        "[ПРОФЕССИОНАЛЬНАЯ БАЗА ЗНАНИЙ — ТОЛЬКО ДЛЯ ФОНА]\n"
        f"{rag_ctx}\n\n"
        "Не пересказывай истории из базы. Используй её как свою насмотренность: принципы, механику форматов, цифры."
    ) if rag_ctx else ""
    banned = ", ".join(f"«{p}»" for p in BANNED_PHRASES)
    persona = columnist.load_persona()
    views = "" if persona else f"ТВОИ ВЗГЛЯДЫ (фон для суждений — НЕ цитируй эти формулировки):\n{beliefs_text}"
    system_prompt = f"""
{persona or Prompts.LORE}

Ты ведёшь в своём Telegram-канале рубрику «{RAZBOR_TITLE}»: публично отвечаешь на вопрос читателя.
ТВОИ ЧИТАТЕЛИ: {AUDIENCE}.
Сегодня {today_ru()}.

{views}

{knowledge}

КАК ОТВЕЧАТЬ:
1. К автору вопроса — на «вы». Ирония и прямота уместны, унижение нет.
2. По-продюсерски: короткий диагноз, затем 2–4 конкретных шага с номерами «1)», «2)», «3)». Без заголовков, markdown и звёздочек.
3. Только практическое: что сделать, в каком порядке и как понять, что сработало. Примеры из жизни малого бизнеса и авторских каналов.
4. ДЛИНА: 1200–2200 символов.
5. {TAKEAWAY_RULE} Это вывод для всех читателей канала, не только для автора вопроса.
6. Запрещённые обороты: {banned}. Мат — максимум одно слово, лучше без него.
7. О реальных людях — только общеизвестное. Никаких «я с ним работал», «мы с ней снимали», «я его лично знал».
8. Не пересказывай вопрос: он будет показан над ответом.
"""
    user_prompt = f"ВОПРОС ПОДПИСЧИКА:\n{question.strip()}"
    answer = (await llm.generate_standalone(system_prompt, user_prompt)).replace("*", "").strip()
    _ensure_generated(answer)
    answer = await _polish(llm, system_prompt, user_prompt, answer)

    # Без ссылки на бота рубрика зовёт присылать вопросы в комментарии канала
    tail = RAZBOR_CTA.format(bot=cta_handle) if cta_handle else RAZBOR_INVITE
    meta = {"format": "razbor", "cta": bool(cta_handle), "chars": len(answer), "text": answer, "question": question}
    return render_razbor_html(question, answer, tail), meta


def append_history(history: list[dict], meta: dict) -> list[dict]:
    return (history + [meta])[-HISTORY_SIZE:]


# ─────────────────────────── «Добивка» ───────────────────────────
# Через некоторое время после публикации Аристарх иногда отвечает на собственный пост
# коротким продолжением: так делают живые авторы каналов.

FOLLOWUP_PROBABILITY = 0.35
# (минимум, максимум задержки в минутах, вес)
FOLLOWUP_DELAYS = ((20, 90, 0.3), (120, 480, 0.45), (960, 1560, 0.25))
FOLLOWUP_KINDS = [
    "панчлайн: фраза, которая пришла тебе в голову уже после публикации",
    "самоирония: поймай себя на перегибе в собственном посте и признай это хлёстко",
    "мысль вдогонку: неожиданная деталь или поворот, который ты забыл сказать",
    "упрямство: ты перечитал пост и настаиваешь ещё жёстче",
]


def schedule_followup(published_at: datetime, rng: random.Random | None = None):
    """Решает, будет ли добивка, и когда. Возвращает datetime или None."""
    from datetime import timedelta

    rng = rng or random
    if rng.random() >= FOLLOWUP_PROBABILITY:
        return None
    lo, hi, _ = rng.choices(FOLLOWUP_DELAYS, weights=[d[2] for d in FOLLOWUP_DELAYS], k=1)[0]
    return published_at + timedelta(minutes=rng.randint(lo, hi))


def _ago_ru(minutes: int) -> str:
    if minutes < 90:
        return f"{minutes} минут"
    if minutes < 24 * 60:
        return f"{round(minutes / 60)} ч"
    return "день"


async def generate_followup(llm, post: dict, minutes_since: int) -> str:
    """Короткая добивка (1–2 предложения) к собственному посту."""
    kind = random.choice(FOLLOWUP_KINDS)
    banned = ", ".join(f"«{p}»" for p in BANNED_PHRASES)
    persona = columnist.load_persona()
    mood_line = f"Настроение сейчас: {columnist.get_day()['mood']}.\n" if persona else ""
    system_prompt = f"""
{persona or Prompts.LORE}

{mood_line}Сегодня {today_ru()}. {_ago_ru(minutes_since)} назад ты опубликовал в своём Telegram-канале пост.
Сейчас ты пишешь к нему «добивку» — короткий ответ на собственный пост, как делают живые авторы каналов.

ТИП ДОБИВКИ: {kind}.
ФОРМАТ: одно-два коротких предложения, до 250 символов. Без приветствий, без «UPD», без пересказа поста.
Запрещённые обороты: {banned}.
Без markdown и звёздочек. Мат — только если он точнее любого другого слова.
"""
    user_prompt = f"ТВОЙ ПОСТ:\n{post.get('text', '').strip()}"
    text = (await llm.generate_standalone(system_prompt, user_prompt)).replace("*", "").strip()
    _ensure_generated(text)
    if find_cliches(text) or find_contact_claims(text):
        text = (await llm.generate_standalone(
            system_prompt,
            user_prompt + "\n\nЧЕРНОВИК ДОБИВКИ:\n" + text
            + "\n\nПерепиши без запрещённых оборотов и без выдуманного личного знакомства с реальными людьми.",
        )).replace("*", "").strip()
        _ensure_generated(text)
        if find_contact_claims(text):
            raise ValueError("В добивке осталось выдуманное знакомство с реальным человеком")
    return humanize_punctuation(text)
