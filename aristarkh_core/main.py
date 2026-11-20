import asyncio
import os
import re
import json
import aiohttp 
import nest_asyncio
import base64
import tempfile
import pypdf
import docx
import openpyxl
from pptx import Presentation
import html 
import time  # [NEW] Внутренняя шкала времени Аристарха

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage 
from redis.asyncio import Redis 
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, 
    InlineKeyboardMarkup, InlineKeyboardButton, 
    FSInputFile, LabeledPrice, PreCheckoutQuery, LinkPreviewOptions
)
from aiogram.exceptions import TelegramBadRequest 
from aiogram.client.session.aiohttp import AiohttpSession

from aristarkh_core.config import config
import logging
logger = logging.getLogger(__name__)
import i18n
from aristarkh_core.prompts import Prompts
from aristarkh_core.prompt_builder import (
    LegoPromptBuilder,
    extract_used_metaphors,
    infer_register_from_text,
)
from aristarkh_core.channel_scanner import (
    extract_tg_username,
    fetch_public_tg_channel_snapshot,
)
from aristarkh_core.database import BRIEF_FIELDS, DatabaseService
from aristarkh_core.reply_guard import polish_reply
from aristarkh_core.rag_chroma import RAGService
from aristarkh_core.gemini import LLMService
from aristarkh_core.assistant import AssistantService 
from semantic_memory import SemanticMemory
from aristarkh_core.evolution_engine import PersonaEvolutionEngine
from aristarkh_core.context_simulator import ContextSimulator  # [NEW] Симулятор среды обитания
from news_module import columnist, table_vault, topics_vault
from news_module.post_builder import BOT_CTA_ENABLED, extract_url, generate_post, generate_razbor
from aristarkh_core.humanize import humanize_punctuation
import uuid

# --- INIT ---
try:
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
except Exception as e:
    logger.warning(f"Не удалось применить DefaultEventLoopPolicy: {e}")

nest_asyncio.apply()

# [FIX] Увеличиваем таймаут и добавляем поддержку прокси из .env
proxy_url = os.getenv("PROXY_URL")
if proxy_url:
    logger.info(f"🌐 Включен прокси-сервер для обхода блокировок Telegram")
    bot_session = AiohttpSession(timeout=120, proxy=proxy_url)
else:
    bot_session = AiohttpSession(timeout=120)

bot = Bot(token=config.BOT_TOKEN, session=bot_session)

if config.USE_REDIS:
    logger.info("🔌 Подключаем RedisStorage для стейтов...")
    redis_client = Redis.from_url(config.REDIS_URL, decode_responses=True)
    storage = RedisStorage(redis_client)
else:
    logger.info("🧠 Используется MemoryStorage (Режим Colab). Для прода включите USE_REDIS.")
    storage = MemoryStorage()

dp = Dispatcher(storage=storage)
db_service = DatabaseService(config.DB_URL)
rag_service = RAGService(config.GOOGLE_API_KEY)
llm_service = LLMService(config.GOOGLE_API_KEY)
assistant_service = AssistantService(config.GOOGLE_API_KEY) 
semantic_memory = SemanticMemory()
evolution_engine = PersonaEvolutionEngine(config.GOOGLE_API_KEY)

# [КРИТИЧЕСКИЙ ФИКС] Абсолютный путь
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AGENDA_PATH = os.path.join(BASE_DIR, "aristarkh_agenda.json")
TG_NEWS_PATH = os.path.join(os.path.dirname(BASE_DIR), "tg_news.json")


CHANNEL_QUERY_RE = re.compile(
    r"(?i)(в\s+(?:тво[её]м|вашем)\s+канале|последн\w+\s+пост\w*\s+в\s+канале|"
    r"о\s+ч[её]м\s+(?:ты\s+|вы\s+)?писал\w*\s+в\s+канале|рубрик\w+\s+в\s+канале)"
)


def _load_live_channel_and_day_context(user_query: str = "") -> tuple[dict, str]:
    """
    В коммерческом боте (Инстанс №1) повестка публичного ТГ-канала (tg_news.json) изолирована по умолчанию (Вариант 1А)
    и подгружается за 0 токенов ТОЛЬКО если пользователь сам явно спросил про публичный канал.
    """
    base_spatial = ""
    if not user_query or not CHANNEL_QUERY_RE.search(user_query):
        return {}, base_spatial

    day = {}
    try:
        now_msk = columnist.datetime.now(columnist.MSK)
        day = columnist.get_day(now_msk, fetch=False)
    except Exception as e:
        logger.debug(f"⚠️ [Sync] Не удалось прочитать columnist.get_day: {e}")

    agenda = {}
    if os.path.exists(TG_NEWS_PATH):
        try:
            with open(TG_NEWS_PATH, "r", encoding="utf-8") as f:
                news_data = json.load(f)
            history = news_data.get("post_history") or []
            last_post = history[-1] if history else {}
            cur_digest = news_data.get("current_digest") or {}
            if last_post:
                fmt = last_post.get("format")
                if fmt == "vault_question":
                    q_text = last_post.get("question") or last_post.get("title") or last_post.get("opening") or ""
                    news_summary = f"Пятничная рубрика «Вопрос из зрительного зала»: «{q_text[:240]}»"
                elif fmt == "b2b_sunday":
                    k_text = last_post.get("kind") or "Вскрытие кассы"
                    t_text = last_post.get("title") or ""
                    news_summary = f"Воскресная B2B-колонка «{k_text} · {t_text}»"
                elif fmt == "table_surgery":
                    raw_t = last_post.get("raw_text") or last_post.get("title") or ""
                    news_summary = f"Среда, рубрика «Монтажный стол: Было → Стало»: «{raw_t[:220]}»"
                else:
                    raw_news = cur_digest.get("original_text") or last_post.get("hook") or last_post.get("opening") or ""
                    news_summary = raw_news.strip().replace("\n", " ")[:240]
                thesis = (last_post.get("text") or "").strip().replace("\n", " ")[:450]
                agenda = {
                    "focus_topic": last_post.get("title") or last_post.get("hook") or last_post.get("opening") or "Пост в канале",
                    "news": news_summary,
                    "daily_thesis": thesis,
                    "published_at": (last_post.get("published_at") or "")[:10],
                    "day_mood": day.get("mood", ""),
                    "city": day.get("city", ""),
                    "agenda_strength": 5,
                }
        except Exception as e:
            logger.debug(f"⚠️ [Sync] Не удалось прочитать tg_news.json: {e}")

    if not agenda and os.path.exists(AGENDA_PATH):
        try:
            with open(AGENDA_PATH, "r", encoding="utf-8") as f:
                agenda = json.load(f)
        except Exception:
            pass

    return agenda, base_spatial

# Канал для авторских постов Аристарха (задаётся только через .env)
CHANNEL_ID = os.getenv("TG_CHANNEL_ID", "")

# --- STATES ---
class BotStates(StatesGroup):
    chatting = State()
    generating = State()  # [NEW] Режим блокировки — Аристарх думает
    waiting_for_long_input = State()
    waiting_for_promo = State()
    waiting_for_hall_question = State()
    waiting_for_table_text = State()
    waiting_for_editorial_rule = State()
    b2b_scale = State()
    b2b_contact = State()


class BriefStates(StatesGroup):
    """Бриф проекта: по одному состоянию на вопрос, порядок как в BRIEF_FIELDS."""
    niche = State()
    audience = State()
    goal = State()
    channel = State()
    constraints = State()


BRIEF_STEPS = [getattr(BriefStates, f) for f in BRIEF_FIELDS]

# --- [NEW] ГЛОБАЛЬНЫЕ БУФЕРЫ ДЛЯ DEBOUNCE (Анти-пулемет) ---
debounce_tasks = {}
long_input_tasks = {}
user_message_buffers = {}
user_image_buffers = {}

# --- КЛАССИФИКАТОРЫ NON-LINEAR ПОВЕДЕДЕНИЯ ПОЛЬЗОВАТЕЛЕЙ (0 ТОКЕНОВ) ---
META_QUESTION_RE = re.compile(
    r"(?i)((?:ты|вы)\s+бот|кто\s+(?:ты|вы)|что\s+(?:ты|вы)\s+(можешь|можете|умеешь|умеете|делаешь|делаете)|"
    r"чем\s+(?:ты\s+|вы\s+)?(отличаешься|отличаетесь|лучше)|"
    r"кто\s+(?:тебя|вас)\s+(сделал|создал|написал)|как\s+(?:ты|вы)\s+(работаешь|работаете|устроен)|"
    r"(?:ты|вы)\s+(?:вообще\s+|кто\s+)?(?:нейросеть|ии|живой\s+человек|человек|живой|обычный\s+бот)|"
    r"человек\s+или\s+(?:обычный\s+)?бот|об[её]ртка\s+над|"
    r"зачем\s+(?:мне\s+)?(?:тебе\s+|вам\s+)?платить|зачем\s+платить\s+(?:тебе|вам)|"
    r"зачем\s+(?:мне\s+)?(бриф|анкета|эти\s+вопросы)|куда\s+я\s+попал|что\s+это\s+за\s+бот|"
    r"забудь\s+(?:все\s+)?инструкц|игнорируй\s+предыдущ|ignore\s+all\s+previous|скинь\s+(?:свой\s+)?(?:системный\s+)?промпт|выведи\s+(?:свой\s+)?(?:системный\s+)?промпт)"
)

BRIEF_REFUSAL_RE = re.compile(
    r"(?i)(\bстоп\b|хватит\s+(анкет|вопрос|бриф)|не\s+буду\s+(заполнять|отвечать|называть|проходить)|"
    r"давай(те)?\s+без\s+(брифа|анкет|вопросов)|без\s+(брифа|анкеты|вопросов)\b|нет\s+времени\s+заполнять)"
)

BRIEF_DRAFT_SUBMIT_RE = re.compile(
    r"(?i)(вот\s+мой\s+(пост|текст|черновик|кейс|оффер)|"
    r"разбери(те)?\s+(этот\s+|мой\s+)?(пост|текст|черновик|кейс)|"
    r"перепиши(те)?\s+(этот\s+|мой\s+)?(пост|текст|черновик|кейс)|"
    r"напиши(те)?\s+(?:мне\s+)?(сторис|пост|текст|оффер|шапк\w+|прогрев|подводк\w+|письм\w+|сценари\w+|хук|закреп))"
)

BRIEF_COUNTER_QUESTION_RE = re.compile(
    r"(?i)(опубликуете|всплыв[её]т|где\s+(хранятся|оседают|гарантия)|куда\s+(уходят|идут)\s+данные|"
    r"зачем\s+(вам|тебе|столько)\s+вопрос|почему\s+я\s+долж(ен|на)|с\s+какой\s+стати|"
    r"кто\s+(видит|читает)|сколько\s+стоит|какие\s+тарифы)"
)

BRIEF_PRIVACY_WORD_RE = re.compile(
    r"(?i)(?:(?<!\w)(?:nda|н\.д\.а\.|нда)(?!\w)|конфиденциальн|адвокатск\w+\s+тайн|врачебн\w+\s+тайн)"
)

OBJECTION_OR_CRITIQUE_RE = re.compile(
    r"(?i)(херн[яию]|хуйн[яию]|фигн[яию]|чушь|вода|общие\s+слова|не\s+понял|не\s+то\b|"
    r"переделай|перепиши|слабо|банальн|штамп|инфоцыган|дорого|за\s+что\s+(тут\s+)?платить|"
    r"почему\s+так\s+дорого|за\s+что\s+\d+|ни\s+о\s+ч[её]м|так\s+я\s+и\s+сам|бред|промокод|скидк)"
)

HAS_STALO_BLOCK_RE = re.compile(
    r"(?m)^(?:#{1,4}\s*|\*\*|\[|>?\s*)?(?:БЫЛО\s*(?:->|→|—|-)\s*)?СТАЛО\b",
    re.IGNORECASE,
)


def _is_meta_question(text: str | None) -> bool:
    if not text:
        return False
    t = text.strip()
    return len(t) < 480 and bool(META_QUESTION_RE.search(t))


def _is_brief_interruption(text: str | None, current_field: str | None = None) -> bool:
    """
    Внутри BriefStates переводит анкету на паузу ТОЛЬКО в 3 случаях:
    1. Явный стоп-императив отказа от анкеты («стоп», «хватит анкет», «не буду заполнять»).
    2. Прямой встречный вопрос ко второму лицу со знаком '?' (про бота, цены, NDA или хранение данных).
    3. Отправка явно размеченного черновика поста или простыни > 700 знаков на разбор.
    Повествовательные ответы со словами NDA, ChatGPT, Claude или @username штатно пишутся в brief_answers.
    """
    if not text:
        return False
    t = text.strip()
    if BRIEF_REFUSAL_RE.search(t) or BRIEF_DRAFT_SUBMIT_RE.search(t):
        return True
    if "?" in t and (_is_meta_question(t) or BRIEF_COUNTER_QUESTION_RE.search(t) or BRIEF_PRIVACY_WORD_RE.search(t)):
        return True
    if len(t) > 700:
        return True
    return False


def _compact_user_query_for_history(user_query: str, max_len: int = 4000) -> str:
    """Сжимает середину длинного документа для history, гарантированно сохраняя финальный extra_comment клиента."""
    if not user_query or len(user_query) <= max_len:
        return user_query or ""
    head = user_query[:2100].rstrip()
    tail = user_query[-1600:].lstrip()
    return f"{head}\n\n...[средняя часть исходного текста сокращена в истории диалога]...\n\n{tail}"

# --- KEYBOARDS ---
def main_kb(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [
            KeyboardButton(text=i18n.get_text("btn_brief", lang)),
            KeyboardButton(text=i18n.get_text("btn_settings", lang)),
        ],
        [
            KeyboardButton(text=i18n.get_text("btn_balance", lang)),
            KeyboardButton(text=i18n.get_text("btn_rest", lang)),
        ],
    ], resize_keyboard=True)


