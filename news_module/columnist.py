"""
«Живой колумнист»: личность автора канала и его день.

- Личность читается из persona_private/columnist_ru.md (приватный файл, не в git).
  Если файла нет, посты пишутся со старым коротким ядром Prompts.LORE.
- «День Аристарха» создаётся раз в сутки по московскому времени и хранится в columnist_day.json:
  настроение (темперамент, инерция вчерашнего дня, день недели, погода, даты календаря),
  сцена утра и вечера по его распорядку, реальная погода в Москве (Open-Meteo, без ключа).
- Журнал личных заметок (columnist_journal.json): что он уже рассказывал о себе в канале.
"""

import json
import os
import random
from datetime import date, datetime, timedelta

import pytz
import requests

from aristarkh_core.config import logger

MSK = pytz.timezone("Europe/Moscow")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PERSONA_PATH = os.path.join(ROOT, os.getenv("COLUMNIST_PERSONA", "persona_private/columnist_ru.md"))
DAY_PATH = os.path.join(ROOT, "columnist_day.json")
JOURNAL_PATH = os.path.join(ROOT, "columnist_journal.json")

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]

# Настроение дня и тоны, которыми он в такой день пишет (тоны из палитры post_builder.TONES)
MOODS = {
    "ироничная грусть": ["холодная ирония", "ностальгия", "усталость и скука", "неожиданное уважение"],
    "азарт": ["азарт", "весёлое хулиганство", "злорадство", "неожиданное уважение"],
    "раздражение": ["брезгливость", "злорадство", "холодная ирония"],
    "усталость": ["усталость и скука", "холодная ирония", "ностальгия"],
    "лёгкость": ["весёлое хулиганство", "неожиданное уважение", "азарт", "ностальгия"],
    "тревога": ["тревога", "холодная ирония", "брезгливость"],
}
# Темперамент: фоновая меланхолия, «ироничная грусть» (библия, §11.4)
BASE_WEIGHTS = {"ироничная грусть": 30, "азарт": 15, "раздражение": 15, "усталость": 15, "лёгкость": 15, "тревога": 10}
INERTIA = 25  # настроение вчерашнего дня частично переходит в сегодняшний
WEEKDAY_WEIGHTS = {
    0: {"раздражение": 10, "усталость": 5},                              # понедельник
    4: {"азарт": 10, "лёгкость": 10},                                    # пятница
    5: {"лёгкость": 10, "ироничная грусть": 5, "раздражение": -5},       # суббота
    6: {"лёгкость": 5, "ироничная грусть": 10, "раздражение": -5},       # воскресенье
}
GREY_WEATHER = {3, 45, 48, 51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99}

# Сцены по распорядку дня (библия, §9.3–9.4). Петербург — на вторые выходные месяца.
SCENES = {
    ("будни", "morning"): [
        "утро: только что сварил кофе в турке, листаешь каналы и делаешь пометки",
        "утро: ждёшь положенные полчаса после таблетки, стоишь у окна с телефоном",
        "утро: до первой встречи час, допиваешь кофе",
    ],
    ("будни", "evening"): [
        "вечер: закончил пересматривать монтаж проекта, который сейчас лечишь",
        "вечер: собираешься на прогулку вокруг пруда",
        "вечер: после позднего обеда на Патриках, впереди ужин и солдатики",
        "вечер: скоро звонить маме, как каждый день в семь",
    ],
    ("суббота", "morning"): ["суббота: долгий завтрак, собираешься в книжный на Тверской", "суббота: никуда не торопишься, второй кофе"],
    ("суббота", "evening"): ["суббота: весь день красил фигуры для диорамы, в лупе, под джаз", "суббота: вернулся с выставки, ноги гудят"],
    ("воскресенье", "morning"): ["воскресенье: позднее утро, никуда не торопишься", "воскресенье: читаешь, кофе остывает"],
    ("воскресенье", "evening"): ["воскресенье: вечер дома, книги и солдатики", "воскресенье: прошёлся вокруг пруда, дома тихо"],
    ("спб-суббота", "morning"): ["суббота: в «Сапсане», едешь в Петербург к маме, место у окна"],
    ("спб-суббота", "evening"): ["суббота: в Петербурге, на Васильевском, у мамы"],
    ("спб-воскресенье", "morning"): ["воскресенье: Петербург, прошёлся по 7-й линии до Невы"],
    ("спб-воскресенье", "evening"): ["воскресенье: вечерний «Сапсан» обратно в Москву"],
}

