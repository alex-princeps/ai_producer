import os
import re
import time
import requests
import json
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

# Загружаем переменные окружения из .env файла бота
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# API ключ Gemini для выбора top-новости
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# t.me с VPS часто недоступен напрямую («No route to host»), через прокси — стабильно
PROXY_URL = (os.getenv("PROXY_URL") or "").strip().strip('"')
PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

# Список каналов для парсинга
CHANNELS_TO_PARSE = [
    "sncmag",           # Антиглянец — светская хроника
    "minaevlife",       # Минаев
    "tatlersheroine",   # Героиня Татлера
    "prbezposhady",     # Беспощадный пиарщик
    "ksbchk",           # Собчак
]

# Путь к JSON-файлу с архивом новостей
JSON_FILENAME = os.path.join(BASE_DIR, "tg_news.json")


def get_schedule_config() -> tuple[int, str]:
    """
    Определяет глубину парсинга в зависимости от времени запуска.
    
    Returns:
        tuple: (часы для парсинга, контекст запуска)
    """
    current_hour = datetime.now().hour
    
    if 5 <= current_hour < 11:
        return 14, "Утренний сбор"
    elif 11 <= current_hour < 16:
        return 5, "Дневной пульс"
    else:
        return 4, "Вечерний перехват"


def save_to_json(data: dict):
    """
    Сохраняет данные в JSON-файл, обновляя существующий архив.
    
    Args:
        data: словарь с данными для сохранения
    """
    try:
        # Загружаем старые данные, если файл существует
        if os.path.exists(JSON_FILENAME):
            with open(JSON_FILENAME, 'r', encoding='utf-8') as f:
                try:
                    old_data = json.load(f)
                except json.JSONDecodeError:
                    old_data = {}
        else:
            old_data = {}

        # Обработка digest (главная новость)
        if "digest" in data:
            # Сохраняем историю дайджестов
            if "digests_history" not in old_data:
                old_data["digests_history"] = []
            
            old_data["digests_history"].append(data["digest"])
            if len(old_data["digests_history"]) > 30:
                old_data["digests_history"] = old_data["digests_history"][-30:]
            
            old_data["current_digest"] = data["digest"]
        
        # Обновляем остальные данные
        old_data.update(data)
        
        # Сохраняем
        with open(JSON_FILENAME, 'w', encoding='utf-8') as f:
            json.dump(old_data, f, ensure_ascii=False, indent=2)
        
        print(f"💾 Данные сохранены в {JSON_FILENAME}")
    
    except Exception as e:
        print(f"❌ Ошибка при сохранении в JSON: {e}")


def get_recent_posts(channel_username: str, hours: int) -> list:
    """
    Парсит публичный Telegram-канал через веб-версию t.me/s/.
    
    Args:
        channel_username: имя канала без @
        hours: за сколько последних часов собирать посты
    
    Returns:
        list: список словарей с постами
    """
    url = f"https://t.me/s/{channel_username}"
    # [FIX] Оставляем полный User-Agent — Telegram может блокировать упрощённый
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    print(f"🔍 Парсинг канала: @{channel_username}...")
    
    try:
        response = requests.get(url, headers=headers, timeout=15, proxies=PROXIES)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"❌ Ошибка доступа к @{channel_username}: {e}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    messages = soup.find_all("div", class_="tgme_widget_message")
    
    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours)
    recent_posts = []

    for msg in messages:
        # Извлекаем текст
        text_div = msg.find("div", class_="tgme_widget_message_text")
        if not text_div:
            continue
        
        # Заменяем <br> на переносы строк
        for br in text_div.find_all("br"):
            br.replace_with("\n")
        
        # get_text(strip=True) склеивал предложения без пробелов («хуйню.Но»)
        text = re.sub(r"[ \t]+", " ", text_div.get_text()).strip()
        
        # Извлекаем время
        time_tag = msg.find("time", class_="time")
        if not time_tag or not time_tag.has_attr("datetime"):
            continue
        
        try:
            post_time = datetime.fromisoformat(time_tag["datetime"])
        except ValueError:
            continue
        
        # Фильтруем по времени
        if post_time >= cutoff_time:
            post_link = msg.get("data-post", f"{channel_username}")
            post_url = f"https://t.me/{post_link}"
            
            recent_posts.append({
                "text": text,
                "url": post_url,
                "datetime": post_time.strftime("%Y-%m-%d %H:%M:%S"),
                "channel": channel_username
            })
    
    print(f"  ✓ Собрано постов: {len(recent_posts)}")
    return recent_posts