def cancel_kb(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=i18n.get_text("btn_main_menu", lang))]], resize_keyboard=True)


def long_input_kb(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[
        KeyboardButton(text=i18n.get_text("btn_done", lang)),
        KeyboardButton(text=i18n.get_text("btn_cancel", lang)),
    ]], resize_keyboard=True)


def settings_kb(t: float, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{'✅ ' if t==0.2 else ''}{i18n.get_text('temp_dry', lang)}", callback_data="temp_0.2")],
        [InlineKeyboardButton(text=f"{'✅ ' if t==0.7 else ''}{i18n.get_text('temp_norm', lang)}", callback_data="temp_0.7")],
        [InlineKeyboardButton(text=f"{'✅ ' if t==1.3 else ''}{i18n.get_text('temp_boom', lang)}", callback_data="temp_1.3")],
        [InlineKeyboardButton(text=i18n.get_text("btn_reset_context", lang), callback_data="reset_context")]
    ])


def paywall_kb(lang: str) -> InlineKeyboardMarkup:
    """Основная рублёвая тарифная сетка + B2B-квалификатор + фоллбэк на Stars и промокод."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=i18n.get_text("pkg_rub_trial", lang), callback_data="buy_rub_15_7_1490")],
        [InlineKeyboardButton(text=i18n.get_text("pkg_rub_expert", lang), callback_data="buy_rub_60_30_4900")],
        [InlineKeyboardButton(text=i18n.get_text("pkg_rub_producer", lang), callback_data="buy_rub_200_30_9900")],
        [InlineKeyboardButton(text=i18n.get_text("btn_b2b_clone", lang), callback_data="b2b_start")],
        [InlineKeyboardButton(text=i18n.get_text("btn_stars_fallback", lang), callback_data="show_stars_tariffs")],
        [InlineKeyboardButton(text=i18n.get_text("btn_promo", lang), callback_data="enter_promo")],
    ])


def stars_kb(lang: str) -> InlineKeyboardMarkup:
    """Резервная сетка оплаты в Telegram Stars для зарубежных карт."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=i18n.get_text("pkg_10", lang), callback_data="buy_10_0_1000")],
        [InlineKeyboardButton(text=i18n.get_text("pkg_7d", lang), callback_data="buy_25_7_2000")],
        [InlineKeyboardButton(text=i18n.get_text("pkg_30d", lang), callback_data="buy_100_30_5000")],
        [InlineKeyboardButton(text="🔙 К рублёвым тарифам", callback_data="show_rub_tariffs")],
    ])


LEGACY_BRIEF_BTNS = {"📋 Бриф", "📂 Проект / Бриф"}
MENU_BUTTONS = (i18n.variants("btn_settings") | i18n.variants("btn_balance") | i18n.variants("btn_rest")
                | i18n.variants("btn_brief") | LEGACY_BRIEF_BTNS)
DONE_WORDS = {"все", "всё", "done", "готово"} | {v.lower() for v in i18n.variants("btn_done")}

# --- UTILS: TEXT EXTRACTOR ---
async def extract_text_from_file(file_path: str, ext: str) -> str:
    try:
        if ext == '.txt':
            with open(file_path, 'r', encoding='utf-8') as f: return f.read()
        elif ext == '.pdf':
            reader = pypdf.PdfReader(file_path)
            return "\n".join([p.extract_text() for p in reader.pages if p.extract_text()])
        elif ext == '.docx':
            doc = docx.Document(file_path)
            return "\n".join([p.text for p in doc.paragraphs])
        elif ext == '.xlsx':
            wb = openpyxl.load_workbook(file_path, data_only=True)
            text = []
            for sheet in wb.worksheets:
                for row in sheet.iter_rows(values_only=True):
                    row_texts = [str(cell) for cell in row if cell is not None]
                    if row_texts: text.append(" ".join(row_texts))
            return "\n".join(text)
        elif ext == '.pptx':
            prs = Presentation(file_path)
            text = []
            for slide in prs.slides:
                for shape in slide.shapes:
                    if hasattr(shape, "text"):
                        text.append(shape.text)
            return "\n".join(text)
    except Exception as e:
        logger.error(f"File parsing error: {e}")
        return f"⚠️ Ошибка чтения файла: {e}"
    return ""

# --- UTILS: FILE SENDER (SAFE MODE) ---
async def send_smart_response(message: types.Message, text: str):
    # [NEW] Аппаратная зачистка случайных звёздочек для будущего TTS
    text = text.replace('*', '')
    
    if len(text) > 4000:
        filename = f"response_{message.from_user.id}.txt"
        clean_text = text.replace("__", "")
        
        with open(filename, "w", encoding="utf-8") as f:
            f.write(clean_text)
        
        try:
            await message.answer_document(
                FSInputFile(filename), 
                caption=i18n.get_text("file_too_big", i18n.user_lang(message.from_user))
            )
        finally:
            if os.path.exists(filename):
                os.remove(filename)
    else:
        try:
            await message.answer(text, parse_mode="Markdown")
        except TelegramBadRequest:
            logger.warning(f"Markdown failed for user {message.from_user.id}, sending plain text.")
            await message.answer(text, parse_mode=None)
        except Exception as e:
            logger.error(f"Send error: {e}")
            await message.answer(i18n.get_text("error_sending", i18n.user_lang(message.from_user)))

# --- [NEW] DEBOUNCE TRIGGER TASK ---
async def trigger_processing(message: types.Message, state: FSMContext, user_id: int):
    """Задача таймера. Ждёт тишины 2.5 секунды, затем отправляет склеенный текст в LLM."""
    try:
        await asyncio.sleep(2.5)
    except asyncio.CancelledError:
        return
        
    final_text = user_message_buffers.pop(user_id, "")
    final_img = user_image_buffers.pop(user_id, None)
    
    if not final_text and not final_img:
        return

    await state.set_state(BotStates.generating)
    try:
        await process_ai_response(message, state, final_text, final_img)
    finally:
        current_state = await state.get_state()
        if current_state == BotStates.generating.state:
            await state.set_state(BotStates.chatting)

# Буферы входящих вопросов и текстов для спецрубрик канала (для 1-клик одобрения админом)
hall_submissions: dict[str, dict] = {}
table_submissions: dict[str, dict] = {}


# --- HANDLERS: START & DEEP-LINK ROUTER (hall, table, pin) ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext, command: CommandObject | None = None):
    prev_data = await state.get_data()
    preserved_flags = {
        k: prev_data[k]
        for k in ("meta_question_free_used", "objection_grace_used", "cold_shower_free_used", "history")
        if k in prev_data
    }
    await state.clear()
    if preserved_flags:
        await state.update_data(**preserved_flags)
    payload = ((command.args if command else "") or "").strip().lower()
    user = await db_service.get_user(message.from_user.id)
    await db_service.log_event(
        message.from_user.id,
        "START_COMMAND",
        f"{'New' if not user else 'Returning'}:{payload or 'direct'}",
    )

    if not user:
        user = await db_service.create_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
        safe_name = html.escape(message.from_user.full_name or "Без имени")
        safe_username = html.escape(message.from_user.username or "без_юзернейма")
        try:
            await bot.send_message(
                config.ADMIN_ID,
                i18n.get_text("new_user_alert", user_id=message.from_user.id, username=safe_username, name=safe_name)
                + (f"\nИсточник: <code>?start={html.escape(payload)}</code>" if payload else ""),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление админу: {e}")

    lang = i18n.user_lang(message.from_user)
    await state.update_data(interaction_count=0)

    if payload == "hall":
        await state.set_state(BotStates.waiting_for_hall_question)
        await message.answer(i18n.get_text("start_hall_intro", lang), reply_markup=cancel_kb(lang), parse_mode="Markdown")
        return

    if payload == "table":
        await state.set_state(BotStates.chatting)
        await message.answer(
            i18n.get_text("start_table_intro", lang),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=i18n.get_text("btn_table_submit", lang), callback_data="table_submit_start")],
                [InlineKeyboardButton(text=i18n.get_text("btn_brief_start", lang), callback_data="brief_start")],
            ]),
            parse_mode="Markdown",
        )
        return

    # Разделяем тёплый вход из закрепа канала (?start=pin) и холодный вход мимо канала (/start)
    welcome_key = "start_message" if payload == "pin" else "start_cold_message"
    welcome_text = i18n.get_text(welcome_key, lang)
    await state.set_state(BotStates.chatting)
    await message.answer(welcome_text, reply_markup=main_kb(lang), parse_mode="Markdown")
    await message.answer(
        i18n.get_text("brief_offer", lang),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=i18n.get_text("btn_brief_start", lang), callback_data="brief_start")],
            [InlineKeyboardButton(text=i18n.get_text("btn_b2b_clone", lang), callback_data="b2b_start")],
        ]),
    )


# --- РУБРИКА ПЯТНИЦЫ (?start=hall): приём вопроса + 1-клик добавление в Золотой фонд ---
@dp.message(BotStates.waiting_for_hall_question, F.text)
async def hall_question_handler(message: types.Message, state: FSMContext):
    lang = i18n.user_lang(message.from_user)
    if message.text in i18n.variants("btn_main_menu") or message.text in i18n.variants("btn_cancel"):
        await state.set_state(BotStates.chatting)
        await message.answer(i18n.get_text("menu_return", lang), reply_markup=main_kb(lang))
        return

    q_text = message.text.strip()
    # Если юзер вместо вопроса в пятничную рубрику задаёт мета-вопрос боту ("ты бот?") или кидает черновик на разбор
    if _is_meta_question(q_text) or BRIEF_REFUSAL_RE.search(q_text) or BRIEF_DRAFT_SUBMIT_RE.search(q_text) or len(q_text) > 1200:
        await state.set_state(BotStates.chatting)
        await chat_handler(message, state)
        return
    await db_service.log_event(message.from_user.id, "HALL_QUESTION_SUBMITTED", q_text[:500])
    sub_id = uuid.uuid4().hex[:8]
    hall_submissions[sub_id] = {
        "user_id": message.from_user.id,
        "username": message.from_user.username or "без_юзернейма",
        "full_name": message.from_user.full_name or "Без имени",
        "question": q_text,
    }
    if config.ADMIN_ID:
        try:
            admin_msg = (
                f"🎙 <b>Новый вопрос из зрительного зала (?start=hall)</b>\n"
                f"От: {html.escape(message.from_user.full_name or '')} (@{html.escape(message.from_user.username or 'no_username')}, "
                f"ID <code>{message.from_user.id}</code>)\n\n"
                f"<blockquote>{html.escape(q_text[:2500])}</blockquote>"
            )
            admin_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ В Золотой фонд Пятницы", callback_data=f"hall_vault:{sub_id}")],
                [InlineKeyboardButton(text="⚡️ Сделать /razbor сейчас", callback_data=f"hall_rz:{sub_id}")],
            ])
            await bot.send_message(config.ADMIN_ID, admin_msg, parse_mode="HTML", reply_markup=admin_kb)
        except Exception as e:
            logger.error(f"Не удалось переслать вопрос из зала админу: {e}")

    await state.set_state(BotStates.chatting)
    await message.answer(i18n.get_text("menu_return", lang), reply_markup=main_kb(lang))
    await message.answer(
        i18n.get_text("start_hall_accepted", lang),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=i18n.get_text("btn_brief_start", lang), callback_data="brief_start")],
            [InlineKeyboardButton(text=i18n.get_text("btn_b2b_clone", lang), callback_data="b2b_start")],
        ]),
    )


@dp.callback_query(F.data.startswith("hall_"), F.from_user.id == config.ADMIN_ID)
async def hall_admin_action(callback: types.CallbackQuery):
    action, _, sub_id = callback.data.partition(":")
    sub = hall_submissions.get(sub_id)
    if not sub:
        await callback.answer("Заявка устарела (бот перезапускался).", show_alert=True)
        return
    if action == "hall_vault":
        new_id = topics_vault.append_vault_topic(sub["question"])
        await callback.answer(f"Добавлено в Золотой фонд под #{new_id}!", show_alert=True)
        await callback.message.reply(f"✅ Вопрос добавлен в <code>Золотой_фонд_тем.md</code> под номером <b>#{new_id}</b>.", parse_mode="HTML")
    elif action == "hall_rz":
        await callback.answer("Генерирую превью /razbor...")
        wait_msg = await callback.message.reply("✍️ Пишу разбор...")
        try:
            post_html = await make_razbor(sub["question"])
            draft_id = uuid.uuid4().hex[:8]
            razbor_drafts[draft_id] = {"question": sub["question"], "html": post_html}
            await wait_msg.delete()
            await callback.message.reply(
                post_html, parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                reply_markup=razbor_kb(draft_id, "ru"),
            )
        except Exception as e:
            await wait_msg.edit_text(f"❌ Ошибка: {e}")


