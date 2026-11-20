"""
Лёгкий детерминированный HTTP-парсер публичных Telegram-каналов через https://t.me/s/{username} (0 LLM-токенов).
Реализует Вариант 2А:
1. Отсеивает чужие репосты и sponsored-блоки по HTML-верстке.
2. Детерминированно фильтрует рекламные посевы (ФЗ-38: erid, ИНН, ОГРН, Реклама., #реклама, чужие @каналы + промокоды/подписки).
3. Пропускает пустые технические подписи (< 50 знаков).
4. Динамически отбирает от 5 (лонгриды) до 12 (короткие заметки 200-350 знаков) чистых авторских постов до бюджета ~5 000 знаков.
5. Выделяет 2 эталонных авторских поста (voice_samples) для <AUTHOR_VOICE_SAMPLES>.
"""
import html as html_lib
import os
import re
import aiohttp

from aristarkh_core.config import logger

TG_HANDLE_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?([a-zA-Z][a-zA-Z0-9_]{3,31})\b|"
    r"(?<![\w.])@([a-zA-Z][a-zA-Z0-9_]{3,31})\b"
)

IGNORED_HANDLES = {
    "media_producer_bot",
    "producer_gradov",
    "info_junk",
    "username",
    "channel",
    "bot",
    "telegram",
}

AD_LEGAL_RE = re.compile(
    r"(?i)(\berid\s*[:=]?|\bинн\s*\d{10,12}\b|\bогрн(?:ип)?\s*\d{13,15}\b|"
    r"(?:^|\n)\s*реклама[\s.:!]|#реклама\b|#партнерскийматериал\b|#промо\b|маркировка\s+рекламы)"
)

AD_SEED_CTA_RE = re.compile(
    r"(?i)(подписывай(?:тесь|ся)\s+на\s+(?:канал|блог)?|рекомендую\s+канал|"
    r"по\s+промокоду\s+[a-zа-яё0-9_-]{3,}|скидк\w+\s+\d{1,2}\s*%.*промокод|"
    r"забирайте\s+подарок\s+по\s+ссылке|переходите\s+в\s+канал)"
)

EXTERNAL_MENTION_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/([a-zA-Z][a-zA-Z0-9_]{3,31})|@([a-zA-Z][a-zA-Z0-9_]{3,31})"
)


def extract_tg_username(text: str | None) -> str | None:
    if not text:
        return None
    for m in TG_HANDLE_RE.finditer(text):
        handle = (m.group(1) or m.group(2) or "").strip()
        if handle and handle.lower() not in IGNORED_HANDLES:
            return handle
    return None


def _strip_html(raw: str) -> str:
    txt = re.sub(r"<br\s*/?>", "\n", raw or "", flags=re.IGNORECASE)
    txt = re.sub(r"</p>|</div>", "\n", txt, flags=re.IGNORECASE)
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = html_lib.unescape(txt)
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()


def is_ad_post(post_text: str, channel_username: str) -> bool:
    """Детерминированный детектор рекламного поста (0 токенов)."""
    if AD_LEGAL_RE.search(post_text):
        return True
    if AD_SEED_CTA_RE.search(post_text):
        own = channel_username.lower()
        for m in EXTERNAL_MENTION_RE.finditer(post_text):
            other = (m.group(1) or m.group(2) or "").lower()
            if other and other != own:
                return True
    return False


