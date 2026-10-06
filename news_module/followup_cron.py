"""
«Добивка»: Аристарх отвечает на собственный пост в канале коротким продолжением.

Время добивки решается при публикации (publisher_cron.py → post_history[].followup_at).
Этот скрипт запускается по cron раз в час, находит самую старую «созревшую» добивку и публикует
её ответом (reply) на исходный пост. За один запуск — максимум одна добивка.
Добивки к постам из другого канала (в том числе опубликованным до смены TG_CHANNEL_ID) пропускаются.

Ещё здесь страховка опечаток: если публикатор не смог исправить опечатку в посте (сбой сети,
перезапуск сервера), правка повторяется, как только посту больше TYPO_REPAIR_AFTER_MIN минут.

Перед отправкой — случайная пауза 0..FOLLOWUP_JITTER_MIN минут, чтобы добивки не выходили ровно в :50.
Файл tg_news.json читается и пишется под блокировкой (state_io): публикатор может писать его одновременно.

--dry-run: берёт последний пост (даже если добивка не запланирована), печатает текст, ничего не шлёт.
"""

import argparse
import asyncio
import os
import random
from datetime import datetime, timedelta, timezone

import aiohttp
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import ReplyParameters

from aristarkh_core.config import config, logger
from aristarkh_core.gemini import LLMService
from news_module import typos
from news_module.post_builder import generate_followup
from news_module.state_io import read_json, update_json

CHANNEL_ID = os.getenv("TG_CHANNEL_ID", "")
NEWS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tg_news.json")
try:
    FOLLOWUP_JITTER_MIN = float(os.getenv("FOLLOWUP_JITTER_MIN", "") or 0)
except ValueError:
    FOLLOWUP_JITTER_MIN = 0.0


def _update_post(message_id: int, **fields):
    """Обновить запись поста в свежей копии tg_news.json под блокировкой."""
    def mutate(data: dict):
        for h in data.get("post_history", []):
            if h.get("message_id") == message_id and h.get("channel") == CHANNEL_ID:
                for key, value in fields.items():
                    h[key] = value(h) if callable(value) else value
    update_json(NEWS_PATH, mutate)


TYPO_REPAIR_AFTER_MIN = 40  # публикатор правит опечатку за 2-25 минут; позже - значит, не смог


def _bot() -> Bot:
    proxy_url = os.getenv("PROXY_URL")
    session = AiohttpSession(timeout=120, proxy=proxy_url) if proxy_url else AiohttpSession(timeout=120)
    return Bot(token=config.BOT_TOKEN, session=session)


async def repair_typos(now: datetime) -> None:
    """Неисправленные опечатки старше TYPO_REPAIR_AFTER_MIN минут: правим пост, до трёх попыток на пост."""
    stale = [
        h for h in read_json(NEWS_PATH).get("post_history", [])
        if h.get("channel") == CHANNEL_ID and h.get("message_id") and isinstance(h.get("typo"), dict)
        and not h["typo"].get("fixed") and h["typo"].get("fix_html") and h["typo"].get("repair_attempts", 0) < 3
        and h.get("published_at")
        and now - datetime.fromisoformat(h["published_at"]) > timedelta(minutes=TYPO_REPAIR_AFTER_MIN)
    ]
    if not stale:
        return
    bot = _bot()
    try:
        for h in stale:
            if await typos.edit_back(bot, CHANNEL_ID, h["message_id"], h["typo"]["fix_html"]):
                typos.mark_typo_fixed(NEWS_PATH, CHANNEL_ID, h["message_id"])
                logger.info(f"✅ [Followup] Опечатка в посте {h['message_id']} исправлена со второй попытки.")
            else:
                _update_post(h["message_id"], typo=lambda e: {**e["typo"], "repair_attempts": e["typo"].get("repair_attempts", 0) + 1})
                logger.error(f"❌ [Followup] Опечатку в посте {h['message_id']} исправить не удалось.")
    finally:
        await bot.session.close()


def _due_post(history: list[dict], now: datetime, dry_run: bool, channel: str = CHANNEL_ID):
    if dry_run:
        return history[-1] if history else None
    due = [
        h for h in history
        if h.get("channel") == channel
        and h.get("followup_at") and not h.get("followup_done") and h.get("message_id")
        and h.get("followup_attempts", 0) < 3
        and datetime.fromisoformat(h["followup_at"]) <= now
    ]
    return min(due, key=lambda h: h["followup_at"]) if due else None


async def run_followup(dry_run: bool = False) -> None:
    if not CHANNEL_ID and not dry_run:
        logger.error("❌ [Followup] TG_CHANNEL_ID не задан в .env.")
        return
    if not os.path.exists(NEWS_PATH):
        return
    if not dry_run:
        try:
            await repair_typos(datetime.now(timezone.utc))
        except Exception as e:
            logger.error(f"❌ [Followup] Проверка опечаток не удалась: {e}")
    history = read_json(NEWS_PATH).get("post_history", [])
    post = _due_post(history, datetime.now(timezone.utc), dry_run)
    if not post:
        logger.info("⏩ [Followup] Созревших добивок нет.")
        return

    if not dry_run:
        # Счётчик попыток сохраняем заранее: при сбое генерации или отправки не будем бесконечно повторять
        _update_post(post["message_id"], followup_attempts=lambda h: h.get("followup_attempts", 0) + 1)
        if FOLLOWUP_JITTER_MIN > 0:
            delay = random.uniform(0, FOLLOWUP_JITTER_MIN) * 60
            logger.info(f"⏳ [Followup] Пауза {delay / 60:.0f} мин перед добивкой.")
            await asyncio.sleep(delay)

    now = datetime.now(timezone.utc)
    published = datetime.fromisoformat(post["published_at"]) if post.get("published_at") else now
    minutes_since = max(1, int((now - published).total_seconds() // 60))

    async with aiohttp.ClientSession() as session:
        llm = LLMService(config.GOOGLE_API_KEY)
        llm.http_session = session
        text = await generate_followup(llm, post, minutes_since)

    if dry_run:
        print(f"\n[DRY RUN] Добивка к посту {post.get('message_id')} (через {minutes_since} мин):\n{text}\n")
        return

    bot = _bot()
    try:
        sent = await bot.send_message(
            CHANNEL_ID, text, reply_parameters=ReplyParameters(message_id=post["message_id"])
        )
        logger.info(f"✅ [Followup] Добивка опубликована: {sent.message_id} → ответ на {post['message_id']}")
    finally:
        await bot.session.close()

    _update_post(post["message_id"], followup_done=True, followup_text=text, followup_message_id=sent.message_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Добивка к собственному посту в канале")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(run_followup(dry_run=args.dry_run))