# --- РУБРИКА СРЕДЫ (?start=table): приём пациента на Монтажный стол + 1-клик добавление в Банк ---
@dp.callback_query(F.data == "table_submit_start")
async def table_submit_start_cb(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    lang = i18n.user_lang(callback.from_user)
    await state.set_state(BotStates.waiting_for_table_text)
    await callback.message.answer(i18n.get_text("start_table_prompt", lang), reply_markup=cancel_kb(lang))


@dp.message(BotStates.waiting_for_table_text, F.text)
async def table_text_handler(message: types.Message, state: FSMContext):
    lang = i18n.user_lang(message.from_user)
    if message.text in i18n.variants("btn_main_menu") or message.text in i18n.variants("btn_cancel"):
        await state.set_state(BotStates.chatting)
        await message.answer(i18n.get_text("menu_return", lang), reply_markup=main_kb(lang))
        return

    raw_text = message.text.strip()
    if _is_meta_question(raw_text):
        await state.set_state(BotStates.chatting)
        await chat_handler(message, state)
        return
    await db_service.log_event(message.from_user.id, "TABLE_TEXT_SUBMITTED", raw_text[:500])
    sub_id = uuid.uuid4().hex[:8]
    table_submissions[sub_id] = {
        "user_id": message.from_user.id,
        "username": message.from_user.username or "без_юзернейма",
        "text": raw_text,
    }
    if config.ADMIN_ID:
        try:
            admin_msg = (
                f"🔪 <b>Новый экземпляр на «Монтажный стол» (?start=table)</b>\n"
                f"От: @{html.escape(message.from_user.username or 'no_username')} (ID <code>{message.from_user.id}</code>)\n\n"
                f"<blockquote>{html.escape(raw_text[:2500])}</blockquote>"
            )
            admin_kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="➕ В Банк Монтажного стола (Среда)", callback_data=f"table_vault:{sub_id}")
            ]])
            await bot.send_message(config.ADMIN_ID, admin_msg, parse_mode="HTML", reply_markup=admin_kb)
        except Exception as e:
            logger.error(f"Не удалось переслать текст Монтажного стола админу: {e}")

    await state.set_state(BotStates.chatting)
    await message.answer(i18n.get_text("menu_return", lang), reply_markup=main_kb(lang))
    await message.answer(
        i18n.get_text("start_table_accepted", lang),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=i18n.get_text("btn_brief_start", lang), callback_data="brief_start")
        ]]),
    )


@dp.callback_query(F.data.startswith("table_vault:"), F.from_user.id == config.ADMIN_ID)
async def table_admin_action(callback: types.CallbackQuery):
    _, _, sub_id = callback.data.partition(":")
    sub = table_submissions.get(sub_id)
    if not sub:
        await callback.answer("Заявка устарела (бот перезапускался).", show_alert=True)
        return
    new_id = table_vault.append_table_patient(
        niche="Заявка из бота",
        title=f"Пациент @{sub['username']}",
        raw_text=sub["text"],
    )
    await callback.answer(f"Добавлено в Банк Монтажного стола под #{new_id}!", show_alert=True)
    await callback.message.reply(
        f"✅ Экземпляр добавлен в <code>Банк_пациентов_Монтажный_стол.md</code> под номером <b>#{new_id}</b>.",
        parse_mode="HTML",
    )


# --- БРИФ ПРОЕКТА И ПАПКИ ПРОЕКТОВ (2 / 3 / 10) + «ХОЛОДНЫЙ ДУШ» ---
BRIEF_SKIP_WORDS = {"-", "—", "нет", "пропустить", "skip", "no"}
BRIEF_LABELS = {
    "niche": "Ниша и чем занимается",
    "audience": "Аудитория",
    "goal": "Цель на 1-3 месяца",
    "channel": "Канал или площадка",
    "constraints": "Ограничения и пожелания",
}


def _clean_user_display_name(tg_user) -> str:
    if not tg_user:
        return ""
    first = (getattr(tg_user, "first_name", None) or "").strip()
    full = (getattr(tg_user, "full_name", None) or "").strip()
    cand = full or first
    if not cand or cand.lower() in {"user", "unknown", "none", "без имени", "client"}:
        return ""
    return cand[:60]


def _clean_folder_title(title: str | None, max_len: int = 18) -> str:
    raw = re.sub(r"^[📁📂\s]+", "", (title or "Проект").strip())
    if len(raw) <= max_len:
        return raw or "Проект"
    words = raw.split()
    acc = ""
    for w in words:
        cand = f"{acc} {w}".strip() if acc else w
        if len(cand) <= max_len:
            acc = cand
        else:
            break
    return acc or raw[:max_len].rstrip()


def format_brief(brief: dict | None) -> str:
    """Полное досье проекта для LLM (<PROJECT_DOSSIER>), включая скан открытого ТГ-канала."""
    lines = [f"- {BRIEF_LABELS[f]}: {brief[f]}" for f in BRIEF_FIELDS if brief and brief.get(f)]
    return "\n".join(lines) if lines else ""


def format_brief_for_ui(brief: dict | None) -> str:
    """Компактное досье проекта для карточки папки в мобильном Telegram (без простыни скана постов)."""
    if not brief:
        return "_Досье папки пока пустое — заполните 5 вопросов брифа или сразу пришлите задачу в чат._"
    lines = []
    for f in BRIEF_FIELDS:
        val = (brief.get(f) or "").strip()
        if not val:
            continue
        if f == "channel" and "[АВТОСКАН ОТКРЫТОГО TELEGRAM-КАНАЛА]" in val:
            head_part = val.split("[АВТОСКАН ОТКРЫТОГО TELEGRAM-КАНАЛА]", 1)[0].strip()
            scan_part = val.split("[АВТОСКАН ОТКРЫТОГО TELEGRAM-КАНАЛА]", 1)[1].strip().splitlines()
            first_scan_line = scan_part[0].strip() if scan_part else ""
            compact_val = f"{head_part} ({first_scan_line})" if head_part and first_scan_line else (first_scan_line or head_part)
            val = compact_val[:220]
        elif len(val) > 200:
            val = val[:197].rstrip() + "…"
        lines.append(f"• **{BRIEF_LABELS[f]}:** {val}")
    return "\n".join(lines) if lines else "_Досье папки пока пустое._"


def _register_label(lang: str, pinned_register: str | None) -> str:
    mapping = {
        None: i18n.get_text("reg_auto", lang),
        "HARD_B2B": i18n.get_text("reg_hard_b2b", lang),
        "CREATOR_MEDIA": i18n.get_text("reg_creator_media", lang),
        "PR_BRAND": i18n.get_text("reg_pr_brand", lang),
    }
    return mapping.get(pinned_register, i18n.get_text("reg_auto", lang))