def parse_tg_public_html(page_html: str, username: str, target_chars: int = 5000) -> dict | None:
    """Парсит HTML страницы https://t.me/s/{username} и применяет 4-ступенчатый фильтр постов."""
    if not page_html or "tgme_channel_info" not in page_html:
        return None

    title_m = re.search(r'<div class="tgme_channel_info_header_title"[^>]*>(.*?)</div>', page_html, re.DOTALL)
    title = _strip_html(title_m.group(1)) if title_m else f"@{username}"

    subs_m = re.search(r'<div class="tgme_channel_info_counter"[^>]*>.*?<span class="counter_value">(.*?)</span>', page_html, re.DOTALL)
    subscribers = _strip_html(subs_m.group(1)) if subs_m else ""

    bio_m = re.search(r'<div class="tgme_channel_info_description"[^>]*>(.*?)</div>', page_html, re.DOTALL)
    bio = _strip_html(bio_m.group(1)) if bio_m else ""

    # Извлекаем все блоки сообщений со страницы (Telegram отдаёт последние ~20 постов)
    wraps = re.split(r'(?=<div class="tgme_widget_message_wrap\b)', page_html)[1:]
    if not wraps:
        return None

    clean_posts: list[str] = []
    ads_filtered = 0
    forwards_filtered = 0

    # Идём от самых свежих постов вглубь ленты
    for wrap in reversed(wraps):
        # Слой 1: отсев чужих репостов и sponsored
        if "tgme_widget_message_forwarded_from" in wrap or "tgme_widget_message_sponsored" in wrap:
            forwards_filtered += 1
            continue

        text_m = re.search(
            r'<div class="tgme_widget_message_text js-message_text"[^>]*>(.*?)</div>',
            wrap,
            re.DOTALL,
        )
        if not text_m:
            continue

        post_text = _strip_html(text_m.group(1))
        # Слой 3: отсев технических огрызков (< 50 знаков)
        if len(post_text) < 50:
            continue

        # Слой 2: отсев рекламных интеграций и посевов
        if is_ad_post(post_text, username):
            ads_filtered += 1
            continue

        clean_posts.append(post_text)

    # Слой 4: динамическое окно 5..12 чистых авторских постов до бюджета target_chars (~5 000 знаков)
    selected_posts: list[str] = []
    total_chars = 0
    for p in clean_posts:
        trimmed = p[:1100].rstrip()
        if len(selected_posts) >= 12:
            break
        if len(selected_posts) >= 5 and (total_chars + len(trimmed)) > target_chars:
            break
        selected_posts.append(trimmed)
        total_chars += len(trimmed)

    # Выделяем 2 наиболее характерных авторских поста (250..650 знаков) как слепок живого голоса
    voice_candidates = [p for p in selected_posts if 200 <= len(p) <= 700]
    if len(voice_candidates) < 2:
        voice_candidates = [p[:580] for p in selected_posts[:2]]
    voice_samples = [p[:580].strip() for p in voice_candidates[:2]]

    lines = [
        f"Публичный Telegram-канал: @{username} («{title}»" + (f", {subscribers} подписчиков" if subscribers else "") + ")",
    ]
    if bio:
        lines.append(f"Реальная текущая шапка (bio): «{bio[:300]}»")
    if ads_filtered:
        lines.append(f"Рекламная активность: в последних 20 постах найдено {ads_filtered} рекламных интеграций (исключены из анализа авторского стиля).")
    if selected_posts:
        lines.append(f"Выборка последних {len(selected_posts)} чистых авторских постов (без рекламы и репостов):")
        for idx, sp in enumerate(selected_posts, 1):
            compact = sp.replace("\n", " ")[:420]
            lines.append(f"  {idx}. {compact}")

    return {
        "username": username,
        "title": title,
        "subscribers": subscribers,
        "bio": bio,
        "posts_count": len(selected_posts),
        "ads_filtered_count": ads_filtered,
        "forwards_filtered_count": forwards_filtered,
        "posts": selected_posts,
        "voice_samples": voice_samples,
        "formatted_summary": "\n".join(lines),
    }


async def fetch_public_tg_channel_snapshot(
    username: str,
    http_session: aiohttp.ClientSession | None = None,
    timeout_sec: float = 5.0,
) -> dict | None:
    """Асинхронно забирает публичный срез https://t.me/s/{username} (с поддержкой PROXY_URL) с безопасным фоллбэком."""
    clean_u = (username or "").lstrip("@").strip()
    if not clean_u:
        return None
    url = f"https://t.me/s/{clean_u}"
    proxy_url = os.getenv("PROXY_URL") or None
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9",
    }
    own_session = http_session is None
    session = http_session or aiohttp.ClientSession()
    try:
        async with session.get(
            url,
            headers=headers,
            proxy=proxy_url,
            timeout=aiohttp.ClientTimeout(total=timeout_sec),
        ) as resp:
            if resp.status != 200:
                return None
            page_html = await resp.text()
            snap = parse_tg_public_html(page_html, clean_u)
            if snap:
                logger.info(
                    f"📡 [ChannelScanner] @{clean_u}: bio={len(snap['bio'])}ч, "
                    f"posts={snap['posts_count']}, ads_filtered={snap['ads_filtered_count']}"
                )
            return snap
    except Exception as e:
        logger.debug(f"📡 [ChannelScanner] Не удалось прочитать @{clean_u}: {e}")
        return None
    finally:
        if own_session:
            await session.close()
