"""
Куратор: Аристарх сам листает ленту и решает, о чём писать.

Раньше новость выбирал безличный редактор (Flash-Lite), а Ари только комментировал выбранное.
Теперь так:
1. Код готовит короткий список (tg_news.build_shortlist): жёсткие фильтры (запретные темы, реклама,
   уже прокомментированное, потолок ИИ), без дублей, разные темы и каналы, до 10 новостей.
2. Ари со своей личностью оценивает каждую: насколько хочется написать об этом именно сейчас (0-10),
   что именно зацепило (его угол для поста) или почему мимо, каким форматом ответил бы.
   Он видит время суток и день недели, фон дня (манеру настроения), свои последние посты,
   темы, которые уже надоели (усталость с затуханием) и которых давно не было (любопытство).
3. Код берёт самую цепляющую новость не ниже порога CURATOR_MIN_INTEREST. Если ничего не цепляет,
   поста нет: это пишется в журнал куратора как «не зацепило», без планок и замен.
4. Иногда Ари возвращается к вчерашней истории («додумал за ночь»): она идёт отдельным кандидатом,
   если вчера был пост по новости и возврата не было три дня.

Журнал решений: curator_log.json в корне (вне git). Из него же считается усталость от тем.
"""

import json
import math
import os
import random
import re
from datetime import datetime, timedelta, timezone

from aristarkh_core.config import logger
from news_module import columnist
from news_module.state_io import read_json, update_json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(ROOT, "curator_log.json")
LOG_KEEP = 300


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


CURATOR_ON = os.getenv("NEWS_CURATOR", "").strip().lower() in ("1", "on", "true", "yes")
MIN_INTEREST = _env_float("CURATOR_MIN_INTEREST", 6)
FATIGUE_HALF_LIFE_DAYS = 3.0   # «надоело» затухает: пост трёхдневной давности весит вдвое меньше
TIRED_AT = 1.2                 # с какой суммарной усталостью тема ощущается как «уже воротит»
FRESH_AFTER_DAYS = 7           # тема, которой не было неделю, ощущается как давно не тронутая
RETURN_COOLDOWN_DAYS = 3       # вернуться к вчерашней истории можно не чаще раза в три дня

# Темы: id -> (как он их называет, ключевые слова). Нужны для усталости и разнообразия, а не для вкуса.
TOPICS = {
    "tv": ("телевидение", r"\bтв\b|телеканал|телевиз|телешоу|ток-шоу|\bэфир"),
    "screen": ("кино, сериалы и стриминги", r"стриминг|кинопоиск|\bokko\b|\bivi\b|netflix|сериал|фильм|\bкино|премьер|кассов\w* сбор|прокат"),
    "showbiz": ("звёзды и шоу-бизнес", r"певиц|певец|актёр|актер|актрис|звезд[аы]|рэпер|концерт|альбом|шоу-бизнес|музыкант|продюсер\w* артист"),
    "fashion": ("мода и люкс", r"\bмод[аеуы]\b|показ\w* мод|коллекци|люкс|ювелир|\bчасы\b|vogue|chanel|gucci|dior|prada|подиум"),
    "ads": ("реклама и бренды", r"реклам\w* кампани|рекламн\w* ролик|агентств|креатив|билборд|наружн|слоган|ребрендинг|логотип|tbwa|saatchi"),
    "platforms": ("платформы и маркетинг", r"маркетплейс|ozon|wildberries|авито|яндекс|telegram|телеграм|\bvk\b|вконтакт|охват|таргет|трафик|метрик|алгоритм"),
    "ai": ("нейросети и ИИ", r"\bии\b|нейросет|искусственн\w* интеллект|chatgpt|openai|gemini|ии-"),
    "creators": ("блогеры и инфлюенсеры", r"блогер|инфлюенс|тикток|tiktok|ютуб|youtube|подписчик|reels|стример"),
    "media_biz": ("медиабизнес и деньги", r"\bipo\b|выручк|инвест|сделк|холдинг|издател|\bсми\b|журнал|газет|медиагрупп"),
    "culture": ("культура и искусство", r"музе|выставк|театр|художник|галере|искусств|книг|литератур|ярмарк\w* искусств"),
    "food_city": ("еда, рестораны и город", r"ресторан|\bкафе\b|кофейн|фастфуд|макдон|mcdonald|бургер|\bеда\b|меню|subway"),
}
_TOPIC_RE = {k: re.compile(v[1], re.IGNORECASE) for k, v in TOPICS.items()}


def topic_of(text: str) -> str:
    scores = {k: len(rx.findall(text or "")) for k, rx in _TOPIC_RE.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] else "other"