def load_published_urls() -> set:
    """Ссылки на новости, которые уже комментировались (чтобы не выбрать повторно)."""
    if not os.path.exists(JSON_FILENAME):
        return set()
    try:
        with open(JSON_FILENAME, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return set()
    return {d.get("url") for d in data.get("digests_history", []) if d.get("url")}


def filter_top_news(all_posts: list):
    """
    Отправляет посты в Gemini Flash-Lite для выбора ОДНОЙ top-новости.

    Модель возвращает НОМЕР выбранного поста и краткую суть, а ссылку и
    оригинальный текст мы берём из распарсенного поста — так ссылка и цитата
    в посте Аристарха всегда настоящие, а не пересказанные моделью.

    Returns:
        dict: {"content", "url", "channel", "original_text"} или None
    """
    if not all_posts:
        return None

    if not GEMINI_API_KEY:
        print("❌ API ключ Gemini не найден")
        return None

    published = load_published_urls()
    candidates = [p for p in all_posts if p["url"] not in published]
    if not candidates:
        print("🤷‍♂️ Все свежие посты уже комментировались.")
        return None

    print(f"🧠 Выбор top-новости через Gemini Flash-Lite из {len(candidates)} постов...")

    from google import genai as google_genai
    from google.genai import types as genai_types
    client = google_genai.Client(api_key=GEMINI_API_KEY)

    combined_text = "\n\n".join(
        f"#{i} [{post['channel']}] ({post['datetime']}): {post['text'][:1500]}"
        for i, post in enumerate(candidates)
    )

    prompt = f"""
Ты — интеллектуальный фильтр новостей для медиа-продюсера.
Из этой ленты выбери РОВНО ОДНУ самую скандальную, хайповую или важную новость
про медиа, шоу-бизнес, телевидение, кино, глянец, блогеров или медиаполитику.

Лента новостей (у каждого поста номер #N):
{combined_text}

Верни JSON строго такого вида:
{{"index": <номер выбранного поста>, "summary": "<суть новости в 2-3 предложениях, без ссылок>"}}
"""

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash-lite',
            contents=prompt,
            config=genai_types.GenerateContentConfig(response_mime_type="application/json"),
        )
        choice = json.loads(response.text)
        idx = int(choice.get("index", -1))
        if not 0 <= idx < len(candidates):
            print(f"⚠️ Модель вернула неверный номер {idx}, берём первый пост")
            idx = 0
        post = candidates[idx]
        return {
            "content": (choice.get("summary") or post["text"][:500]).strip(),
            "url": post["url"],
            "channel": post["channel"],
            "original_text": post["text"],
        }
    except Exception as e:
        print(f"❌ Ошибка Gemini: {e}")
        return None


def main():
    """
    Главная функция пайплайна.
    1. Собирает посты из каналов
    2. Выбирает top-новость через Flash
    3. Сохраняет в tg_news.json с флагом published=False
    """
    print(f"🚀 Запуск сборщика новостей: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Проверка ключей
    if not GEMINI_API_KEY:
        print("❌ GEMINI_API_KEY не найден в .env")
        return
    
    # Определяем параметры сбора
    hours_to_fetch, run_context = get_schedule_config()
    print(f"⏰ Режим: {run_context} (глубина {hours_to_fetch}ч)")
    
    # 1. Сбор постов
    all_recent_posts = []
    channels_data = {}
    
    for channel in CHANNELS_TO_PARSE:
        posts = get_recent_posts(channel, hours=hours_to_fetch)
        all_recent_posts.extend(posts)
        
        if posts:
            channels_data[channel] = {
                "channel_name": f"@{channel}",
                "posts_count": len(posts),
                "posts": posts
            }
        
        # Пауза между каналами
        time.sleep(2)
    
    print(f"📊 Всего собрано постов: {len(all_recent_posts)}")
    
    if not all_recent_posts:
        print("🤷‍♂️ Новых постов нет.")
        return
    
    # 2. Сохраняем ВСЕ исходные данные в JSON
    current_date = datetime.now().strftime("%Y-%m-%d")
    current_time = datetime.now().strftime("%H:%M:%S")
    
    save_data = {
        "last_update": f"{current_date} {current_time}",
        "date": current_date,
        "time": current_time,
        "hours_covered": hours_to_fetch,
        "run_context": run_context,
        "channels_data": channels_data,
        "total_posts": len(all_recent_posts)
    }
    save_to_json(save_data)
    
    # 3. Выбираем top-новость через Flash
    top_news = filter_top_news(all_recent_posts)
    
    if not top_news:
        print("🤷‍♂️ Не удалось выбрать top-новость.")
        return
    
    # 4. Сохраняем top-новость в JSON (current_digest) с флагом для publisher_cron.py
    digest_data = {
        "digest": {
            "date": f"{current_date} {current_time}",
            "context": run_context,
            "content": top_news["content"],
            "url": top_news["url"],
            "channel": top_news["channel"],
            "original_text": top_news["original_text"],
            "published": False  # Флаг для publisher_cron.py
        }
    }
    save_to_json(digest_data)
    
    print("✅ Новость подготовлена. Ожидает запуска publisher_cron.py")
    print("🏁 Работа скрипта завершена.")


if __name__ == "__main__":
    main()