WMO = [
    ((0,), "ясно"), ((1,), "в основном ясно"), ((2,), "переменная облачность"), ((3,), "пасмурно"),
    ((45, 48), "туман"), ((51, 53, 55, 56, 57), "морось"), ((61, 63, 65, 66, 67), "дождь"),
    ((71, 73, 75, 77), "снег"), ((80, 81, 82), "ливень"), ((85, 86), "снегопад"), ((95, 96, 99), "гроза"),
]
WEATHER_URL = ("https://api.open-meteo.com/v1/forecast?latitude=55.7558&longitude=37.6173"
               "&current=temperature_2m,weather_code&timezone=Europe%2FMoscow")


def load_persona() -> str | None:
    try:
        with open(PERSONA_PATH, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def fetch_weather() -> tuple[str | None, int | None]:
    """Погода в Москве сейчас: («+9°, пасмурно», код WMO). При сбое (None, None)."""
    try:
        cur = requests.get(WEATHER_URL, timeout=8).json()["current"]
        t, code = round(cur["temperature_2m"]), int(cur["weather_code"])
        desc = next((d for codes, d in WMO if code in codes), "")
        return f"{'+' if t > 0 else ''}{t}°" + (f", {desc}" if desc else ""), code
    except Exception as e:
        logger.warning(f"⚠️ [Columnist] Погода недоступна: {e}")
        return None, None


def slot_of(now: datetime) -> str:
    return "morning" if now.hour < 15 else "evening"


def _day_kind(d: date) -> str:
    wd = d.weekday()
    if wd < 5:
        return "будни"
    saturday = d - timedelta(days=wd - 5)
    spb = 8 <= saturday.day <= 14  # вторые выходные месяца: Петербург
    return ("спб-" if spb else "") + ("суббота" if wd == 5 else "воскресенье")


def _calendar(d: date) -> tuple[dict, str | None, float]:
    """Поправки к настроению, внутренний фон дня (вслух не называется) и шанс пропустить вечерний пост."""
    weights, note, skip_evening = {}, None, 0.0
    if (d.month, d.day) == (1, 7):
        weights["лёгкость"] = 30
        note = "Сегодня твой личный светлый день. Ты никому не объясняешь почему."
    if (d.month, d.day) == (3, 17):
        weights.update({"ироничная грусть": 30, "усталость": 10})
        note = "Тяжёлая для тебя годовщина. Ты держишься и ничего не рассказываешь, вечером хочешь побыть один."
        skip_evening = 0.7
    days_to = (date(d.year, 11, 14) - d).days
    if 0 < days_to <= 25:
        weights.update({"тревога": 20 + (15 if days_to <= 4 else 0), "раздражение": 10, "лёгкость": -10})
        note = "Скоро ежегодное обследование. Ты никому об этом не пишешь, но стал резче, суевернее и хуже спишь."
    if days_to == 0:
        weights.update({"тревога": 25, "ироничная грусть": 15})
        note = "Сегодня твой день рождения, и на этой неделе обследование. Ты это не афишируешь и возраст не называешь."
    return weights, note, skip_evening


def _pick_mood(d: date, yesterday_mood: str | None, weather_code: int | None, temp_text: str | None, rng) -> str:
    weights = dict(BASE_WEIGHTS)
    for extra in (WEEKDAY_WEIGHTS.get(d.weekday(), {}), _calendar(d)[0]):
        for k, v in extra.items():
            weights[k] = weights.get(k, 0) + v
    if yesterday_mood in weights:
        weights[yesterday_mood] += INERTIA
    if weather_code in GREY_WEATHER:
        weights["ироничная грусть"] += 10
        weights["усталость"] += 5
    elif weather_code in (0, 1) and temp_text and temp_text.startswith("+") and int(temp_text[1:].split("°")[0]) >= 15:
        weights["лёгкость"] += 10
    moods = [m for m in weights if weights[m] > 0]
    return rng.choices(moods, weights=[weights[m] for m in moods], k=1)[0]


def _load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_day(now: datetime | None = None, fetch: bool = True) -> dict:
    """День Аристарха: создаётся при первом обращении за сутки, погода подтягивается для текущего слота."""
    now = now or datetime.now(MSK)
    today, slot = now.date(), slot_of(now)
    day = _load_json(DAY_PATH, {})
    if day.get("date") != today.isoformat():
        weather, code = fetch_weather() if fetch else (None, None)
        rng = random.Random(f"{today.isoformat()}-aristarkh")
        kind = _day_kind(today)
        _, note, skip_evening = _calendar(today)
        day = {
            "date": today.isoformat(),
            "weekday": WEEKDAYS[today.weekday()],
            "mood": _pick_mood(today, day.get("mood"), code, weather, rng),
            "note": note,
            "skip_evening": skip_evening,
            "scenes": {s: rng.choice(SCENES[(kind, s)]) for s in ("morning", "evening")},
            "weather": {"morning": weather if slot == "morning" else None,
                        "evening": weather if slot == "evening" else None},
        }
        _save_json(DAY_PATH, day)
        logger.info(f"🗓️ [Columnist] Новый день: {day['weekday']}, настроение «{day['mood']}», {weather or 'погода неизвестна'}")
    elif fetch and not day["weather"].get(slot):
        day["weather"][slot], _ = fetch_weather()
        _save_json(DAY_PATH, day)
    return day


def pick_tone(day: dict, last_tone: str | None, rng=random) -> str:
    tones = MOODS.get(day.get("mood"), MOODS["ироничная грусть"])
    return rng.choice([t for t in tones if t != last_tone] or tones)


def describe_day(day: dict, slot: str, mention_allowed: bool) -> str:
    d = date.fromisoformat(day["date"])
    lines = [
        "[ТВОЙ ДЕНЬ — фон, а не тема поста]",
        f"Сегодня {day['weekday']}, {d.day} {MONTHS[d.month - 1]}."
        + (f" В Москве сейчас {day['weather'][slot]}." if day["weather"].get(slot) else ""),
        f"Настроение дня: {day['mood']}.",
        f"Где ты и что делаешь: {day['scenes'][slot]}.",
        ("Можешь мимоходом, одной фразой в середине или в конце, упомянуть что-то из своего дня или погоду, "
         "если это ложится естественно. Не начинай с этого пост."
         if mention_allowed else "В этом посте не упоминай свой день и погоду: просто пиши в этом настроении."),
    ]
    if day.get("note"):
        lines.append(f"Внутренний фон (никогда не называй его и не намекай прямо, он только окрашивает тон): {day['note']}")
    return "\n".join(lines)


def skip_probability(day: dict, slot: str) -> float:
    """Шанс, что сегодня в этот слот «не пишется» (как у живого автора). По умолчанию 0."""
    weekend = date.fromisoformat(day["date"]).weekday() >= 5
    try:
        base = float(os.getenv("SKIP_PROB_WEEKEND" if weekend else "SKIP_PROB_WEEKDAY", "0") or 0)
    except ValueError:
        base = 0.0
    return max(base, day.get("skip_evening", 0.0)) if slot == "evening" else base


def recent_posts_block(history: list[dict], callback_allowed: bool, n: int = 6) -> str:
    lines = []
    for h in history[-n:]:
        if not h.get("opening"):
            continue
        when = ""
        if h.get("published_at"):
            when = datetime.fromisoformat(h["published_at"]).astimezone(MSK).strftime("%d.%m") + ": "
        lines.append(f"- {when}«{h['opening']}»")
    if not lines:
        return ""
    rule = ("Если это действительно в тему, можешь одной фразой сослаться на свой недавний пост "
            "(«на днях писал про…»), но не пересказывай его." if callback_allowed
            else "Не ссылайся на эти посты и не повторяй их мысли.")
    return "[ЧТО ТЫ ПИСАЛ НЕДАВНО]\n" + "\n".join(lines) + "\n" + rule


def journal_recent(n: int = 4) -> list[dict]:
    return _load_json(JOURNAL_PATH, [])[-n:]


def journal_add(kind: str, text: str, keep: int = 60):
    entries = _load_json(JOURNAL_PATH, [])
    entries.append({"date": datetime.now(MSK).date().isoformat(), "kind": kind, "text": text})
    _save_json(JOURNAL_PATH, entries[-keep:])