def topic_label(topic: str) -> str:
    return TOPICS.get(topic, ("разное",))[0]


# ── журнал и усталость ──

def load_log() -> list[dict]:
    data = read_json(LOG_PATH)
    return data.get("entries", []) if isinstance(data, dict) else []


def log_decision(entry: dict) -> None:
    def mutate(data: dict):
        data["entries"] = (data.get("entries", []) + [entry])[-LOG_KEEP:]
    update_json(LOG_PATH, mutate)


def _posted_topics(log: list[dict], history: list[dict]) -> list[tuple[str, datetime]]:
    """(тема, когда) для всех постов: из журнала куратора, а для старых постов - по тексту из истории."""
    items = [(e["topic"], datetime.fromisoformat(e["at"])) for e in log if e.get("decision") == "post" and e.get("topic")]
    logged = {e.get("message_id") for e in log if e.get("message_id")}
    for h in history:
        if h.get("format") == "personal" or not h.get("published_at") or h.get("message_id") in logged:
            continue
        items.append((h.get("topic") or topic_of(h.get("text", "")), datetime.fromisoformat(h["published_at"])))
    return items


def fatigue(log: list[dict], history: list[dict], now: datetime) -> dict[str, float]:
    out: dict[str, float] = {}
    for topic, at in _posted_topics(log, history):
        age = max(0.0, (now - at).total_seconds() / 86400)
        out[topic] = out.get(topic, 0.0) + math.pow(0.5, age / FATIGUE_HALF_LIFE_DAYS)
    return out


def state_block(log: list[dict], history: list[dict], now: datetime) -> str:
    fat = fatigue(log, history, now)
    last_seen: dict[str, datetime] = {}
    for topic, at in _posted_topics(log, history):
        last_seen[topic] = max(at, last_seen.get(topic, at))
    tired = [topic_label(t) for t, v in sorted(fat.items(), key=lambda x: -x[1]) if v >= TIRED_AT and t != "other"]
    fresh = [topic_label(t) for t in TOPICS if t not in last_seen or (now - last_seen[t]).days >= FRESH_AFTER_DAYS]
    lines = ["[ЧТО У ТЕБЯ СЕЙЧАС НА ДУШЕ ПО ТЕМАМ]"]
    if tired:
        lines.append(f"Уже надоело, писал об этом много за последние дни: {', '.join(tired)}. Чтобы снова взяться, новость должна быть по-настоящему сильной.")
    if fresh:
        lines.append(f"Давно не трогал, тут любопытнее: {', '.join(fresh)}.")
    return "\n".join(lines) if len(lines) > 1 else ""


# ── возврат к вчерашней истории ──

def yesterday_story(history: list[dict], log: list[dict], digests: list[dict], now: datetime) -> dict | None:
    """Вчерашний пост по новости, к которому можно вернуться, или None."""
    last_return = max((datetime.fromisoformat(e["at"]) for e in log if e.get("return")), default=None)
    if last_return and now - last_return < timedelta(days=RETURN_COOLDOWN_DAYS):
        return None
    yesterday = (now.astimezone(columnist.MSK) - timedelta(days=1)).date()
    for h in reversed(history):
        if h.get("format") == "personal" or not h.get("url") or not h.get("published_at"):
            continue
        if datetime.fromisoformat(h["published_at"]).astimezone(columnist.MSK).date() != yesterday:
            continue
        digest = next((d for d in reversed(digests) if d.get("url") == h["url"]), None)
        if digest:
            return {"url": h["url"], "channel": digest.get("channel"), "text": digest.get("original_text") or "",
                    "content": digest.get("content", ""), "yesterday_post": h.get("text", ""), "is_return": True}
    return None


# ── оценка ленты ──

FORMAT_IDS = "sting, observation, producer_fix, twist, against, doubt, bet, numbers, memory, story"


def _candidates_block(candidates: list[dict]) -> str:
    out = []
    for i, c in enumerate(candidates, 1):
        if c.get("is_return"):
            out.append(f"#{i} ВЧЕРАШНЯЯ ИСТОРИЯ, о которой ты уже писал. Твой вчерашний пост начинался так: "
                       f"«{c['yesterday_post'][:300]}». Вернуться стоит, только если она правда не отпускает "
                       f"и ты додумал что-то новое. Сама новость:\n{c['text'][:700]}")
        else:
            out.append(f"#{i} [@{c.get('channel')}, {str(c.get('datetime', ''))[:10]}, тема: {topic_label(c.get('topic', 'other'))}]\n"
                       f"{c['text'][:900]}")
    return "\n\n".join(out)