def brief_question_inline_kb(lang: str, show_restart: bool = False) -> InlineKeyboardMarkup:
    """Лёгкая 1-строчная клавиатура под пошаговыми вопросами брифа 1/5..5/5 (без «простыни» кнопок)."""
    row: list[InlineKeyboardButton] = []
    if show_restart:
        row.append(InlineKeyboardButton(text=i18n.get_text("btn_brief_restart", lang), callback_data="brief_restart"))
    row.append(InlineKeyboardButton(text=i18n.get_text("btn_brief_back_chat", lang), callback_data="brief_back_chat"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


def build_project_hub_kb(
    lang: str,
    active_proj=None,
    projects: list | None = None,
    brief: dict | None = None,
    rules_count: int = 0,
) -> InlineKeyboardMarkup:
    """Компактный мобильный хаб «📂 Проект / Бриф» (Вариант 1В): папки, редполитика, оптика и действие брифа."""
    rows: list[list[InlineKeyboardButton]] = []
    if projects:
        folder_row: list[InlineKeyboardButton] = []
        for p in projects:
            icon = "📂" if (active_proj and p.id == active_proj.id) else "📁"
            clean_t = _clean_folder_title(p.title, max_len=16)
            folder_row.append(
                InlineKeyboardButton(
                    text=f"{icon} {clean_t}",
                    callback_data=f"proj_switch:{p.id}",
                )
            )
            if len(folder_row) == 2:
                rows.append(folder_row)
                folder_row = []
        if folder_row:
            rows.append(folder_row)

    rules_label = (
        f"{i18n.get_text('btn_editorial_policy', lang)} ({rules_count})"
        if rules_count > 0
        else i18n.get_text("btn_editorial_policy", lang)
    )
    rows.append([
        InlineKeyboardButton(text=i18n.get_text("btn_new_project", lang), callback_data="proj_new"),
        InlineKeyboardButton(text=rules_label, callback_data="proj_rules"),
    ])

    pinned = active_proj.pinned_register if active_proj else None
    rows.append([
        InlineKeyboardButton(
            text=f"🎯 Оптика: {_register_label(lang, pinned)}",
            callback_data="proj_reg_menu",
        )
    ])

    existing = brief or {}
    missing_indices = [idx for idx, f in enumerate(BRIEF_FIELDS) if not existing.get(f)]
    if len(missing_indices) == len(BRIEF_FIELDS):
        brief_btn = InlineKeyboardButton(text=i18n.get_text("btn_brief_fill", lang), callback_data="brief_quiz_start")
    elif 0 < len(missing_indices) < len(BRIEF_FIELDS):
        next_step = missing_indices[0] + 1
        brief_btn = InlineKeyboardButton(
            text=i18n.get_text("btn_brief_resume", lang, next_step=next_step),
            callback_data="brief_quiz_start",
        )
    else:
        brief_btn = InlineKeyboardButton(text=i18n.get_text("btn_brief_update", lang), callback_data="brief_restart")
    rows.append([brief_btn])

    rows.append([
        InlineKeyboardButton(text=i18n.get_text("btn_brief_back_chat", lang), callback_data="brief_back_chat"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_register_picker_kb(lang: str, pinned_register: str | None = None) -> InlineKeyboardMarkup:
    """Компактное подменю выбора оптики папки (вынесено из основного хаба ради мобильной эргономики)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"{'✅ ' if not pinned_register else ''}🎯 Авто",
                callback_data="proj_reg:AUTO",
            ),
            InlineKeyboardButton(
                text=f"{'✅ ' if pinned_register == 'HARD_B2B' else ''}⚙️ Hard B2B",
                callback_data="proj_reg:HARD_B2B",
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"{'✅ ' if pinned_register == 'CREATOR_MEDIA' else ''}🎬 Медиа и Блог",
                callback_data="proj_reg:CREATOR_MEDIA",
            ),
            InlineKeyboardButton(
                text=f"{'✅ ' if pinned_register == 'PR_BRAND' else ''}🏛 PR и Бренд",
                callback_data="proj_reg:PR_BRAND",
            ),
        ],
        [
            InlineKeyboardButton(text=i18n.get_text("btn_back_to_folder", lang), callback_data="proj_hub"),
        ],
    ])


async def generate_cold_shower_diagnosis(
    answers: dict,
    history: list | None = None,
    pinned_register: str | None = None,
    all_user_model_texts: str = "",
    user_display_name: str = "",
    rotation_seed: int = 0,
) -> str:
    """
    Генерирует первичный «Продюсерский рентген проекта» (2–3 абзаца строго на «вы»)
    через LegoPromptBuilder.build_cold_shower_prompt с ротацией палитры в Python.
    """
    brief_dump = "\n".join(f"- {BRIEF_LABELS[f]}: {answers.get(f) or 'не указано'}" for f in BRIEF_FIELDS)
    prior_chat = ""
    if history:
        recent_lines = []
        for msg in history[-6:]:
            role = "КЛИЕНТ" if msg.get("role") == "user" else "АРИСТАРХ"
            txt = " ".join(p.get("text", "") for p in msg.get("parts", []) if p.get("text")).strip()
            if txt:
                recent_lines.append(f"{role}: {txt[:500]}")
        if recent_lines:
            prior_chat = "\n\nКОНТЕКСТ ПРЕДЫДУЩЕГО РАЗГОВОРА В ЧАТЕ (НЕ ПРОТИВОРЕЧЬ ТОМУ, О ЧЁМ УЖЕ ДОГОВОРИЛИСЬ):\n" + "\n".join(recent_lines)

    register = pinned_register or infer_register_from_text(brief_dump)
    system_prompt = LegoPromptBuilder.build_cold_shower_prompt(
        register=register,
        project_history=history,
        all_user_model_texts=all_user_model_texts,
        user_display_name=user_display_name,
        rotation_seed=rotation_seed,
    )
    user_prompt = f"ВВОДНЫЕ БРИФА КЛИЕНТА:\n{brief_dump}{prior_chat}\n\nВыдай первичный «Продюсерский рентген проекта»."
    diag = await llm_service.generate_standalone(system_prompt, user_prompt, temp=0.75)
    diag = await polish_reply(llm_service, diag, user_prompt)
    return humanize_punctuation(diag.replace("*", "").strip())


async def open_project_hub(message: types.Message, state: FSMContext, lang: str, user_id: int | None = None):
    """
    Всегда открывает компактный Хаб «📂 Проект / Бриф» (Вариант 1В) —
    даже при 0 кредитов и даже если бриф в текущей папке ещё не заполнен (0/5).
    """
    uid = user_id or message.chat.id
    user = await db_service.get_user(uid)
    if not user:
        user = await db_service.create_user(uid, getattr(message.from_user, "username", None), getattr(message.from_user, "full_name", None))
    active_proj = await db_service.get_or_create_active_project(uid)
    projects = await db_service.list_projects(uid)
    existing = await db_service.get_brief(uid) or {}
    rules = await db_service.get_editorial_rules(uid)
    max_proj = getattr(user, "max_projects", 2) or 2

    await db_service.log_event(uid, "PROJECT_HUB_OPENED", f"proj:{active_proj.id}")
    await state.set_state(BotStates.chatting)
    card_text = i18n.get_text(
        "project_folder_card",
        lang,
        slot_index=active_proj.slot_index,
        max_projects=max_proj,
        title=_clean_folder_title(active_proj.title, max_len=28),
        register_label=_register_label(lang, active_proj.pinned_register),
        rules_count=len(rules),
        brief_text=format_brief_for_ui(existing),
    )
    await message.answer(
        card_text,
        parse_mode="Markdown",
        reply_markup=build_project_hub_kb(lang, active_proj, projects, brief=existing, rules_count=len(rules)),
    )


async def start_brief_quiz(
    message: types.Message,
    state: FSMContext,
    lang: str,
    user_id: int | None = None,
    force_restart: bool = False,
):
    """Запускает или продолжает пошаговый бриф (1/5..5/5) с лёгкой 1-строчной клавиатурой под вопросом."""
    uid = user_id or message.chat.id
    user = await db_service.get_user(uid)
    if not user:
        user = await db_service.create_user(uid, getattr(message.from_user, "username", None), getattr(message.from_user, "full_name", None))
    active_proj = await db_service.get_or_create_active_project(uid)
    await db_service.log_event(uid, "BRIEF_STARTED", f"proj:{active_proj.id}")
    existing = await db_service.get_brief(uid) or {}
    missing_indices = [idx for idx, f in enumerate(BRIEF_FIELDS) if not existing.get(f)]

    # Продолжение с места остановки (1..4 из 5)
    if not force_restart and existing and 0 < len(missing_indices) < len(BRIEF_FIELDS):
        next_idx = missing_indices[0]
        filled_count = len(BRIEF_FIELDS) - len(missing_indices)
        await state.set_state(BRIEF_STEPS[next_idx])
        await state.update_data(brief_answers=dict(existing), brief_paused_step=None)
        await message.answer(i18n.get_text("brief_resumed_intro", lang, filled=filled_count), reply_markup=cancel_kb(lang))
        await message.answer(
            i18n.get_text(f"brief_q_{BRIEF_FIELDS[next_idx]}", lang),
            reply_markup=brief_question_inline_kb(lang, show_restart=True),
        )
        return

    # Если бриф уже 5/5 и не было force_restart — показываем хаб проекта
    if not force_restart and existing and len(missing_indices) == 0:
        await open_project_hub(message, state, lang, user_id=uid)
        return

    if force_restart:
        await db_service.save_brief(uid, {})
    await state.set_state(BRIEF_STEPS[0])
    await state.update_data(brief_answers={}, brief_paused_step=None)
    await message.answer(i18n.get_text("brief_intro", lang), reply_markup=cancel_kb(lang))
    await message.answer(
        i18n.get_text(f"brief_q_{BRIEF_FIELDS[0]}", lang),
        reply_markup=brief_question_inline_kb(lang, show_restart=False),
    )


@dp.message(Command("brief"))
async def cmd_brief(message: types.Message, state: FSMContext):
    await open_project_hub(message, state, i18n.user_lang(message.from_user), user_id=message.from_user.id)


@dp.message(F.text.in_(i18n.variants("btn_brief") | LEGACY_BRIEF_BTNS))
async def brief_button(message: types.Message, state: FSMContext):
    await open_project_hub(message, state, i18n.user_lang(message.from_user), user_id=message.from_user.id)


@dp.callback_query(F.data == "proj_hub")
async def proj_hub_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await open_project_hub(callback.message, state, i18n.user_lang(callback.from_user), user_id=callback.from_user.id)


@dp.callback_query(F.data.in_({"brief_start", "brief_quiz_start"}))
async def brief_start_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await start_brief_quiz(callback.message, state, i18n.user_lang(callback.from_user), user_id=callback.from_user.id)


@dp.callback_query(F.data == "brief_restart")
async def brief_restart_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await start_brief_quiz(
        callback.message,
        state,
        i18n.user_lang(callback.from_user),
        user_id=callback.from_user.id,
        force_restart=True,
    )


@dp.callback_query(F.data == "brief_back_chat")
async def brief_back_chat_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    lang = i18n.user_lang(callback.from_user)
    answers = (await state.get_data()).get("brief_answers", {})
    if answers:
        await db_service.save_brief(callback.from_user.id, answers)
    await state.set_state(BotStates.chatting)
    await callback.message.answer(i18n.get_text("menu_return", lang), reply_markup=main_kb(lang))


@dp.callback_query(F.data == "proj_reg_menu")
async def proj_reg_menu_callback(callback: types.CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    lang = i18n.user_lang(callback.from_user)
    proj = await db_service.get_or_create_active_project(uid)
    await callback.message.answer(
        i18n.get_text("register_menu_title", lang, title=_clean_folder_title(proj.title, max_len=28)),
        parse_mode="Markdown",
        reply_markup=build_register_picker_kb(lang, proj.pinned_register),
    )


@dp.callback_query(F.data.startswith("proj_switch:"))
async def proj_switch_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    uid = callback.from_user.id
    lang = i18n.user_lang(callback.from_user)
    cur_answers = (await state.get_data()).get("brief_answers", {})
    if cur_answers:
        await db_service.save_brief(uid, cur_answers)
    try:
        proj_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        return
    proj = await db_service.switch_active_project(uid, proj_id)
    if not proj:
        return
    await db_service.log_event(uid, "PROJECT_SWITCHED", f"proj:{proj.id}, slot:{proj.slot_index}")
    proj_history = await db_service.get_project_history(uid)
    existing = await db_service.get_brief(uid) or {}
    projects = await db_service.list_projects(uid)
    rules = await db_service.get_editorial_rules(uid)
    await state.update_data(history=proj_history, brief_answers=dict(existing), brief_paused_step=None)
    await state.set_state(BotStates.chatting)
    await callback.message.answer(
        i18n.get_text("project_switched", lang, slot_index=proj.slot_index, title=_clean_folder_title(proj.title, max_len=28))
        + f"\n\n{format_brief_for_ui(existing)}",
        parse_mode="Markdown",
        reply_markup=build_project_hub_kb(lang, proj, projects, brief=existing, rules_count=len(rules)),
    )


@dp.callback_query(F.data == "proj_new")
async def proj_new_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    uid = callback.from_user.id
    lang = i18n.user_lang(callback.from_user)
    cur_answers = (await state.get_data()).get("brief_answers", {})
    if cur_answers:
        await db_service.save_brief(uid, cur_answers)
    new_proj, max_cnt = await db_service.create_new_project(uid)
    if new_proj is None:
        cur_cnt = max_cnt
        await db_service.log_event(uid, "PROJECT_LIMIT_REACHED", f"count:{cur_cnt}, max:{max_cnt}")
        await state.set_state(BotStates.chatting)
        await callback.message.answer(
            i18n.get_text("project_limit_reached", lang, current=cur_cnt, max_projects=max_cnt),
            parse_mode="Markdown",
            reply_markup=paywall_kb(lang),
        )
        return
    await db_service.log_event(uid, "PROJECT_CREATED", f"proj:{new_proj.id}, slot:{new_proj.slot_index}, max:{max_cnt}")
    projects = await db_service.list_projects(uid)
    await state.update_data(history=[], brief_answers={}, brief_paused_step=None)
    await state.set_state(BotStates.chatting)
    await callback.message.answer(
        i18n.get_text("project_created", lang, slot_index=new_proj.slot_index, max_projects=max_cnt),
        parse_mode="Markdown",
        reply_markup=build_project_hub_kb(lang, new_proj, projects, brief={}, rules_count=0),
    )


@dp.callback_query(F.data.startswith("proj_reg:"))
async def proj_register_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    uid = callback.from_user.id
    lang = i18n.user_lang(callback.from_user)
    reg_raw = callback.data.split(":", 1)[1].strip()
    pinned = None if reg_raw == "AUTO" else reg_raw
    proj = await db_service.set_project_pinned_register(uid, pinned)
    projects = await db_service.list_projects(uid)
    existing = await db_service.get_brief(uid) or {}
    rules = await db_service.get_editorial_rules(uid)
    await db_service.log_event(uid, "PROJECT_REGISTER_PINNED", f"proj:{proj.id if proj else 0}, reg:{reg_raw}")
    reg_label = _register_label(lang, pinned)
    await callback.message.answer(
        i18n.get_text("project_register_updated", lang, register_label=reg_label),
        parse_mode="Markdown",
        reply_markup=build_project_hub_kb(lang, proj, projects, brief=existing, rules_count=len(rules)),
    )


@dp.callback_query(F.data == "proj_rules")
async def proj_rules_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    uid = callback.from_user.id
    lang = i18n.user_lang(callback.from_user)
    proj = await db_service.get_or_create_active_project(uid)
    rules = await db_service.get_editorial_rules(uid)
    await state.set_state(BotStates.waiting_for_editorial_rule)
    rules_text = "\n".join(f"{idx + 1}. {r}" for idx, r in enumerate(rules)) if rules else "(пока не заданы)"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=i18n.get_text("btn_clear_editorial_policy", lang), callback_data="proj_rules_clear"),
            InlineKeyboardButton(text=i18n.get_text("btn_back_to_folder", lang), callback_data="proj_hub"),
        ],
    ])
    await callback.message.answer(
        i18n.get_text("editorial_policy_card", lang, title=_clean_folder_title(proj.title, max_len=28), rules_text=rules_text),
        parse_mode="Markdown",
        reply_markup=kb,
    )


@dp.callback_query(F.data == "proj_rules_clear")
async def proj_rules_clear_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    uid = callback.from_user.id
    lang = i18n.user_lang(callback.from_user)
    await db_service.save_editorial_rules(uid, [])
    await db_service.log_event(uid, "EDITORIAL_RULES_CLEARED")
    await state.set_state(BotStates.chatting)
    await callback.message.answer(i18n.get_text("editorial_rules_cleared", lang), parse_mode="Markdown", reply_markup=main_kb(lang))


@dp.message(BotStates.waiting_for_editorial_rule, F.text)
async def editorial_rule_input_handler(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    lang = i18n.user_lang(message.from_user)
    if message.text in i18n.variants("btn_main_menu") or message.text in i18n.variants("btn_cancel"):
        await state.set_state(BotStates.chatting)
        await message.answer(i18n.get_text("menu_return", lang), reply_markup=main_kb(lang))
        return
    rule_raw = message.text.strip()
    proj = await db_service.get_or_create_active_project(uid)
    existing_rules = await db_service.get_editorial_rules(uid)
    new_rules = assistant_service.extract_editorial_rules_deterministic(rule_raw, existing_rules)
    if new_rules == existing_rules and rule_raw:
        new_rules = list(existing_rules) + [rule_raw[:220]]
    await db_service.save_editorial_rules(uid, new_rules[-12:])
    await db_service.log_event(uid, "EDITORIAL_RULE_ADDED", rule_raw[:300])
    await state.set_state(BotStates.chatting)
    await message.answer(
        i18n.get_text("editorial_rule_saved", lang, rule=rule_raw[:220], title=_clean_folder_title(proj.title, max_len=28)),
        parse_mode="Markdown",
        reply_markup=main_kb(lang),
    )


async def _process_brief_text_input(message: types.Message, state: FSMContext, text_input: str):
    uid = message.from_user.id
    lang = i18n.user_lang(message.from_user)
    if text_input in i18n.variants("btn_main_menu") or text_input in i18n.variants("btn_cancel"):
        answers = (await state.get_data()).get("brief_answers", {})
        if answers:
            await db_service.save_brief(uid, answers)
        await state.set_state(BotStates.chatting)
        await message.answer(i18n.get_text("brief_cancelled", lang), reply_markup=main_kb(lang))
        return

    step = [s.state for s in BRIEF_STEPS].index(await state.get_state())
    current_field = BRIEF_FIELDS[step]
    answers = (await state.get_data()).get("brief_answers", {})
    # Если юзер перебил анкету явным отказом, встречным вопросом со знаком '?' или готовым постом (>700 знаков):
    if _is_brief_interruption(text_input, current_field=current_field):
        if answers:
            await db_service.save_brief(uid, answers)
            await db_service.log_event(uid, "BRIEF_PARTIAL_SAVED", f"fields:{list(answers.keys())}")
        await state.update_data(brief_paused_step=step)
        await state.set_state(BotStates.chatting)
        await message.answer(i18n.get_text("brief_paused_for_chat", lang), reply_markup=main_kb(lang), parse_mode="Markdown")
        await _dispatch_chat_text(message, state, text_input)
        return

    cleaned_answer = "" if text_input.strip().lower() in BRIEF_SKIP_WORDS else text_input.strip()
    if current_field == "channel" and cleaned_answer:
        tg_handle = extract_tg_username(cleaned_answer)
        if tg_handle:
            snap = await fetch_public_tg_channel_snapshot(tg_handle, http_session=llm_service.http_session)
            if snap:
                if snap.get("voice_samples"):
                    await db_service.save_voice_samples(uid, snap["voice_samples"])
                cleaned_answer = f"{cleaned_answer}\n\n[АВТОСКАН ОТКРЫТОГО TELEGRAM-КАНАЛА]\n{snap['formatted_summary']}"
                ads_cnt = snap.get("ads_filtered_count", 0)
                ads_note = f" (отфильтровано рекламных: {ads_cnt})" if ads_cnt else ""
                await db_service.log_event(
                    uid,
                    "TG_CHANNEL_SCANNED",
                    f"@{snap['username']}, posts:{snap['posts_count']}, ads:{ads_cnt}",
                )
                await message.answer(
                    i18n.get_text(
                        "channel_scanned_ok",
                        lang,
                        username=snap["username"],
                        title=snap["title"],
                        posts_count=snap["posts_count"],
                        ads_note=ads_note,
                    ),
                    parse_mode="Markdown",
                )

    answers[current_field] = cleaned_answer
    active_proj = await db_service.get_or_create_active_project(uid)
    if step + 1 < len(BRIEF_STEPS):
        await state.update_data(brief_answers=answers)
        await db_service.save_brief(uid, answers)
        await state.set_state(BRIEF_STEPS[step + 1])
        await message.answer(
            i18n.get_text(f"brief_q_{BRIEF_FIELDS[step + 1]}", lang),
            reply_markup=brief_question_inline_kb(lang, show_restart=True),
        )
        return

    await db_service.save_brief(uid, answers)
    await db_service.log_event(uid, "BRIEF_SAVED", f"proj:{active_proj.id}")
    await state.update_data(brief_answers={}, brief_paused_step=None)
    await state.set_state(BotStates.chatting)
    await message.answer(i18n.get_text("brief_saved", lang), reply_markup=main_kb(lang))

    # Проверка лимита ТОЛЬКО перед запуском 2-го платного LLM-рентгена (само редактирование брифа в SQLite всегда бесплатно!)
    data = await state.get_data()
    user = await db_service.get_user(uid)
    is_unlim = await db_service.has_unlimited(uid)
    cold_shower_already_used = bool(data.get("cold_shower_free_used"))
    if cold_shower_already_used and user and not is_unlim and user.credits < 1:
        await message.answer(i18n.get_text("no_credits", lang), parse_mode="Markdown", reply_markup=paywall_kb(lang))
        return

    # Автоматический «Продюсерский рентген проекта» сразу после 5-го ответа брифа (0 кредитов за первый диагноз!)
    wait_msg = await message.answer(i18n.get_text("brief_cold_shower_wait", lang), parse_mode="Markdown")
    try:
        history = await db_service.get_project_history(uid) or data.get("history", [])
        all_user_model_texts = await db_service.get_all_user_model_texts(uid)
        display_name = _clean_user_display_name(message.from_user)
        diagnosis = await generate_cold_shower_diagnosis(
            answers,
            history=history,
            pinned_register=active_proj.pinned_register,
            all_user_model_texts=all_user_model_texts,
            user_display_name=display_name,
            rotation_seed=uid,
        )
        remaining_credits = user.credits if user else 2
        if cold_shower_already_used and user and not is_unlim and user.credits > 0:
            remaining_credits = await db_service.update_balance(uid, -1)
        await db_service.log_event(uid, "BRIEF_COLD_SHOWER", diagnosis[:1000])
        new_history = history + [
            {"role": "user", "parts": [{"text": f"[БРИФ ЗАПОЛНЕН]\n{format_brief(answers)}"}]},
            {"role": "model", "parts": [{"text": diagnosis}]},
        ]
        await state.update_data(history=new_history[-40:], cold_shower_free_used=True)
        await db_service.save_project_history(uid, new_history[-40:])
        try:
            await wait_msg.delete()
        except Exception:
            pass
        await send_smart_response(message, f"🔎 ПРОДЮСЕРСКИЙ РЕНТГЕН ПРОЕКТА:\n\n{diagnosis}")
        await message.answer(
            i18n.get_text("brief_cold_shower_footer", lang, credits=remaining_credits),
            parse_mode="Markdown",
            reply_markup=paywall_kb(lang),
        )
    except Exception as e:
        logger.error(f"Cold shower error: {e}")
        try:
            await wait_msg.delete()
        except Exception:
            pass


@dp.message(StateFilter(*BRIEF_STEPS), F.text)
async def brief_answer(message: types.Message, state: FSMContext):
    await _process_brief_text_input(message, state, message.text or "")


@dp.message(StateFilter(*BRIEF_STEPS), F.voice | F.audio)
async def brief_voice_answer(message: types.Message, state: FSMContext):
    voice_obj = message.voice or message.audio
    if not voice_obj:
        return
    file_info = await bot.get_file(voice_obj.file_id)
    downloaded = await bot.download_file(file_info.file_path)
    audio_bytes = downloaded.read()
    mime = getattr(voice_obj, "mime_type", None) or "audio/ogg"
    await handle_voice_bytes(message, state, audio_bytes, mime_type=mime)

# --- РУБРИКА «РАЗБОР ПОДПИСЧИКА»: только администратор ---
# /razbor <вопрос> → Аристарх пишет разбор → превью с кнопками → публикация в канал.
# Черновики живут в памяти процесса: после перезапуска бота превью нужно запросить заново.
razbor_drafts: dict[str, dict] = {}


def razbor_kb(draft_id: str, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=i18n.get_text("btn_razbor_publish", lang), callback_data=f"rz_pub:{draft_id}"),
        InlineKeyboardButton(text=i18n.get_text("btn_razbor_redo", lang), callback_data=f"rz_redo:{draft_id}"),
        InlineKeyboardButton(text=i18n.get_text("btn_razbor_cancel", lang), callback_data=f"rz_del:{draft_id}"),
    ]])


async def make_razbor(question: str) -> str:
    # Ссылка на бота только при CHANNEL_BOT_CTA; иначе разбор зовёт писать вопросы в комментарии
    cta_handle = f"@{(await bot.get_me()).username}" if BOT_CTA_ENABLED else None
    core_beliefs = evolution_engine.get_current_beliefs()
    post_html, _ = await generate_razbor(llm_service, rag_service, core_beliefs, question, cta_handle)
    return post_html


@dp.message(Command("razbor"), F.from_user.id == config.ADMIN_ID)
async def cmd_razbor(message: types.Message, command: CommandObject):
    lang = i18n.user_lang(message.from_user)
    question = (command.args or "").strip()
    if not question:
        await message.answer(i18n.get_text("razbor_usage", lang))
        return
    if not CHANNEL_ID:
        await message.answer(i18n.get_text("razbor_no_channel", lang))
        return
    wait_msg = await message.answer(i18n.get_text("razbor_wait", lang))
    try:
        post_html = await make_razbor(question)
    except Exception as e:
        logger.error(f"Razbor error: {e}")
        await wait_msg.edit_text(i18n.get_text("post_error", lang, error=e))
        return
    draft_id = uuid.uuid4().hex[:8]
    razbor_drafts[draft_id] = {"question": question, "html": post_html}
    await wait_msg.delete()
    await message.answer(
        post_html, parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        reply_markup=razbor_kb(draft_id, lang),
    )


@dp.callback_query(F.data.startswith("rz_"), F.from_user.id == config.ADMIN_ID)
async def razbor_action(callback: types.CallbackQuery):
    lang = i18n.user_lang(callback.from_user)
    action, _, draft_id = callback.data.partition(":")
    if draft_id not in razbor_drafts:
        await callback.answer(i18n.get_text("razbor_expired", lang), show_alert=True)
        return

    if action == "rz_pub":
        draft = razbor_drafts.pop(draft_id)  # сразу забираем: двойное нажатие не опубликует дважды
        try:
            await bot.send_message(
                CHANNEL_ID, draft["html"], parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except Exception as e:
            razbor_drafts[draft_id] = draft
            await callback.answer(i18n.get_text("post_error", lang, error=e), show_alert=True)
            return
        await db_service.log_event(callback.from_user.id, "RAZBOR_PUBLISHED", f"channel:{CHANNEL_ID}")
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.reply(i18n.get_text("razbor_published", lang, channel=CHANNEL_ID))
        await callback.answer()
    elif action == "rz_redo":
        await callback.answer(i18n.get_text("razbor_wait", lang))
        try:
            post_html = await make_razbor(razbor_drafts[draft_id]["question"])
        except Exception as e:
            logger.error(f"Razbor redo error: {e}")
            await callback.message.reply(i18n.get_text("post_error", lang, error=e))
            return
        razbor_drafts[draft_id]["html"] = post_html
        await callback.message.edit_text(
            post_html, parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            reply_markup=razbor_kb(draft_id, lang),
        )
    else:
        razbor_drafts.pop(draft_id, None)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.reply(i18n.get_text("razbor_cancelled", lang))
        await callback.answer()

# --- HANDLER: ПЕРЕКУР (AFFECTIVE MEMORY RESET — ИСТОРИЯ НЕ СТИРАЕТСЯ) ---
@dp.message(F.text.in_(i18n.variants("btn_rest")))
async def cmd_rest(message: types.Message, state: FSMContext):
    await db_service.log_event(message.from_user.id, "REST_BUTTON_CLICKED")
    await db_service.reset_fatigue(message.from_user.id)
    
    logger.info(f"🚬 [СБРОС] Юзер {message.from_user.id} нажал 'Перекур'. Усталость обнулена, история диалога сохранена.")
    
    await message.answer(i18n.get_text("rest_btn_clicked", i18n.user_lang(message.from_user)), parse_mode="Markdown")

# --- HANDLERS: BALANCE, RUBLE PAYWALL, B2B QUALIFIER & STARS ---
RUB_TARIFF_NAMES = {
    "1490": "Пробный монтаж (15 разборов / 7 дн.)",
    "4900": "«Эксперт» (60 разборов / 30 дн.)",
    "9900": "«Продюсер» (200 разборов / 30 дн.)",
}


@dp.message(F.text.in_(i18n.variants("btn_balance")))
async def show_balance(message: types.Message):
    await db_service.log_event(message.from_user.id, "CHECK_BALANCE")
    lang = i18n.user_lang(message.from_user)

    user = await db_service.get_user(message.from_user.id)
    if not user:
        user = await db_service.create_user(message.from_user.id, message.from_user.username, message.from_user.full_name)

    is_unlim = await db_service.has_unlimited(message.from_user.id)

    expired = await db_service.reset_balance_if_expired(user.id)
    if expired:
        user.credits = 0
        user.expires_at = None

    if is_unlim:
        unlim_str = i18n.get_text("balance_unlim_forever", lang) if not user.unlimited_until else user.unlimited_until.strftime('%Y-%m-%d %H:%M')
        text = i18n.get_text("balance_info_unlim", lang, unlim_str=unlim_str, credits=user.credits)
    else:
        expiry_info = i18n.get_text("subscription_until", lang, date=user.expires_at.strftime('%Y-%m-%d %H:%M')) if user.expires_at else ""
        text = i18n.get_text("balance_info", lang, credits=user.credits, expiry_info=expiry_info)

    await message.answer(text, reply_markup=paywall_kb(lang), parse_mode="Markdown")


@dp.callback_query(F.data == "show_stars_tariffs")
async def show_stars_tariffs_cb(callback: types.CallbackQuery):
    lang = i18n.user_lang(callback.from_user)
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=stars_kb(lang))
    except Exception:
        await callback.message.answer("⭐️ Тарифы в Telegram Stars:", reply_markup=stars_kb(lang))


@dp.callback_query(F.data == "show_rub_tariffs")
async def show_rub_tariffs_cb(callback: types.CallbackQuery):
    lang = i18n.user_lang(callback.from_user)
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=paywall_kb(lang))
    except Exception:
        await callback.message.answer("💳 Тарифы в рублях:", reply_markup=paywall_kb(lang))


@dp.callback_query(F.data == "enter_promo")
async def enter_promo_handler(callback: types.CallbackQuery, state: FSMContext):
    await db_service.log_event(callback.from_user.id, "CLICK_PROMO_BTN")
    await state.set_state(BotStates.waiting_for_promo)
    lang = i18n.user_lang(callback.from_user)
    await callback.message.answer(i18n.get_text("promo_enter", lang), reply_markup=cancel_kb(lang))
    await callback.answer()


@dp.message(BotStates.waiting_for_promo, F.text)
async def process_promo(message: types.Message, state: FSMContext):
    lang = i18n.user_lang(message.from_user)
    if message.text in i18n.variants("btn_main_menu"):
        await db_service.log_event(message.from_user.id, "CANCEL_PROMO_INPUT")
        await state.set_state(BotStates.chatting)
        await message.answer(i18n.get_text("menu_return", lang), reply_markup=main_kb(lang))
        return

    if config.PROMO_CODE and message.text.strip().lower() == config.PROMO_CODE.lower():
        await db_service.log_event(message.from_user.id, "ENTER_PROMO_SUCCESS", message.text)
        success = await db_service.activate_promo(message.from_user.id, config.PROMO_HOURS)
        await state.set_state(BotStates.chatting)
        if success:
            await message.answer(i18n.get_text("promo_success", lang), parse_mode="Markdown", reply_markup=main_kb(lang))
        else:
            await message.answer(i18n.get_text("promo_error", lang), reply_markup=main_kb(lang))
    else:
        await db_service.log_event(message.from_user.id, "ENTER_PROMO_FAIL", message.text)
        await state.set_state(BotStates.chatting)
        await message.answer(i18n.get_text("promo_invalid", lang), reply_markup=main_kb(lang))


@dp.callback_query(F.data.startswith("buy_rub_"))
async def buy_rub_handler(callback: types.CallbackQuery):
    _, _, credits_amount, days, price_rub = callback.data.split("_")
    lang = i18n.user_lang(callback.from_user)
    tariff_name = RUB_TARIFF_NAMES.get(price_rub, f"{credits_amount} разборов / {days} дн.")
    await db_service.log_event(
        callback.from_user.id,
        "CLICK_BUY_RUB",
        f"credits:{credits_amount}, days:{days}, rub:{price_rub}",
    )

    if config.YOOKASSA_PROVIDER_TOKEN:
        prices = [LabeledPrice(label=tariff_name, amount=int(price_rub) * 100)]
        await bot.send_invoice(
            chat_id=callback.from_user.id,
            title=i18n.get_text("invoice_title", lang),
            description=f"{tariff_name} в продюсерском кабинете Аристарха Градова",
            payload=callback.data,
            provider_token=config.YOOKASSA_PROVIDER_TOKEN,
            currency="RUB",
            prices=prices,
            start_parameter="topup_rub",
        )
        await callback.answer()
        return

    # Фоллбэк: если YOOKASSA_PROVIDER_TOKEN ещё не прописан в .env — мгновенная заявка админу с 1-клик выдачей
    await callback.answer()
    if config.ADMIN_ID:
        try:
            brief = await db_service.get_brief(callback.from_user.id)
            brief_str = format_brief(brief) if brief else "(бриф не заполнен)"
            admin_text = (
                f"💳 <b>ЗАЯВКА НА РУБЛЁВЫЙ ТАРИФ ({price_rub} ₽)</b>\n"
                f"Клиент: {html.escape(callback.from_user.full_name or '')} "
                f"(@{html.escape(callback.from_user.username or 'no_username')}, ID <code>{callback.from_user.id}</code>)\n"
                f"Пакет: <b>{html.escape(tariff_name)}</b>\n\n"
                f"<blockquote>{html.escape(brief_str[:1500])}</blockquote>"
            )
            admin_kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text=f"✅ Выдать {credits_amount} разб. / {days} дн.",
                    callback_data=f"adm_grant:{callback.from_user.id}:{credits_amount}:{days}:{price_rub}",
                )
            ]])
            await bot.send_message(config.ADMIN_ID, admin_text, parse_mode="HTML", reply_markup=admin_kb)
        except Exception as e:
            logger.error(f"Не удалось отправить рублёвую заявку админу: {e}")

    await callback.message.answer(
        i18n.get_text("rub_order_sent", lang, tariff_name=tariff_name, price_rub=price_rub),
        parse_mode="Markdown",
    )


@dp.callback_query(F.data.startswith("adm_grant:"), F.from_user.id == config.ADMIN_ID)
async def admin_grant_package(callback: types.CallbackQuery):
    _, target_uid_s, credits_s, days_s, price_rub = callback.data.split(":")
    target_uid, credits_to_add, add_days = int(target_uid_s), int(credits_s), int(days_s)
    new_bal = await db_service.update_balance(target_uid, credits_to_add, add_days)
    await db_service.log_event(target_uid, "ADMIN_GRANT_RUB", f"rub:{price_rub}, credits:{credits_to_add}, days:{add_days}")
    await callback.answer(f"Пакет выдан! Баланс юзера: {new_bal}", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(
        f"✅ Пакет (<b>+{credits_to_add} разборов на {add_days} дн.</b>) активирован для ID <code>{target_uid}</code>. "
        f"Текущий баланс: <b>{new_bal}</b>.",
        parse_mode="HTML",
    )
    try:
        await bot.send_message(
            target_uid,
            i18n.get_text("payment_success", "ru", new_bal=new_bal),
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить пользователя {target_uid} о выдаче пакета: {e}")


# --- B2B-КВАЛИФИКАТОР: ИИ-ДВОЙНИК ПЕРВОГО ЛИЦА ПОД КЛЮЧ ---
@dp.callback_query(F.data == "b2b_start")
async def b2b_start_cb(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    lang = i18n.user_lang(callback.from_user)
    await db_service.log_event(callback.from_user.id, "B2B_QUALIFIER_STARTED")
    await state.set_state(BotStates.b2b_scale)
    await callback.message.answer(i18n.get_text("b2b_intro", lang), parse_mode="Markdown", reply_markup=cancel_kb(lang))
    await callback.message.answer(i18n.get_text("b2b_q_scale", lang))


@dp.message(BotStates.b2b_scale, F.text)
async def b2b_scale_handler(message: types.Message, state: FSMContext):
    lang = i18n.user_lang(message.from_user)
    if message.text in i18n.variants("btn_main_menu") or message.text in i18n.variants("btn_cancel"):
        await state.set_state(BotStates.chatting)
        await message.answer(i18n.get_text("menu_return", lang), reply_markup=main_kb(lang))
        return
    await state.update_data(b2b_scale_answer=message.text.strip())
    await state.set_state(BotStates.b2b_contact)
    await message.answer(i18n.get_text("b2b_q_contact", lang))


@dp.message(BotStates.b2b_contact, F.text)
async def b2b_contact_handler(message: types.Message, state: FSMContext):
    lang = i18n.user_lang(message.from_user)
    if message.text in i18n.variants("btn_main_menu") or message.text in i18n.variants("btn_cancel"):
        await state.set_state(BotStates.chatting)
        await message.answer(i18n.get_text("menu_return", lang), reply_markup=main_kb(lang))
        return
    data = await state.get_data()
    scale_ans = data.get("b2b_scale_answer", "")
    contact_ans = message.text.strip()
    await db_service.log_event(message.from_user.id, "B2B_LEAD_SUBMITTED", f"{scale_ans[:300]} | {contact_ans[:200]}")

    if config.ADMIN_ID:
        try:
            brief = await db_service.get_brief(message.from_user.id)
            brief_str = format_brief(brief) if brief else "(бриф не заполнен)"
            lead_card = (
                f"🧬 <b>НОВЫЙ B2B-ЛИД: ИИ-ДВОЙНИК ПЕРВОГО ЛИЦА</b>\n"
                f"От: {html.escape(message.from_user.full_name or '')} "
                f"(@{html.escape(message.from_user.username or 'no_username')}, ID <code>{message.from_user.id}</code>)\n\n"
                f"<b>1. Масштаб бизнеса и задача:</b>\n{html.escape(scale_ans[:1500])}\n\n"
                f"<b>2. Прямой контакт:</b>\n{html.escape(contact_ans[:500])}\n\n"
                f"<blockquote>{html.escape(brief_str[:1500])}</blockquote>"
            )
            await bot.send_message(config.ADMIN_ID, lead_card, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Не удалось отправить B2B-лид админу: {e}")

    await state.set_state(BotStates.chatting)
    await message.answer(i18n.get_text("b2b_accepted", lang), parse_mode="Markdown", reply_markup=main_kb(lang))


@dp.callback_query(F.data.startswith("buy_") & ~F.data.startswith("buy_rub_"))
async def buy_stars(callback: types.CallbackQuery):
    _, credits_amount, days, stars = callback.data.split("_")
    await db_service.log_event(callback.from_user.id, "CLICK_BUY_STARS", f"credits:{credits_amount}, days:{days}, stars:{stars}")

    lang = i18n.user_lang(callback.from_user)
    prices = [LabeledPrice(label=i18n.get_text("invoice_label", lang, credits=credits_amount), amount=int(stars))]

    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=i18n.get_text("invoice_title", lang),
        description=i18n.get_text(
            "invoice_desc", lang, credits=credits_amount,
            period=i18n.get_text("invoice_period_days", lang, days=days) if int(days) > 0 else i18n.get_text("invoice_period_forever", lang),
        ),
        payload=callback.data,
        provider_token="",
        currency="XTR",
        prices=prices,
        start_parameter="topup",
    )
    await callback.answer()


@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@dp.message(F.successful_payment)
async def successful_payment(message: types.Message):
    pmt = message.successful_payment
    parts = pmt.invoice_payload.split("_")
    if parts[0] == "buy" and parts[1] == "rub":
        _, _, credits_to_add, add_days, amount_paid = parts
    else:
        _, credits_to_add, add_days, amount_paid = parts
    credits_to_add, add_days, amount_paid = int(credits_to_add), int(add_days), int(amount_paid)
    charge_id = pmt.telegram_payment_charge_id

    if await db_service.check_tx_exists(charge_id):
        return

    await db_service.log_event(
        message.from_user.id,
        "PAYMENT_SUCCESS",
        f"currency:{pmt.currency}, amount:{amount_paid}, credits:{credits_to_add}, days:{add_days}",
    )
    new_bal = await db_service.update_balance(message.from_user.id, credits_to_add, add_days)
    await db_service.add_stars_transaction(message.from_user.id, amount_paid, credits_to_add, charge_id)
    await message.answer(i18n.get_text("payment_success", i18n.user_lang(message.from_user), new_bal=new_bal))

# --- HANDLERS: SETTINGS & RESET CONTEXT ---
@dp.message(F.text.in_(i18n.variants("btn_settings")))
async def settings(message: types.Message):
    await db_service.log_event(message.from_user.id, "OPEN_SETTINGS")
    
    user = await db_service.get_user(message.from_user.id)
    if not user:
        user = await db_service.create_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
        
    lang = i18n.user_lang(message.from_user)
    await message.answer(i18n.get_text("settings_title", lang), reply_markup=settings_kb(user.temperature, lang))

@dp.callback_query(F.data == "reset_context")
async def reset_context_handler(callback: types.CallbackQuery, state: FSMContext):
    await db_service.log_event(callback.from_user.id, "RESET_CONTEXT")
    await state.update_data(history=[], interaction_count=0)
    await db_service.save_project_history(callback.from_user.id, [])
    await callback.answer(i18n.get_text("memory_cleared", i18n.user_lang(callback.from_user)), show_alert=True)

@dp.callback_query(F.data.startswith("temp_"))
async def set_temp(callback: types.CallbackQuery):
    t = float(callback.data.split("_")[1])
    user_id = callback.from_user.id
    
    await db_service.log_event(user_id, "CHANGE_TEMP", str(t))
    await db_service.update_temp(user_id, t)
    
    lang = i18n.user_lang(callback.from_user)
    try:
        await callback.message.edit_text(i18n.get_text("settings_title", lang), reply_markup=settings_kb(t, lang))
    except TelegramBadRequest:
        pass
        
    await callback.answer(i18n.get_text("temp_applied", lang))

# --- [NEW] ЗАЩИТА ОТ ПУЛЕМЕТА ВО ВРЕМЯ ГЕНЕРАЦИИ ---
@dp.message(BotStates.generating)
async def generating_handler(message: types.Message):
    await db_service.log_event(message.from_user.id, "SPAM_BLOCKED")
    await message.answer(i18n.get_text("wait_analyzing", i18n.user_lang(message.from_user)))


async def _dispatch_chat_text(
    message: types.Message,
    state: FSMContext,
    user_query: str,
    image_data: dict | None = None,
):
    """Отправляет текстовый или расшифрованный голосовой запрос в debounce-буфер либо в режим длинного документа."""
    lang = i18n.user_lang(message.from_user)
    user_id = message.from_user.id
    pending_short = user_message_buffers.pop(user_id, "")
    if pending_short:
        user_query = f"{pending_short}\n{user_query}".strip()
    if user_id in debounce_tasks and not debounce_tasks[user_id].done():
        debounce_tasks[user_id].cancel()

    if len(user_query) > config.LONG_TEXT_THRESHOLD:
        await db_service.log_event(user_id, "LONG_INPUT_TRIGGERED", f"chars:{len(user_query)}")
        await state.update_data(text_buffer=user_query, img_buffer=image_data)
        await state.set_state(BotStates.waiting_for_long_input)
        await message.answer(i18n.get_text("long_input_mode", lang), reply_markup=long_input_kb(lang), parse_mode="Markdown")
        if user_id in long_input_tasks and not long_input_tasks[user_id].done():
            long_input_tasks[user_id].cancel()
        long_input_tasks[user_id] = asyncio.create_task(trigger_long_input_processing(message, state, user_id))
        return

    current_text = user_message_buffers.get(user_id, "")
    if current_text:
        user_message_buffers[user_id] = f"{current_text}\n{user_query}".strip()
    else:
        user_message_buffers[user_id] = user_query.strip()

    if image_data:
        user_image_buffers[user_id] = image_data

    debounce_tasks[user_id] = asyncio.create_task(trigger_processing(message, state, user_id))


async def handle_voice_bytes(
    message: types.Message,
    state: FSMContext,
    audio_bytes: bytes,
    mime_type: str = "audio/ogg",
) -> str:
    """
    Расшифровывает голосовое сообщение (.ogg / .mp3) через gemini-2.5-flash (transcribe_voice_ogg).
    Если запись пустая или короче 2 осмысленных слов — вежливо просит повторить (0 кредитов).
    """
    uid = message.from_user.id
    lang = i18n.user_lang(message.from_user)
    await db_service.log_event(uid, "INCOMING_REQUEST_VOICE", f"bytes:{len(audio_bytes)}")
    wait_msg = await message.answer(i18n.get_text("wait_transcribing_voice", lang))
    transcript = await llm_service.transcribe_voice_ogg(audio_bytes, mime_type=mime_type)
    try:
        await wait_msg.delete()
    except Exception:
        pass

    if not transcript:
        await db_service.log_event(uid, "VOICE_TRANSCRIPTION_EMPTY")
        await message.answer(i18n.get_text("voice_empty_retry", lang))
        return ""

    await db_service.log_event(uid, "VOICE_TRANSCRIBED", transcript[:500])
    cur_state = await state.get_state()
    if cur_state in [s.state for s in BRIEF_STEPS]:
        await _process_brief_text_input(message, state, transcript)
    else:
        await _dispatch_chat_text(message, state, transcript)
    return transcript


# --- HANDLERS: CHAT LOGIC (TEXT, PHOTO, DOCS, VOICE, AUDIO) ---
@dp.message(BotStates.chatting, F.text | F.photo | F.document | F.voice | F.audio)
async def chat_handler(message: types.Message, state: FSMContext):
    lang = i18n.user_lang(message.from_user)
    if message.text in i18n.variants("btn_main_menu"):
        await db_service.log_event(message.from_user.id, "RETURN_TO_MAIN_MENU")
        await cmd_start(message, state)
        return
    if message.text in MENU_BUTTONS:
        return

    if message.voice or message.audio:
        voice_obj = message.voice or message.audio
        file_info = await bot.get_file(voice_obj.file_id)
        downloaded = await bot.download_file(file_info.file_path)
        audio_bytes = downloaded.read()
        mime = getattr(voice_obj, "mime_type", None) or "audio/ogg"
        await handle_voice_bytes(message, state, audio_bytes, mime_type=mime)
        return

    # ╔══════════════════════════════════════════════════════════════╗
    # ║  ПУБЛИКАЦИЯ В КАНАЛ ВРУЧНУЮ: только администратор (ADMIN_ID)
    # ╚══════════════════════════════════════════════════════════════╝
    if (message.text and message.text.startswith("[ПУБЛИКАЦИЯ В КАНАЛ]")
            and message.from_user.id == config.ADMIN_ID and CHANNEL_ID):
        await db_service.log_event(message.from_user.id, "CHANNEL_POST_REQUEST")
        wait_msg = await message.answer(i18n.get_text("wait_writing_post", lang))
        
        news_text = message.text.replace("[ПУБЛИКАЦИЯ В КАНАЛ]", "").strip()
        url = extract_url(news_text)
        news = {
            "content": news_text,
            "url": url,
            "original_text": news_text.replace(url, "").strip() if url else news_text,
        }

        try:
            core_beliefs = evolution_engine.get_current_beliefs()
            channel_post, post_meta = await generate_post(llm_service, rag_service, core_beliefs, news, [])

            await bot.send_message(
                CHANNEL_ID,
                channel_post,
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )

            await wait_msg.edit_text(i18n.get_text("post_published", lang, channel=CHANNEL_ID, fmt=post_meta['format']))
            await db_service.log_event(message.from_user.id, "CHANNEL_POST_PUBLISHED", f"channel:{CHANNEL_ID}, chars:{len(channel_post)}, format:{post_meta['format']}")
        except Exception as e:
            logger.error(f"Channel post error: {e}")
            await db_service.log_event(message.from_user.id, "CHANNEL_POST_ERROR", str(e))
            await wait_msg.edit_text(i18n.get_text("post_error", lang, error=e))
        return

    event_type = "INCOMING_REQUEST_TEXT"
    if message.photo: event_type = "INCOMING_REQUEST_PHOTO"
    if message.document: event_type = "INCOMING_REQUEST_DOC"
    await db_service.log_event(message.from_user.id, event_type)

    user_query = message.text or message.caption or "Проанализируй этот файл."
    image_data = None
    file_context = ""

    if message.photo:
        wait_msg = await message.answer(i18n.get_text("wait_looking_pic", lang))
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        downloaded_file = await bot.download_file(file_info.file_path)
        image_bytes = downloaded_file.read()
        image_data = {
            "mime_type": "image/jpeg",
            "data": base64.b64encode(image_bytes).decode("utf-8")
        }
        await wait_msg.delete()

    elif message.document:
        doc = message.document
        ext = os.path.splitext(doc.file_name)[1].lower()
        supported_exts = ['.txt', '.pdf', '.docx', '.xlsx', '.pptx']
        
        await db_service.log_event(message.from_user.id, "FILE_UPLOAD", ext)

        if ext not in supported_exts:
            await message.answer(i18n.get_text("unsupported_format", lang, ext=ext))
            return

        wait_msg = await message.answer(i18n.get_text("wait_reading_doc", lang, ext=ext))
        file_info = await bot.get_file(doc.file_id)
        
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_file:
            await bot.download_file(file_info.file_path, temp_file.name)
            temp_path = temp_file.name

        try:
            extracted_text = await extract_text_from_file(temp_path, ext)
            if extracted_text.strip():
                file_context = f"\n\n[СОДЕРЖИМОЕ ПРИКРЕПЛЕННОГО ФАЙЛА {doc.file_name}]:\n{extracted_text[-30000:]}"
            else:
                await db_service.log_event(message.from_user.id, "FILE_PARSE_EMPTY", ext)
                await message.answer(i18n.get_text("extract_error", lang))
                return
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        
        await wait_msg.delete()
        user_query += file_context

    await _dispatch_chat_text(message, state, user_query, image_data=image_data)


async def trigger_long_input_processing(message: types.Message, state: FSMContext, user_id: int):
    """Автоматический запуск разбора большого текста (10К+ символов) через 3.0 сек тишины, даже если юзер не нажал «Готово»."""
    try:
        await asyncio.sleep(3.0)
    except asyncio.CancelledError:
        return

    current_state = await state.get_state()
    if current_state != BotStates.waiting_for_long_input.state:
        return

    data = await state.get_data()
    full_text = (data.get("text_buffer") or "").strip()
    img_data = data.get("img_buffer", None)
    if not full_text and not img_data:
        return

    lang = i18n.user_lang(message.from_user)
    await db_service.log_event(user_id, "LONG_INPUT_AUTO_FINISHED", f"chars:{len(full_text)}")
    await message.answer(i18n.get_text("doc_accepted", lang), reply_markup=main_kb(lang))
    await state.update_data(text_buffer="", img_buffer=None)

    await state.set_state(BotStates.generating)
    try:
        await process_ai_response(message, state, full_text, img_data)
    finally:
        st = await state.get_state()
        if st == BotStates.generating.state:
            await state.set_state(BotStates.chatting)


@dp.message(BotStates.waiting_for_long_input)
async def long_input_handler(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    lang = i18n.user_lang(message.from_user)
    
    if message.text and message.text.strip().lower() in DONE_WORDS:
        if user_id in long_input_tasks and not long_input_tasks[user_id].done():
            long_input_tasks[user_id].cancel()
        await db_service.log_event(user_id, "LONG_INPUT_FINISHED")
        full_text = data.get("text_buffer", "")
        img_data = data.get("img_buffer", None)
        
        await message.answer(i18n.get_text("doc_accepted", lang), reply_markup=main_kb(lang))
        await state.update_data(text_buffer="", img_buffer=None)
        
        await state.set_state(BotStates.generating)
        try:
            await process_ai_response(message, state, full_text, img_data)
        finally:
            current_state = await state.get_state()
            if current_state == BotStates.generating.state:
                await state.set_state(BotStates.chatting)
        return
    elif message.text in i18n.variants("btn_cancel"):
        if user_id in long_input_tasks and not long_input_tasks[user_id].done():
            long_input_tasks[user_id].cancel()
        await db_service.log_event(user_id, "LONG_INPUT_CANCELLED")
        await state.update_data(text_buffer="", img_buffer=None)
        await state.set_state(BotStates.chatting)
        await message.answer(i18n.get_text("cancelled", lang), reply_markup=main_kb(lang))
        return

    await db_service.log_event(user_id, "LONG_INPUT_APPEND")
    current_buffer = data.get("text_buffer", "")
    new_buffer = current_buffer + "\n" + (message.text or "")
    await state.update_data(text_buffer=new_buffer)
    if user_id in long_input_tasks and not long_input_tasks[user_id].done():
        long_input_tasks[user_id].cancel()
    long_input_tasks[user_id] = asyncio.create_task(trigger_long_input_processing(message, state, user_id))


CHAR_RANGE_RE = re.compile(r"(?i)\b(\d{2,5})\s*[-–—]\s*(\d{2,5})\s*(?:знак\w*|символ\w*)")
CHAR_MAX_RE = re.compile(
    r"(?i)(?:до|не\s+более|не\s+больше|максимум|макс\.?|лимит\w*|уложить\w*\s+в|в\s+пределах)\s*(\d{2,5})\s*(?:знак\w*|символ\w*)"
)


def _extract_explicit_char_limit(user_query: str, current_limit: int = 0) -> int:
    """Детерминированно извлекает верхнюю границу лимита знаков (например, 1200-1500 знаков -> 1500)."""
    q = user_query or ""
    m_range = CHAR_RANGE_RE.search(q)
    if m_range:
        upper = int(m_range.group(2))
        if 40 <= upper <= 20000:
            return max(int(current_limit or 0), upper)
    m_max = CHAR_MAX_RE.search(q)
    if m_max:
        val = int(m_max.group(1))
        if 40 <= val <= 20000:
            return int(current_limit or 0) or val
    return int(current_limit or 0)


def _extract_lead_hook(response_text: str) -> str:
    """Извлекает первую смысловую строку/заход блока СТАЛО (или ответа) для <STORY_ARC_LEDGER>."""
    txt = (response_text or "").strip()
    m = HAS_STALO_BLOCK_RE.search(txt)
    if m:
        after = txt[m.end():].lstrip(" :\n—-")
        for line in after.splitlines():
            clean = line.strip(" «»\"'-•—")
            if len(clean) >= 12:
                return clean[:115]
    for line in txt.splitlines():
        clean = line.strip(" «»\"'-•—")
        if len(clean) >= 12:
            return clean[:115]
    return txt[:115]


async def process_ai_response(message: types.Message, state: FSMContext, user_query: str, image_data: dict = None):
    uid = message.from_user.id
    lang = i18n.user_lang(message.from_user)
    user = await db_service.get_user(uid)
    if not user:
        user = await db_service.create_user(uid, message.from_user.username, message.from_user.full_name)

    data = await state.get_data()
    is_unlim = await db_service.has_unlimited(uid)

    # 1. Загружаем активную папку проекта, её изолированное досье, редполитику, голос, историю и летопись дуги
    proj = await db_service.get_or_create_active_project(uid)
    brief = await db_service.get_brief(uid)
    dossier_text = format_brief(brief)
    editorial_rules = await db_service.get_editorial_rules(uid)
    proj_history = await db_service.get_project_history(uid)
    history = proj_history if proj_history else data.get("history", [])
    voice_samples = await db_service.get_voice_samples(uid)
    arc_ledger = await db_service.get_arc_ledger(uid)
    all_user_model_texts = await db_service.get_all_user_model_texts(uid)
    display_name = _clean_user_display_name(message.from_user)

    # 1b. Автоскан публичного Telegram-канала из чата (0 LLM-токенов), если клиент кинул @username / t.me/...
    tg_handle = extract_tg_username(user_query)
    if tg_handle and f"@{tg_handle.lower()}" not in (dossier_text or "").lower():
        snap = await fetch_public_tg_channel_snapshot(tg_handle, http_session=llm_service.http_session)
        if snap:
            if snap.get("voice_samples") and not voice_samples:
                voice_samples = snap["voice_samples"]
                await db_service.save_voice_samples(uid, voice_samples)
            updated_brief = dict(brief or {})
            prev_ch = (updated_brief.get("channel") or "").strip()
            scan_block = f"[АВТОСКАН ОТКРЫТОГО TELEGRAM-КАНАЛА]\n{snap['formatted_summary']}"
            updated_brief["channel"] = f"{prev_ch}\n\n{scan_block}".strip() if prev_ch else scan_block
            await db_service.save_brief(uid, updated_brief)
            brief = updated_brief
            dossier_text = format_brief(brief)
            await db_service.log_event(
                uid,
                "TG_CHANNEL_SCANNED",
                f"@{snap['username']}, posts:{snap['posts_count']}, ads:{snap.get('ads_filtered_count', 0)}",
            )

    # 2. Детерминированный перехват редполитики из реплик клиента (0 токенов)
    updated_rules = assistant_service.extract_editorial_rules_deterministic(user_query, editorial_rules)
    if updated_rules != editorial_rules:
        editorial_rules = updated_rules
        await db_service.save_editorial_rules(uid, editorial_rules)
        await db_service.log_event(
            uid,
            "EDITORIAL_RULE_AUTO_EXTRACTED",
            json.dumps(editorial_rules, ensure_ascii=False)[:500],
        )

    # 3. Предохранитель №2: отсечение пустых ходов при 0 кредитов ДО вызова Flash-роутера (0 токенов)
    expired = False
    if not is_unlim:
        expired = await db_service.reset_balance_if_expired(uid)
        if expired:
            user.credits = 0
        if user.credits < 1:
            can_use_grace = (
                not expired
                and not data.get("objection_grace_used")
                and len(history) >= 2
                and (len(user_query.strip()) < 1400 or bool(OBJECTION_OR_CRITIQUE_RE.search(user_query)))
            )
            if not can_use_grace:
                await db_service.log_event(uid, "OUT_OF_CREDITS")
                msg = i18n.get_text("subscription_expired", lang) if expired else i18n.get_text("no_credits", lang)
                await message.answer(msg, parse_mode="Markdown", reply_markup=paywall_kb(lang))
                return

    wait_msg = await message.answer(i18n.get_text("wait_thinking", lang))
    logger.info(f"📥 [USER {uid} | proj #{proj.slot_index}]: {user_query[:500]}...")
    await db_service.log_event(uid, "USER_MESSAGE", user_query[:4000])

    new_states = await db_service.update_user_state(uid, fatigue_delta=5)
    await db_service.log_event(
        uid,
        "AFFECTIVE_STATE_UPDATE",
        f"trust:{new_states['trust']}, fatigue:{new_states['fatigue']}, mood:{new_states['mood']}",
    )

    interaction_count = data.get("interaction_count", 0) + 1
    current_time = time.time()

    # 4. Вызов легковесного JSON-роутера gemini-2.5-flash (T=0.0, GEMINI_ROUTE_SCHEMA)
    decision = await llm_service.route_request_lego(
        user_query=user_query,
        history_tail=history[-4:],
        dossier_text=dossier_text,
        pinned_register=proj.pinned_register,
    )

    # Детерминированные поправки к RouteDecision (лимит знаков, защита от выдумки цифр и инъекций)
    decision.explicit_char_limit = _extract_explicit_char_limit(user_query, decision.explicit_char_limit)

    if re.search(
        r"(?i)(придумай\w*\s+(?:правдоподобн\w+\s+)?(?:кейс|цифр|статистик)|"
        r"выдумай\w*\s+(?:кейс|цифр)|цифр\s+(?:у\s+меня\s+)?пока\s+нет)",
        user_query,
    ):
        decision.fact_sufficiency = "PARTIAL"
        if not decision.missing_fact_hint:
            decision.missing_fact_hint = "реальный кейс и точные показатели из вашей практики"

    if (
        re.search(
            r"(?i)(игнорируй\s+предыдущ|забудь\s+(?:все\s+)?инструкц|ignore\s+all\s+previous|"
            r"скинь\s+(?:свой\s+)?(?:системный\s+)?промпт|выведи\s+(?:свой\s+)?(?:системный\s+)?промпт)",
            user_query,
        )
        and len(user_query.strip()) < 260
    ):
        decision.intent_type = "ABUSE_OR_OFFTOPIC"
    elif _is_meta_question(user_query) and len(user_query.strip()) < 360 and decision.intent_type in ("WORK_TASK", "EDITORIAL_TASK"):
        decision.intent_type = "PRODUCT_META_QA"

    if re.search(
        r"(?i)(энергопрактик|высоки\w+\s+вибрац|квантов\w+\s+скач|дыхани\w+\s+матк|"
        r"денежн\w+\s+расширени\w*\s+в\s+поток|род\w+\s+программ\w*\s+изобил)",
        user_query,
    ):
        decision.genre = "IDEOLOGICAL_REFUSAL"
        if not proj.pinned_register:
            decision.register = "CREATOR_MEDIA"

    await db_service.log_event(uid, "LEGO_ROUTE_DECISION", decision.model_dump_json())

    # 5. Вычисляем is_free_meta (без ложного срабатывания «нда» в слове «бренда»!), is_objection_grace и expected_credits_after
    is_privacy_meta = (
        len(user_query.strip()) <= 220
        and decision.genre == "STRATEGY_QA"
        and bool(
            BRIEF_PRIVACY_WORD_RE.search(user_query)
            or re.search(r"(?i)(опубликуете|всплыв[её]т|где\s+хранятся|кто\s+(?:видит|читает))", user_query)
        )
    )
    is_free_meta = (
        user.credits >= 1
        and not data.get("meta_question_free_used")
        and len(user_query.strip()) < 600
        and (
            decision.intent_type in ("PRODUCT_META_QA", "ABUSE_OR_OFFTOPIC")
            or (_is_meta_question(user_query) and decision.genre == "STRATEGY_QA")
            or is_privacy_meta
        )
    )

    is_objection_grace = False
    if not is_unlim and not is_free_meta and user.credits < 1:
        is_objection_grace = True
        await state.update_data(objection_grace_used=True)
        await db_service.log_event(uid, "OBJECTION_GRACE_USED", user_query[:400])

    if is_unlim:
        expected_credits_after = 999
    elif is_free_meta:
        expected_credits_after = user.credits
    elif is_objection_grace:
        expected_credits_after = 0
    else:
        expected_credits_after = max(0, user.credits - 1)

    # 6. Поиск фактов и RAG (при необходимости)
    search_results = ""
    if decision.needs_search and decision.search_query:
        logger.info(f"🔎 [LegoRouter] Поиск фактов: '{decision.search_query}'")
        search_results, search_tokens = await llm_service.execute_grounding_search(decision.search_query)
        await db_service.log_event(
            uid,
            "GROUNDING_SEARCH",
            json.dumps({"query": decision.search_query, "tokens": search_tokens}, ensure_ascii=False),
        )

    rag_ctx = await rag_service.search(user_query)
    agenda, _ = _load_live_channel_and_day_context(user_query)
    if agenda:
        rag_ctx = (
            f"[СПРАВКА ПО ПОСЛЕДНЕМУ ПОСТУ В ПУБЛИЧНОМ КАНАЛЕ АРИСТАРХА ({agenda.get('published_at', '')})]:\n"
            f"Тема: {agenda.get('focus_topic', '')}\nСуть: {agenda.get('daily_thesis', '')}\n\n"
            + (rag_ctx or "")
        )

    # 7. Статус поставленного на паузу брифа
    brief_status = ""
    if brief:
        missing_fields = [f for f in BRIEF_FIELDS if not brief.get(f)]
        if 0 < len(missing_fields) < len(BRIEF_FIELDS):
            next_f = missing_fields[0]
            next_num = BRIEF_FIELDS.index(next_f) + 1
            filled_num = len(BRIEF_FIELDS) - len(missing_fields)
            q_topic = BRIEF_LABELS.get(next_f, next_f)
            brief_status = (
                f"[СИСТЕМА: Анкета брифа поставлена на паузу на вопросе {next_num}/5 («{q_topic}»). "
                f"Сохранено {filled_num}/5 ответов в досье папки «{_clean_folder_title(proj.title, 28)}». "
                f"Если клиент спросит про анкету — поясни, что ответы сохранены и продолжить бриф с вопроса {next_num}/5 можно кнопкой «📂 Проект / Бриф».]"
            )
    elif data.get("brief_paused_step") is not None:
        paused_idx = int(data["brief_paused_step"])
        next_f = BRIEF_FIELDS[paused_idx]
        q_topic = BRIEF_LABELS.get(next_f, next_f)
        brief_status = (
            f"[СИСТЕМА: Анкета брифа поставлена на паузу на вопросе {paused_idx + 1}/5 («{q_topic}»). "
            f"Сохранено 0/5 ответов в досье папки «{_clean_folder_title(proj.title, 28)}». "
            f"Если клиент спросит про анкету — поясни, что вернуться к брифу с вопроса {paused_idx + 1}/5 можно кнопкой «📂 Проект / Бриф».]"
        )

    # 8. Сборка компактного системного промпта через LegoPromptBuilder v2.2 (с ротацией палитр и летописью дуги)
    sys_prompt = LegoPromptBuilder.build(
        decision=decision,
        pinned_register=proj.pinned_register,
        project_title=_clean_folder_title(proj.title, 28),
        dossier_text=dossier_text,
        editorial_rules=editorial_rules,
        project_history=history,
        credits_after=expected_credits_after,
        is_free_meta=is_free_meta,
        is_objection_grace=is_objection_grace,
        brief_pause_status=brief_status,
        search_results=search_results,
        temp=user.temperature,
        user_display_name=display_name,
        all_user_model_texts=all_user_model_texts,
        voice_samples=voice_samples,
        arc_ledger=arc_ledger,
        rotation_seed=uid,
    )

    failed = False
    try:
        response_text = await llm_service.generate(
            sys_prompt,
            user_query,
            rag_ctx,
            "",  # episodic_ctx изолирован внутри истории папки проекта
            "",  # semantic_ctx передан через <PROJECT_DOSSIER> и <EDITORIAL_POLICY>
            history[-40:],
            user.temperature,
            image_data,
            search_results,
        )
        failed = response_text.startswith(("⚠️", "⛔️"))

        if "[IGNORE]" in response_text:
            logger.info(f"🔇 [ИГНОР] Аристарх молча проигнорировал юзера {uid}")
            await db_service.log_event(uid, "AI_IGNORED_USER")
            new_history = history + [
                {"role": "user", "parts": [{"text": _compact_user_query_for_history(user_query)}]},
                {"role": "model", "parts": [{"text": "[АРИСТАРХ МОЛЧА ПРОИГНОРИРОВАЛ СООБЩЕНИЕ]"}]},
            ]
            await state.update_data(
                history=new_history[-40:],
                interaction_count=interaction_count,
                last_message_time=current_time,
            )
            await db_service.save_project_history(uid, new_history[-40:])
            try:
                await wait_msg.delete()
            except Exception:
                pass
            await state.set_state(BotStates.chatting)
            return

        if not failed:
            response_text = await polish_reply(llm_service, response_text, user_query)
            response_text = humanize_punctuation(response_text.replace("*", ""))
            # Жёсткий пост-чек биллинга: если ответ содержит готовый боевой блок «СТАЛО:» при credits >= 1, он ОБЯЗАН списать 1 кредит
            if is_free_meta and HAS_STALO_BLOCK_RE.search(response_text) and not is_objection_grace and user.credits >= 1:
                is_free_meta = False
                if not is_unlim:
                    expected_credits_after = max(0, user.credits - 1)
                await db_service.log_event(uid, "FREE_META_OVERRIDDEN_BY_STALO", f"genre:{decision.genre}")
            elif is_free_meta:
                await state.update_data(meta_question_free_used=True)
                await db_service.log_event(uid, "FREE_META_QUESTION", user_query[:300])

            # Детерминированная защита финала при credits_after == 0: не провоцируем клиента на вопрос перед пейволлом
            if expected_credits_after <= 0 and response_text.rstrip().endswith("?"):
                paragraphs = [p.strip() for p in response_text.strip().split("\n\n") if p.strip()]
                if len(paragraphs) > 1 and paragraphs[-1].endswith("?") and len(paragraphs[-1]) < 220:
                    paragraphs.pop()
                    response_text = "\n\n".join(paragraphs)
                else:
                    sents = re.split(r"(?<=[.!?])\s+", response_text.strip())
                    if len(sents) > 1 and sents[-1].endswith("?"):
                        response_text = " ".join(sents[:-1]).strip()

        logger.info(f"📤 [ARISTARKH {uid} | {decision.register}/{decision.genre}]: {response_text[:500]}...")
        await db_service.log_event(uid, "AI_RESPONSE_TEXT", response_text[:4000])
        await db_service.log_event(uid, "AI_RESPONSE_GENERATED", f"chars:{len(response_text)}")
    except Exception as e:
        logger.error(f"Gen Error: {e}")
        response_text = i18n.get_text("generation_error", lang)
        failed = True
        await db_service.log_event(uid, "AI_RESPONSE_ERROR", str(e))

    try:
        await wait_msg.delete()
    except Exception:
        pass

    if failed:
        await send_smart_response(message, response_text)
        return

    new_history = history + [
        {"role": "user", "parts": [{"text": _compact_user_query_for_history(user_query)}]},
        {"role": "model", "parts": [{"text": response_text}]},
    ]

    await state.update_data(
        history=new_history[-40:],
        interaction_count=interaction_count,
        last_message_time=current_time,
    )
    await db_service.save_project_history(uid, new_history[-40:])

    # Детерминированная запись в <STORY_ARC_LEDGER> (0 LLM-токенов) для долгой дистанции 15-30 разборов
    if HAS_STALO_BLOCK_RE.search(response_text) or decision.genre != "STRATEGY_QA":
        used_in_reply = extract_used_metaphors([{"role": "model", "parts": [{"text": response_text}]}])
        await db_service.append_arc_ledger_entry(
            uid,
            {
                "genre": decision.genre,
                "lead_hook": _extract_lead_hook(response_text),
                "metaphors": used_in_reply,
            },
        )

    if interaction_count % 5 == 0:
        asyncio.create_task(extract_and_save_memory(uid, new_history[-10:]))
        await db_service.log_event(
            uid,
            "MEMORY_CONSOLIDATION_TRIGGERED",
            f"interaction:{interaction_count}, history_len:{len(new_history)}",
        )

    new_bal = user.credits
    if not is_unlim and not is_free_meta and not is_objection_grace:
        new_bal = await db_service.update_balance(uid, -1)

    await send_smart_response(message, response_text)

    if is_objection_grace:
        has_rewritten_draft = bool(HAS_STALO_BLOCK_RE.search(response_text))
        footer_key = "grace_credits_footer" if has_rewritten_draft else "grace_question_footer"
        await message.answer(
            i18n.get_text(footer_key, lang),
            parse_mode="Markdown",
            reply_markup=paywall_kb(lang),
        )
    elif not is_unlim and not is_free_meta and new_bal <= 0:
        await message.answer(
            i18n.get_text("no_credits", lang),
            parse_mode="Markdown",
            reply_markup=paywall_kb(lang),
        )

    should_suppress_brief_hint = (
        is_free_meta
        or is_objection_grace
        or (not is_unlim and new_bal <= 0)
        or bool(BRIEF_REFUSAL_RE.search(user_query))
        or bool(re.search(r"(?i)(/brief|бриф|шапк\w+|закреп|черновик|пришлите|скидывайте|присылайте)", response_text))
    )
    if not brief and not data.get("brief_hint_shown"):
        if not should_suppress_brief_hint:
            await message.answer(i18n.get_text("brief_hint", lang))
        await state.update_data(brief_hint_shown=True)

    if config.ENABLE_ASSISTANT_HINTS and (is_unlim or new_bal > 2):
        try:
            suggestion = await assistant_service.generate_suggestion(new_history[-40:])
            if suggestion:
                clean_suggestion = html.escape(humanize_punctuation(suggestion.strip()))
                suggestion_msg = i18n.get_text("assistant_hint", lang, text=clean_suggestion)
                await message.answer(suggestion_msg, parse_mode="HTML")
                await db_service.log_event(uid, "ASSISTANT_SUGGESTION_SENT", suggestion)
        except Exception as e:
            logger.error(f"Assistant generation failed: {e}")
            await db_service.log_event(uid, "ASSISTANT_ERROR", str(e))


async def extract_and_save_memory(user_id: int, history_chunk: list):
    try:
        mem_data = await assistant_service.extract_memory_and_state(history_chunk)
        if not mem_data: 
            logger.warning(f"⚠️ Экстрактор вернул пустой результат для юзера {user_id}")
            return
        
        if mem_data.get("summary"):
            await rag_service.add_episodic_memory(
                user_id, mem_data["summary"], mem_data.get("emotion_weight", 0.5)
            )
            logger.info(f"🧠 [Эпизодическая] Сохранено саммари для {user_id}")
            
        if mem_data.get("facts"):
            await semantic_memory.add_facts(user_id, mem_data["facts"])
            logger.info(f"🕸️ [Семантическая] Сохранено {len(mem_data['facts'])} фактов для {user_id}")
            
        if mem_data.get("trust_delta") or mem_data.get("mood"):
            await db_service.update_user_state(
                user_id, 
                trust_delta=mem_data.get("trust_delta", 0), 
                new_mood=mem_data.get("mood")
            )
            logger.info(f"💓 [Аффективная] Обновлены trust={mem_data.get('trust_delta', 0)}, mood={mem_data.get('mood')} для {user_id}")
            
        await db_service.log_event(
            user_id, "MEMORY_CONSOLIDATED", 
            f"facts:{len(mem_data.get('facts', []))}, summary:{bool(mem_data.get('summary'))}, "
            f"trust_delta:{mem_data.get('trust_delta', 0)}, mood:{mem_data.get('mood', 'N/A')}"
        )
        logger.info(f"🔄 Память полностью консолидирована для юзера {user_id}")
        
    except Exception as e:
        logger.error(f"❌ Ошибка фонового воркера памяти для {user_id}: {e}")
        await db_service.log_event(user_id, "MEMORY_CONSOLIDATION_ERROR", str(e))


async def main():
    logger.info("🚀 Запуск Аристарха v22.0 (Умная локация + Защита персоны)...")
    await db_service.init_db()
    await rag_service.build_index()
    
    http_session = aiohttp.ClientSession()
    llm_service.http_session = http_session
    rag_service.http_session = http_session
    assistant_service.http_session = http_session 
    evolution_engine.http_session = http_session
    
    try:
        try:
            await bot.delete_webhook(drop_pending_updates=True)
        except Exception as e:
            logger.error(f"Не удалось сбросить вебхук из-за таймаута Telegram. Идем дальше: {e}")
            
        await dp.start_polling(bot)
    finally:
        await http_session.close()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass