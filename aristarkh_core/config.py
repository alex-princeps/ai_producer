import os
import logging
from dotenv import load_dotenv

# [КРИТИЧЕСКИЙ ФИКС] Определяем жёсткий абсолютный путь к папке проекта
# Теперь неважно, откуда запущен скрипт — Cron из /root/, systemd, или руками.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Явно указываем путь к .env (он в корне проекта, на уровень выше aristarkh_core)
dotenv_path = os.path.join(os.path.dirname(BASE_DIR), '.env')
load_dotenv(dotenv_path)

# Логирование
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("AristarkhSystem")

class Config:
    def __init__(self):
        # Telegram
        self.BOT_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip().replace('"', '')
        self.MODEL_NAME = "models/gemini-3.1-pro-preview"
        # [FIX] Модель для теневого ассистента (дешёвая, быстрая, умная)
        self.ASSISTANT_MODEL_NAME = "models/gemini-2.5-flash"
        self.EMBEDDING_MODEL = "models/gemini-embedding-001"
        self.GOOGLE_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip().replace('"', '')

        # [КРИТИЧЕСКИЙ ФИКС] Жёстко привязываем БД и векторную базу к папке проекта
        # Раньше os.getcwd() зависел от того, откуда запущен скрипт (cron → /root/!)
        db_path = os.path.join(BASE_DIR, "aristarkh.db")
        self.DB_URL = os.getenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
        self.CHROMA_PATH = os.path.join(BASE_DIR, "chroma_db")

        # [FIX] Настройки Redis для FSM
        self.REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self.USE_REDIS = os.getenv("USE_REDIS", "false").lower() in ("true", "1", "yes")

        # [FIX] Настройки промокода
        self.PROMO_CODE = (os.getenv("PROMO_CODE") or "").strip()  # пусто = промокоды отключены
        self.PROMO_HOURS = int(os.getenv("PROMO_HOURS", "24"))

        # Admin ID
        self.ADMIN_ID = int(os.getenv("ADMIN_ID") or "0")  # задаётся только через .env

        # Defaults
        self.SESSION_NAME = 'aristarkh_agent'
        
        # Настройки буфера сообщений
        self.LONG_TEXT_THRESHOLD = 3500

        # [КРИТИЧЕСКИЙ ФИКС] Медиа-файлы — теперь с абсолютным путём
        self.PDF_PATH = os.path.join(BASE_DIR, "media", "ИИ-Продюсер_Аристарх Градов.pdf")
        self.AUDIO_PATH = os.path.join(BASE_DIR, "media", "aristarkh_podcast.mp3")
        
config = Config()