def _slot_line(now: datetime) -> str:
    msk = now.astimezone(columnist.MSK)
    part = "утро" if msk.hour < 15 else "вечер"
    return f"Сейчас {columnist.WEEKDAYS[msk.weekday()]}, {part}, около {msk:%H:%M}."


def _parse(text: str) -> list[dict]:
    m = re.search(r"\[.*\]", text or "", re.DOTALL)
    if not m:
        raise ValueError(f"Куратор вернул не JSON: {text[:200]!r}")
    return json.loads(m.group(0))


async def rate(llm, candidates: list[dict], history: list[dict], now: datetime | None = None) -> list[dict]:
    """Ари оценивает каждую новость: [{"n", "interest", "hook", "why_not", "format"}] в порядке кандидатов."""
    now = now or datetime.now(timezone.utc)
    persona = columnist.load_persona() or ""
    day = columnist.get_day(now.astimezone(columnist.MSK))
    mood = columnist.MOOD_MANNER.get(day.get("mood"), "")
    system_prompt = f"""
{persona}

Ты листаешь ленту телеграм-каналов, как каждый день, и решаешь, о чём тебе хочется написать в свой канал.
{_slot_line(now)} Фон дня: {mood}.

{columnist.recent_posts_block(history, False)}

{state_block(load_log(), history, now)}

КАК РЕШАТЬ:
1. Ты живой человек со своим вкусом, а не редактор, который обязан что-то выбрать. Большинство новостей
   тебе неинтересны, и это нормально. Оценивай честно: interest от 0 до 10, насколько тебе хочется
   написать об этом именно сейчас, с твоим сегодняшним фоном и в это время суток.
   7 и выше - цепляет. Ниже 6 - мимо.
2. Цепляет обычно конкретная деталь: странное решение, чьи-то деньги, фальшь или, наоборот, хорошая работа,
   человек, поступок. Тема сама по себе не цепляет. Рекламу, анонсы, советы и тексты без события - мимо.
3. Не зацикливайся: то, что уже надоело, должно быть по-настоящему сильным, а давно не тронутое
   тебе сейчас любопытнее.
4. hook: если цепляет, одной фразой от первого лица, что именно тебя зацепило: конкретная деталь, а не тема.
   Без названия эмоции. why_not: если мимо, одной фразой почему, своими словами.
5. format: каким форматом тебе хочется ответить, один из: {FORMAT_IDS}. Если не цепляет, null.

Верни только JSON-массив, по объекту на каждую новость, в том же порядке:
[{{"n": 1, "interest": 0, "hook": "", "why_not": "", "format": null}}]
"""
    user_prompt = "ЛЕНТА:\n\n" + _candidates_block(candidates)
    raw = await llm.generate_standalone(system_prompt, user_prompt)
    ratings = _parse(raw)
    by_n = {int(r.get("n", 0)): r for r in ratings if isinstance(r, dict)}
    out = []
    for i, c in enumerate(candidates, 1):
        r = by_n.get(i, {})
        try:
            interest = float(r.get("interest", 0))
        except (TypeError, ValueError):
            interest = 0.0
        out.append({"n": i, "interest": interest, "hook": (r.get("hook") or "").strip(),
                    "why_not": (r.get("why_not") or "").strip(), "format": r.get("format")})
    return out


def choose(candidates: list[dict], ratings: list[dict], rng=random) -> tuple[dict, dict] | None:
    """Самая цепляющая новость не ниже порога (при равенстве случайная) или None: ничего не зацепило."""
    ok = [(c, r) for c, r in zip(candidates, ratings) if r["interest"] >= MIN_INTEREST]
    if not ok:
        return None
    top = max(r["interest"] for _, r in ok)
    return rng.choice([(c, r) for c, r in ok if r["interest"] == top])


def decision_entry(now: datetime, candidates: list[dict], ratings: list[dict], chosen: tuple | None,
                   message_id: int | None = None, test: str | None = None) -> dict:
    entry = {
        "at": now.isoformat(), "decision": "post" if chosen else "skip",
        "feed": [{"url": c.get("url"), "topic": c.get("topic"), "interest": r["interest"],
                  "hook": r["hook"], "why_not": r["why_not"]} for c, r in zip(candidates, ratings)],
    }
    if chosen:
        c, r = chosen
        entry.update(url=c.get("url"), topic=c.get("topic") or topic_of(c.get("text", "")), hook=r["hook"],
                     format=r.get("format"), interest=r["interest"], message_id=message_id, **({"return": True} if c.get("is_return") else {}))
    if test:
        entry["test"] = test
    return entry
