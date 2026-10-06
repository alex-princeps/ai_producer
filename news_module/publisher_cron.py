import argparse
import asyncio
import aiohttp
import html
import json
import os
import random
from datetime import datetime, timezone
from aiogram import Bot
from aiogram.types import LinkPreviewOptions
from aiogram.client.session.aiohttp import AiohttpSession  # [NEW] Для прокси-сессии
from aristarkh_core.config import config, logger
from aristarkh_core.gemini import LLMService
from aristarkh_core.rag_chroma import RAGService
from aristarkh_core.evolution_engine import PersonaEvolutionEngine
from news_module import columnist, poetry, typos
from news_module.state_io import update_json
from news_module.post_builder import (
    BOT_CTA_ENABLED, append_history, generate_personal, generate_post, schedule_followup,
)

# Канал для постов (задаётся только через .env)
CHANNEL_ID = os.getenv("TG_CHANNEL_ID", "")


def _env_float(name: str, default: float = 0.0) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


# Ритм живого автора: разброс времени публикации и личная заметка воскресным вечером (по умолчанию выключены)
PUBLISH_JITTER_MIN = _env_float("PUBLISH_JITTER_MIN")
PERSONAL_SUNDAY_PROB = _env_float("PERSONAL_SUNDAY_PROB")
# Редкая опечатка, которую автор через несколько минут правит редактированием поста (по умолчанию выключена)
TYPO_PROB = _env_float("TYPO_PROB")
TYPO_FIX_MINUTES = (2, 25, 6)  # мин, макс, чаще всего: треугольное распределение


def _with_typo(channel_post: str, post_meta: dict) -> tuple[str, dict] | None:
    """HTML поста с одной опечаткой в тексте автора (шапка и цитата новости не трогаются) или None."""
    text = (post_meta.get("text") or "").strip()
    escaped = html.escape(text)
    if not text or channel_post.count(escaped) != 1:
        return None
    # Защищены все строки банка цитат, а не только записанной цитаты: стих в посте не трогаем никогда
    verse = [line for entry in poetry.load_bank() for line in entry["lines"].split("\n")]
    made = typos.make_typo(text, verse)
    if not made:
        return None
    typo_text, info = made
    return channel_post.replace(escaped, html.escape(typo_text), 1), info


async def _fix_typo(bot: Bot, news_path: str, message_id: int, correct_html: str):
    """Через несколько минут автор «замечает» опечатку и правит пост: в канале появляется пометка «изменено»."""
    delay = random.triangular(*TYPO_FIX_MINUTES)
    logger.info(f"✏️ [Publisher] Опечатку заметит через {delay:.0f} мин.")
    await asyncio.sleep(delay * 60)
    if await typos.edit_back(bot, CHANNEL_ID, message_id, correct_html):
        typos.mark_typo_fixed(news_path, CHANNEL_ID, message_id)
        logger.info("✅ [Publisher] Опечатка исправлена правкой поста.")
    else:
        logger.error("❌ [Publisher] Не удалось исправить опечатку: повторит followup_cron.")


