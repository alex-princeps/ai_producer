import argparse
import asyncio
import aiohttp
import html
import json
import os
import random
from datetime import datetime, timedelta, timezone
from aiogram import Bot
from aiogram.types import LinkPreviewOptions
from aiogram.client.session.aiohttp import AiohttpSession  # [NEW] Для прокси-сессии
from aristarkh_core.config import config, logger
from aristarkh_core.gemini import LLMService
from aristarkh_core.rag_chroma import RAGService
from aristarkh_core.evolution_engine import PersonaEvolutionEngine
from news_module import b2b_vault, columnist, curator, poetry, provocateur, table_vault, topics_vault, typos
from news_module.state_io import update_json
from news_module.post_builder import (
    BOT_CTA_ENABLED, POST_FORMATS, append_history, generate_b2b_post, generate_personal,
    generate_post, generate_table_post, generate_vault_post, schedule_followup,
)

# Канал для постов (задаётся только через .env)
CHANNEL_ID = os.getenv("TG_CHANNEL_ID", "")


def _env_float(name: str, default: float = 0.0) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


# Ритм живого автора: разброс времени публикации и личная заметка по воскресеньям (по умолчанию выключены)
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
                        force_personal: bool = False, force_vault: bool = False,
                        force_b2b: bool = False, force_table: bool = False,
                        force_news: bool = False,
                        no_jitter: bool = False, force_typo: bool = False,
                        force_poetry: bool = False):
    """
    Автономный воркер для публикации постов в канал из TG_CHANNEL_ID по недельной ТВ-сетке вещания:
    - Пн, Вт, Чт, Сб: «Аппаратная» (разбор новости со скрытым Провокатором и Диагностическим зеркалом в 3-м лице)
    - Ср: «Монтажный стол: Было → Стало» (с авто-фоллбэком на новость, если банк пуст)
    - Пт: «Вопрос из зрительного зала» (из Золотого фонда тем, с авто-фоллбэком на новость)
    - Вс: B2B-рубрика «Вскрытие кассы / Хроники Франкенштейна» (с авто-фоллбэком на новость)
    """
    logger.info("🚀 [Publisher Cron] Запуск публикации поста для канала...")
    if not CHANNEL_ID and not dry_run:
        logger.error("❌ [Publisher] TG_CHANNEL_ID не задан в .env.")
        return

    base_dir = os.path.dirname(os.path.abspath(__file__))
    news_path = os.path.join(os.path.dirname(base_dir), "tg_news.json")

    news_data = {}
    if os.path.exists(news_path):
        try:
            with open(news_path, 'r', encoding='utf-8') as f:
                news_data = json.load(f)
        except Exception as e:
            logger.warning(f"⚠️ [Publisher] Не удалось прочитать tg_news.json: {e}")

    # === ШАГ 1.5: Недельная телевизионная сетка вещания (по Москве) ===
    now = datetime.now(columnist.MSK)
    slot = columnist.slot_of(now)
    wd = now.weekday()
    personal = force_personal
    history = news_data.get("post_history", [])

    any_forced = bool(force_personal or force_vault or force_b2b or force_table or force_format or force_news)
    is_vault = force_vault
    is_b2b = force_b2b
    is_table = force_table

    if not any_forced:
        if wd == 4 and topics_vault.count_stats()[1] > 0:
            is_vault = True
        elif wd == 6 and b2b_vault.count_b2b_stats()[1] > 0:
            is_b2b = True
        elif wd == 2 and table_vault.count_table_stats()[1] > 0:
            is_table = True

    is_special_rubric = is_vault or is_b2b or is_table

    if columnist.load_persona() and not any_forced and not is_special_rubric and not dry_run:
        day = columnist.get_day(now)
        if random.random() < columnist.skip_probability(day, slot):
            logger.info(f"🛋️ [Publisher] Сегодня не пишется (настроение «{day['mood']}»): слот пропущен.")
            return
        personal = wd == 6 and random.random() < PERSONAL_SUNDAY_PROB

    digest = news_data.get("current_digest", {})
    sl = news_data.get("current_shortlist") or {}
    use_curator = False

    if is_vault:
        logger.info("🏛️ [Publisher] Сетка вещания (Пятница): рубрика «Вопрос из зрительного зала».")
    elif is_b2b:
        logger.info("🎬 [Publisher] Сетка вещания (Воскресенье): B2B-рубрика «Вскрытие кассы / Хроники Франкенштейна».")
    elif is_table:
        logger.info("🔪 [Publisher] Сетка вещания (Среда): рубрика «Монтажный стол: Было → Стало».")
    elif personal:
        logger.info("📝 [Publisher] Сегодня личная заметка вместо новости.")
    else:
        if wd == 2 and not any_forced:
            logger.info("ℹ️ [Publisher] Среда: банк «Монтажного стола» пока пуст — автоматический фоллбэк на новостной выпуск «Аппаратная».")
        # Куратор: Ари сам выбирает из свежего короткого списка (NEWS_CURATOR=on). Список старше 6 часов не берём.
        try:
            sl_fresh = datetime.now() - datetime.strptime(sl.get("date", ""), "%Y-%m-%d %H:%M:%S") < timedelta(hours=6)
        except ValueError:
            sl_fresh = False
        use_curator = (curator.CURATOR_ON and columnist.load_persona() is not None
                       and bool(sl.get("items")) and (sl_fresh or dry_run))
        if use_curator:
            logger.info(f"🧐 [Publisher] Ари сам листает ленту: в списке {len(sl['items'])} новостей.")
        else:
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
            curator_ctx = None
            if personal:
                # === Личная заметка: без новости, без шапки, без добивки ===
                logger.info("🧠 [Publisher] Аристарх пишет личную заметку...")
                channel_post, post_meta = await generate_personal(llm, history, poetry_mode=force_poetry or None)
            elif is_vault:
                # === Рубрика Пятницы «Вопрос из зрительного зала» из золотого фонда тем ===
                logger.info("🧠 [Publisher] Аристарх препарирует тему из золотого фонда (Пятница)...")
                vault_topic = topics_vault.peek_next_vault_topic() if dry_run else topics_vault.get_next_vault_topic()
                if not vault_topic:
                    logger.warning("⚠️ [Publisher] Все темы из фонда использованы!")
                    return

                core_beliefs = evolution_engine.get_current_beliefs()
                bot_uname = getattr(config, "BOT_USERNAME", "media_producer_bot")
                channel_post, post_meta = await generate_vault_post(
                    llm, rag_service, core_beliefs, vault_topic, cta_handle=bot_uname
                )
            elif is_b2b:
                # === Рубрика Воскресенья «Вскрытие кассы / Хроники Франкенштейна» ===
                logger.info("🧠 [Publisher] Аристарх пишет воскресную B2B-колонку...")
                b2b_topic = b2b_vault.peek_next_b2b_topic() if dry_run else b2b_vault.get_next_b2b_topic()
                if not b2b_topic:
                    logger.warning("⚠️ [Publisher] Все темы из B2B-фонда использованы!")
                    return

                core_beliefs = evolution_engine.get_current_beliefs()
                channel_post, post_meta = await generate_b2b_post(
                    llm, rag_service, core_beliefs, b2b_topic
                )
            elif is_table:
                # === Рубрика Среды «Монтажный стол: Было → Стало» ===
                logger.info("🧠 [Publisher] Аристарх препарирует пациента на Монтажном столе (Среда)...")
                patient_item = table_vault.peek_next_table_patient() if dry_run else table_vault.get_next_table_patient()
                if not patient_item:
                    logger.warning("⚠️ [Publisher] В банке Монтажного стола нет пациентов в очереди!")
                    return

                core_beliefs = evolution_engine.get_current_beliefs()
                bot_uname = getattr(config, "BOT_USERNAME", "media_producer_bot")
                channel_post, post_meta = await generate_table_post(
                    llm, rag_service, core_beliefs, patient_item, cta_handle=bot_uname
                )
            else:
                hook_line = return_of = None
                curator_ctx = None
                fmt_wanted = force_format
                if use_curator:
                    now_utc = datetime.now(timezone.utc)
                    cands = [dict(c) for c in sl["items"]]
                    back = curator.yesterday_story(history, curator.load_log(), news_data.get("digests_history", []), now_utc)
                    if back:
                        cands.append(back)
                    ratings = await curator.rate(llm, cands, history, now_utc)
                    for c, r in zip(cands, ratings):
                        logger.info(f"🧐 [Curator] {r['interest']:.0f}/10 @{c.get('channel')} «{c['text'][:60]}» | {r['hook'] or r['why_not']}")
                    chosen = curator.choose(cands, ratings)
                    if not chosen:
                        logger.info("🙅 [Curator] Ничего не зацепило: поста не будет.")
                        if not dry_run:
                            curator.log_decision(curator.decision_entry(now_utc, cands, ratings, None))
                        return
                    c, r = chosen
                    digest = {"date": now_utc.strftime("%Y-%m-%d %H:%M:%S"), "context": "curator",
                              "content": "(пересказа нет, читай оригинал)", "url": c["url"], "channel": c.get("channel"),
                              "original_text": c["text"]}
                    hook_line = r["hook"] or None
                    return_of = c.get("yesterday_post") if c.get("is_return") else None
                    if not fmt_wanted and r.get("format") in {f["id"] for f in POST_FORMATS}:
                        fmt_wanted = r["format"]
                    curator_ctx = (now_utc, cands, ratings, chosen)
                    logger.info(f"🎯 [Curator] Берёт @{c.get('channel')} ({r['interest']:.0f}/10): {hook_line}")

                # === ШАГ 4: Core Beliefs (фон для суждений, если нет файла личности) ===
                core_beliefs = evolution_engine.get_current_beliefs()
                logger.info(f"🧬 [Publisher] Загружено убеждений: {len(core_beliefs)}")

                # === Спарринг-партнёр: каверзный вопрос к новости ===
                prov_q = None
                try:
                    prov_q = await provocateur.formulate_question_to_aristarkh(digest)
                    logger.info(f"🥊 [Provocateur] Спарринг-вопрос: {prov_q}")
                except Exception as e:
                    logger.warning(f"⚠️ [Provocateur] Ошибка формулирования вопроса: {e}")

                # === ШАГ 5: Генерация поста (формат, тон, анти-клише, RAG, шапка со ссылкой) ===
                # Ссылка на бота в постах только при CHANNEL_BOT_CTA: по умолчанию канал - витрина личности
                cta_handle = f"@{(await bot.get_me()).username}" if BOT_CTA_ENABLED else None
                logger.info("🧠 [Publisher] Аристарх пишет пост...")
                channel_post, post_meta = await generate_post(
                    llm, rag_service, core_beliefs, digest, history, fmt_wanted, cta_handle=cta_handle,
                    poetry_mode=force_poetry or None, hook_line=hook_line, return_of=return_of,
                    provocateur_question=prov_q,
                )
                if curator_ctx:
                    post_meta["topic"] = curator_ctx[3][0].get("topic")
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
            followup_at = None if (personal or is_special_rubric) else schedule_followup(published_at)
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
                columnist.journal_add("personal", post_meta["text"], topic=post_meta.get("topic"))
            elif is_vault:
                columnist.journal_add("vault_question", post_meta["text"], topic=post_meta.get("title"))
            elif is_b2b:
                columnist.journal_add("b2b_sunday", post_meta["text"], topic=post_meta.get("title"))
            elif is_table:
                columnist.journal_add("table_surgery", post_meta["text"], topic=post_meta.get("title"))

            # === ШАГ 7: История постов и флаг published - в свежую копию файла под блокировкой ===
            def record(data: dict):
                data["post_history"] = append_history(data.get("post_history", []), post_meta)
                if not personal and not is_special_rubric and curator_ctx:
                    # Выбор куратора становится текущей новостью, и её ссылка больше не попадёт в ленту
                    chosen_digest = {**digest, "published": True, "published_at": published_at.isoformat(),
                                     "published_message_id": sent_message.message_id}
                    data["digests_history"] = (data.get("digests_history", []) + [chosen_digest])[-30:]
                    data["current_digest"] = chosen_digest
                    return
                if not personal and not is_special_rubric:
                    current = data.get("current_digest", {})
                    if current.get("url") == digest.get("url"):
                        current.update(published=True, published_at=published_at.isoformat(),
                                       published_message_id=sent_message.message_id)

            try:
                update_json(news_path, record)
                logger.info("📝 [Publisher] История постов сохранена.")
                if not personal and not is_special_rubric and curator_ctx:
                    curator.log_decision(curator.decision_entry(curator_ctx[0], curator_ctx[1], curator_ctx[2],
                                                                curator_ctx[3], sent_message.message_id))
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
    parser.add_argument("--vault", dest="force_vault", action="store_true", help="рубрика Пятницы «Вопрос из зрительного зала»")
    parser.add_argument("--b2b", dest="force_b2b", action="store_true", help="рубрика Воскресенья «Вскрытие кассы / Хроники Франкенштейна»")
    parser.add_argument("--table", dest="force_table", action="store_true", help="рубрика Среды «Монтажный стол: Было → Стало»")
    parser.add_argument("--news", dest="force_news", action="store_true", help="принудительно обычный новостной выпуск «Аппаратная»")
    parser.add_argument("--format", dest="force_format", help="принудительный формат поста (id из POST_FORMATS), для тестов")
    parser.add_argument("--personal", action="store_true", help="личная заметка вместо поста по новости")
    parser.add_argument("--no-jitter", action="store_true", help="публиковать сразу, без случайной паузы")
    parser.add_argument("--typo", action="store_true", help="опечатка обязательно (с --dry-run: показать пост с ней)")
    parser.add_argument("--poetry", action="store_true", help="цитата из стихов обязательно (нужен банк цитат)")
    args = parser.parse_args()
    asyncio.run(run_publisher(dry_run=args.dry_run, force_format=args.force_format,
                              force_personal=args.personal, force_vault=args.force_vault,
                              force_b2b=args.force_b2b, force_table=args.force_table,
                              force_news=args.force_news,
                              no_jitter=args.no_jitter, force_typo=args.typo, force_poetry=args.poetry))
