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
- в начале поста — ссылка на новость и моноширинная цитата оригинала;
- длинные тире заменяются на человеческие «-», «=», «:» (детерминированно, без LLM);
- в «Ставке» прогноз не может уйти в прошлое: годы раньше текущего → переписывание;
- «добивка»: через время Аристарх отвечает на собственный пост короткой фразой.
"""

import html
import random
import re
from datetime import datetime

import pytz

from aristarkh_core.config import logger
from aristarkh_core.humanize import humanize_punctuation
from aristarkh_core.prompts import Prompts

TELEGRAM_LIMIT = 4096
QUOTE_MAX_CHARS = 700
HISTORY_SIZE = 10

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
        "instruction": "Как эта новость отозвалась лично в тебе: ассоциация, настроение, воспоминание о девяностых или нулевых в общих чертах — без имён реальных людей и без выдуманных встреч с ними.",
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
]
MAX_DILETANT = 1  # слово-маркер допустимо не больше одного раза за пост

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


HOOK_PROBABILITY = 0.25  # доля постов (кроме «Вопроса залу»), которые заканчиваются вопросом к подписчикам


def build_system_prompt(
    fmt: dict, tone: str, core_beliefs: list, rag_context: str, recent_openings: list[str], hook: bool = False
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
        "11. Закончи пост одним острым вопросом к подписчикам — таким, на который хочется ответить в комментариях.\n"
        if hook else ""
    )
    return f"""
{Prompts.LORE}

Ты пишешь пост в свой авторский Telegram-канал DeusExMedia — комментарий к новости.
Сегодня {today_ru()}. Все сроки и прогнозы считай от этой даты.

ФОРМАТ ЭТОГО ПОСТА: «{fmt['name']}». {fmt['instruction']}
ДЛИНА: {lo}–{hi} символов. Это жёсткое требование: не длиннее.
ТОН ЭТОГО ПОСТА: {tone}. Если этот тон совсем не ложится на новость, выбери ближайший уместный.

ТВОИ ВЗГЛЯДЫ (фон для суждений — НЕ цитируй эти формулировки и не пересказывай их):
{beliefs_text}

{knowledge}

КАК ПИСАТЬ:
1. Это живой пост живого человека, а не колонка по шаблону. Каждый пост строится по-своему.
2. Начни сразу с мысли. Не начинай пост так же, как последние посты:
{openings}
3. Запрещённые обороты (они превратили прошлые посты в копии друг друга): {banned}.
4. Слово «дилетант» — максимум один раз, лучше ни разу.
5. Мат — максимум одно слово и только если оно бьёт точнее любого другого. Чаще обходись без него.
6. Не ссылайся на «статью», «новость из канала», «базу знаний». Ты не агрегатор: ты человек, у которого есть мнение.
7. О реальных людях — только то, что есть в новости или общеизвестно. Не выдумывай встреч, разговоров и цитат с ними.
8. Никаких обращений к конкретному собеседнику, никакой личной переписки, никакого «где я сейчас нахожусь».
9. Без заголовков, без списков, без markdown и звёздочек. Обычный текст, абзацы по смыслу.
10. Не пиши «Цитата», не повторяй текст новости — он будет показан над постом отдельно.
{hook_rule}"""


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


def render_post_html(comment: str, news: dict) -> str:
    """Шапка со ссылкой и моноширинной цитатой + сам комментарий (HTML для Telegram)."""
    url = news.get("url") or extract_url(news.get("content", ""))
    channel = news.get("channel")
    # Старый формат новости (до выбора по номеру): «суть + URL: ссылка» — чистим хвост
    quote_src = news.get("original_text") or re.sub(r"\bURL:\s*$", "", URL_RE.sub("", news.get("content", "")).strip()).strip()

    header = []
    if url:
        label = f"Новость · @{channel}" if channel else "Новость"
        header.append(f'🔗 <a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>')
    body = html.escape(comment.strip())

    quote_limit = QUOTE_MAX_CHARS
    while True:
        quote = html.escape(_trim(quote_src, quote_limit)) if quote_src else ""
        block = header + ([f"Цитата:\n<pre>{quote}</pre>"] if quote else [])
        result = "\n".join(block) + ("\n\n" if block else "") + body
        if len(result) <= TELEGRAM_LIMIT or quote_limit <= 100:
            return result[:TELEGRAM_LIMIT]
        quote_limit -= 150


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


async def generate_post(
    llm, rag_service, core_beliefs: list, news: dict, history: list[dict], force_format: str | None = None
) -> tuple[str, dict]:
    """
    Генерирует пост. Возвращает (html для отправки, метаданные для истории).
    history — последние посты: [{"format": id, "opening": "..."}].
    """
    forced = [f for f in POST_FORMATS if f["id"] == force_format]
    fmt = forced[0] if forced else choose_format([h.get("format", "") for h in history])
    tone = choose_tone(history[-1].get("tone") if history else None)
    query = news.get("content", "") or news.get("original_text", "")
    rag_ctx = await rag_service.search(query, top_k=3)
    recent_openings = [h["opening"] for h in history[-6:] if h.get("opening")]

    hook = fmt["id"] != "question" and random.random() < HOOK_PROBABILITY
    system_prompt = build_system_prompt(fmt, tone, core_beliefs, rag_ctx, recent_openings, hook)
    user_prompt = build_user_prompt(news)
    logger.info(f"✍️ [Post] Формат: {fmt['name']}, тон: {tone}, база знаний: {'да' if rag_ctx else 'нет'}")

    comment = (await llm.generate_standalone(system_prompt, user_prompt)).replace("*", "").strip()
    _ensure_generated(comment)
    problems = find_cliches(comment)
    if fmt["id"] == "bet":
        years = past_years(comment, datetime.now(pytz.timezone("Europe/Moscow")).year)
        if years:
            problems.append(f"сроки в прошлом ({', '.join(map(str, years))}): сегодня {today_ru()}, прогноз должен быть в будущем")
    cliches = problems
    if cliches:
        logger.info(f"♻️ [Post] Клише {cliches} — переписываем")
        rewrite_prompt = (
            user_prompt
            + "\n\nТВОЙ ЧЕРНОВИК:\n" + comment
            + "\n\nПерепиши черновик, сохранив мысль, формат и длину, но исправь: "
            + ", ".join(cliches) + "."
        )
        rewritten = (await llm.generate_standalone(system_prompt, rewrite_prompt)).replace("*", "").strip()
        if rewritten and not rewritten.startswith(("⛔️", "⚠️")):
            comment = rewritten
        left = find_cliches(comment)
        if left:
            logger.warning(f"⚠️ [Post] После переписывания остались клише: {left}")

    comment = humanize_punctuation(comment)
    meta = {
        "format": fmt["id"], "tone": tone, "hook": hook,
        "opening": _opening(comment), "chars": len(comment), "text": comment,
    }
    return render_post_html(comment, news), meta


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
    system_prompt = f"""
{Prompts.LORE}

Сегодня {today_ru()}. {_ago_ru(minutes_since)} назад ты опубликовал в своём Telegram-канале DeusExMedia пост.
Сейчас ты пишешь к нему «добивку» — короткий ответ на собственный пост, как делают живые авторы каналов.

ТИП ДОБИВКИ: {kind}.
ФОРМАТ: одно-два коротких предложения, до 250 символов. Без приветствий, без «UPD», без пересказа поста.
Запрещённые обороты: {banned}.
Без markdown и звёздочек. Мат — только если он точнее любого другого слова.
"""
    user_prompt = f"ТВОЙ ПОСТ:\n{post.get('text', '').strip()}"
    text = (await llm.generate_standalone(system_prompt, user_prompt)).replace("*", "").strip()
    _ensure_generated(text)
    if find_cliches(text):
        text = (await llm.generate_standalone(
            system_prompt, user_prompt + "\n\nЧЕРНОВИК ДОБИВКИ:\n" + text + "\n\nПерепиши без запрещённых оборотов."
        )).replace("*", "").strip()
        _ensure_generated(text)
    return humanize_punctuation(text)