async def run_publisher(dry_run: bool = False, force_format: str | None = None,
                        force_personal: bool = False, no_jitter: bool = False,
                        force_typo: bool = False, force_poetry: bool = False):
    """
    Автономный воркер для публикации постов в канал из TG_CHANNEL_ID.

    Пайплайн:
    1. Решает, что сегодня в этот слот: обычный пост, личная заметка (воскресный вечер) или пропуск
    2. Читает свежую новость из tg_news.json (результат работы tg_news.py) и проверяет, не была ли она опубликована
    3. Ждёт случайные 0..PUBLISH_JITTER_MIN минут, чтобы посты не выходили минута в минуту
    4. Генерирует пост через news_module/post_builder.py: формат, настроение и сцена дня, память о своих постах,
       RAG-контекст, стоп-лист клише, вывод для читателя своими словами, ссылка на бота только при CHANNEL_BOT_CTA
    5. Добавляет шапку: ссылка на новость + свёрнутая цитата оригинала
    6. Пушит в канал через Telegram Bot API (HTML, без превью ссылки)
    7. Помечает новость как опубликованную и сохраняет историю постов
    8. Если выпала опечатка (TYPO_PROB): пост уходит с ней, а через 2-25 минут правится

    --dry-run: генерирует пост и печатает его, ничего не публикуя и не пропуская.
    --personal: личная заметка вместо поста по новости.
    --typo: опечатка обязательно (в --dry-run показывает, как выглядел бы пост с ней).
    --poetry: цитата из стихов обязательно (если есть банк цитат).
    """
    logger.info("🚀 [Publisher Cron] Запуск публикации поста для канала...")
    if not CHANNEL_ID and not dry_run:
        logger.error("❌ [Publisher] TG_CHANNEL_ID не задан в .env.")
        return

    base_dir = os.path.dirname(os.path.abspath(__file__))
    news_path = os.path.join(os.path.dirname(base_dir), "tg_news.json")

    # === ШАГ 0: Проверка наличия файла с новостями ===
    if not os.path.exists(news_path):
        logger.error("❌ [Publisher] Файл tg_news.json не найден. Парсер не отработал.")
        return

    # === ШАГ 1: Чтение JSON с обработкой ошибок ===
    try:
        with open(news_path, 'r', encoding='utf-8') as f:
            news_data = json.load(f)
    except json.JSONDecodeError as e:
        logger.error(f"❌ [Publisher] Файл tg_news.json поврежден: {e}")
        return
    except Exception as e:
        logger.error(f"❌ [Publisher] Ошибка чтения tg_news.json: {e}")
        return

    # === ШАГ 1.5: Что сегодня в этот слот (только при файле личности колумниста) ===
    now = datetime.now(columnist.MSK)
    slot = columnist.slot_of(now)
    personal = force_personal
    if columnist.load_persona() and not force_personal and not dry_run:
        day = columnist.get_day(now)
        if random.random() < columnist.skip_probability(day, slot):
            logger.info(f"🛋️ [Publisher] Сегодня не пишется (настроение «{day['mood']}»): слот пропущен.")
            return
        personal = slot == "evening" and now.weekday() == 6 and random.random() < PERSONAL_SUNDAY_PROB

    digest = news_data.get("current_digest", {})
    if not personal:
        if not digest or "content" not in digest:
            logger.error("❌ [Publisher] В tg_news.json нет актуального current_digest.")
            return
        # === ШАГ 2: Проверка на дубли ===
        if digest.get("published", False) and not dry_run:
            logger.info("⏩ [Publisher] Эта новость уже была опубликована. Пропускаем.")
            return
        news_text = digest.get("content", "").strip()
        if not news_text:
            logger.error("❌ [Publisher] Пустой текст новости в current_digest.")
            return
        logger.info(f"📰 [Publisher] Взята новость в работу: {news_text[:100]}...")
    else:
        logger.info("📝 [Publisher] Сегодня личная заметка вместо новости.")

    # === ШАГ 2.5: Живой автор не публикует минута в минуту ===
    if not dry_run and not no_jitter and PUBLISH_JITTER_MIN > 0:
        delay = random.uniform(0, PUBLISH_JITTER_MIN) * 60
        logger.info(f"⏳ [Publisher] Пауза {delay / 60:.0f} мин перед публикацией.")
        await asyncio.sleep(delay)

    # === ШАГ 3: Инициализация сервисов ===
    rag_service = RAGService(config.GOOGLE_API_KEY)
    evolution_engine = PersonaEvolutionEngine(config.GOOGLE_API_KEY)

    # [CRITICAL FIX] Прокси-сессия для Telegram API (как в main.py)
    proxy_url = os.getenv("PROXY_URL")
    if proxy_url:
        logger.info(f"🌐 [Publisher] Включен прокси-сервер для Telegram API")
        bot_session = AiohttpSession(timeout=120, proxy=proxy_url)
    else:
        logger.info("🌐 [Publisher] Прокси не настроен, работаем напрямую")
        bot_session = AiohttpSession(timeout=120)

    bot = Bot(token=config.BOT_TOKEN, session=bot_session)

    async with aiohttp.ClientSession() as session:
        llm = LLMService(config.GOOGLE_API_KEY)
        llm.http_session = session
        rag_service.http_session = session
        evolution_engine.http_session = session

        try:
            history = news_data.get("post_history", [])
            if personal:
                # === Личная заметка: без новости, без шапки, без добивки ===
                logger.info("🧠 [Publisher] Аристарх пишет личную заметку...")
                channel_post, post_meta = await generate_personal(llm, history, poetry_mode=force_poetry or None)
            else:
                # === ШАГ 4: Core Beliefs (фон для суждений, если нет файла личности) ===
                core_beliefs = evolution_engine.get_current_beliefs()
                logger.info(f"🧬 [Publisher] Загружено убеждений: {len(core_beliefs)}")

                # === ШАГ 5: Генерация поста (формат, тон, анти-клише, RAG, шапка со ссылкой) ===
                # Ссылка на бота в постах только при CHANNEL_BOT_CTA: по умолчанию канал - витрина личности
                cta_handle = f"@{(await bot.get_me()).username}" if BOT_CTA_ENABLED else None
                logger.info("🧠 [Publisher] Аристарх пишет пост...")
                channel_post, post_meta = await generate_post(
                    llm, rag_service, core_beliefs, digest, history, force_format, cta_handle=cta_handle,
                    poetry_mode=force_poetry or None,
                )
            logger.info(f"   Пост сгенерирован: {len(channel_post)} символов (формат {post_meta['format']})")

            typo = None
            if force_typo or (not dry_run and TYPO_PROB > 0 and random.random() < TYPO_PROB):
                typo = _with_typo(channel_post, post_meta)
                if typo:
                    logger.info(f"✏️ [Publisher] Опечатка: «{typo[1]['word']}» → «{typo[1]['typo']}» ({typo[1]['kind']})")

            if dry_run:
                print("\n" + "=" * 70 + "\n[DRY RUN] Пост НЕ опубликован. HTML для Telegram:\n" + "=" * 70)
                print(channel_post)
                print("=" * 70 + f"\nМета: {post_meta}\n")
                if typo:
                    print(f"[DRY RUN] С опечаткой «{typo[1]['word']}» → «{typo[1]['typo']}» пост ушёл бы так, "
                          f"а через {TYPO_FIX_MINUTES[0]}-{TYPO_FIX_MINUTES[1]} мин был бы исправлен:\n{typo[0]}\n")
                return

            # === ШАГ 6: Публикация в канал ===
            logger.info(f"📤 [Publisher] Публикация в {CHANNEL_ID}...")
            sent_message = await bot.send_message(
                CHANNEL_ID,
                typo[0] if typo else channel_post,
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            logger.info(f"✅ [Publisher] Пост успешно опубликован! Message ID: {sent_message.message_id}")
            published_at = datetime.now(timezone.utc)
            followup_at = None if personal else schedule_followup(published_at)
            post_meta.update({
                "channel": CHANNEL_ID,
                "message_id": sent_message.message_id,
                "published_at": published_at.isoformat(),
                "followup_at": followup_at.isoformat() if followup_at else None,
                "followup_done": False,
            })
            if typo:
                # Правильный HTML в истории: если правка отсюда не пройдёт, её повторит followup_cron
                post_meta["typo"] = {**typo[1], "fixed": False, "fix_html": channel_post}
            if followup_at:
                logger.info(f"⏰ [Publisher] Запланирована добивка на {followup_at.isoformat()}")
            if personal:
                columnist.journal_add("personal", post_meta["text"])

            # === ШАГ 7: История постов и флаг published - в свежую копию файла под блокировкой ===
            def record(data: dict):
                data["post_history"] = append_history(data.get("post_history", []), post_meta)
                current = data.get("current_digest", {})
                if not personal and current.get("url") == digest.get("url"):
                    current.update(published=True, published_at=published_at.isoformat(),
                                   published_message_id=sent_message.message_id)

            try:
                update_json(news_path, record)
                logger.info("📝 [Publisher] История постов сохранена.")
            except Exception as e:
                logger.error(f"❌ [Publisher] Не удалось сохранить tg_news.json: {e}")
                logger.warning("⚠️ При следующем запуске возможен повторный пост!")

            # === ШАГ 8: Автор замечает опечатку и правит пост ===
            if typo:
                await _fix_typo(bot, news_path, sent_message.message_id, channel_post)

        except Exception as e:
            logger.error(f"❌ [Publisher] Критическая ошибка при публикации: {e}")
        finally:
            # Безопасное закрытие сессии бота
            try:
                if bot.session:
                    await bot.session.close()
                    logger.debug("   Сессия бота закрыта.")
            except Exception as e:
                logger.warning(f"   Предупреждение при закрытии сессии: {e}")

            # Небольшая пауза перед завершением
            await asyncio.sleep(0.5)

    logger.info("🏁 [Publisher Cron] Работа завершена.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Публикация поста Аристарха в канал")
    parser.add_argument("--dry-run", action="store_true", help="сгенерировать и показать пост, не публикуя и не меняя tg_news.json")
    parser.add_argument("--format", dest="force_format", help="принудительный формат поста (id из POST_FORMATS), для тестов")
    parser.add_argument("--personal", action="store_true", help="личная заметка вместо поста по новости")
    parser.add_argument("--no-jitter", action="store_true", help="публиковать сразу, без случайной паузы")
    parser.add_argument("--typo", action="store_true", help="опечатка обязательно (с --dry-run: показать пост с ней)")
    parser.add_argument("--poetry", action="store_true", help="цитата из стихов обязательно (нужен банк цитат)")
    args = parser.parse_args()
    asyncio.run(run_publisher(dry_run=args.dry_run, force_format=args.force_format,
                              force_personal=args.personal, no_jitter=args.no_jitter,
                              force_typo=args.typo, force_poetry=args.poetry))
