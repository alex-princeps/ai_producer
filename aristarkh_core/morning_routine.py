import asyncio
import aiohttp
import json
import os
from datetime import datetime, timezone
from aristarkh_core.config import config, logger
from aristarkh_core.database import DatabaseService
from aristarkh_core.gemini import LLMService
from aristarkh_core.evolution_engine import PersonaEvolutionEngine

async def run_morning_routine():
    """
    Утренняя рутина Аристарха (БЕЗ GOOGLE SEARCH).
    1. Читает новость из файла tg_news.json (результат внешнего парсера).
    2. Анализирует новость через призму своих убеждений (Эволюция Ядра).
    3. Создает фоновую Повестку на День (Agenda).
    4. Сохраняет в aristarkh_agenda.json.
    """
    logger.info("🌅 Аристарх проснулся. Начинаем утреннее планирование...")
    
    db = DatabaseService(config.DB_URL)
    evolution_engine = PersonaEvolutionEngine(config.GOOGLE_API_KEY)
    
    base_dir = os.path.dirname(os.path.abspath(__file__))
    agenda_path = os.path.join(base_dir, "aristarkh_agenda.json")
    news_path = os.path.join(base_dir, "tg_news.json")
    
    SYSTEM_ID = 0 
    
    # [CRITICAL FIX A] Читаем новость из локального парсера, а не из интернета
    news_text = ""
    if os.path.exists(news_path):
        try:
            with open(news_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                digest = data.get("current_digest", {})
                if isinstance(digest, dict):
                    news_text = digest.get("content", "")
                elif isinstance(digest, str):
                    news_text = digest
        except Exception as e:
            logger.error(f"Ошибка чтения tg_news.json: {e}")
            
    if not news_text:
        news_text = "Индустрия стагнирует. Громких новостей за ночь не появилось, сплошная рутина."
        logger.info("⚠️ Файл с новостями пуст или отсутствует. Используем базовую заглушку для повестки.")
    else:
        logger.info(f"📰 Прочитана новость из парсера: {news_text[:150]}...")

    async with aiohttp.ClientSession() as session:
        llm = LLMService(config.GOOGLE_API_KEY)
        llm.http_session = session
        evolution_engine.http_session = session
        
        core_beliefs = evolution_engine.get_current_beliefs()
        logger.info(f"🧬 Загружено убеждений: {len(core_beliefs)}")
        
        logger.info("🧠 Формируем фоновый фильтр на день...")
        agenda_data, agenda_tokens = await llm.generate_daily_agenda(news_text, core_beliefs)
        
        if not agenda_data:
            logger.warning("⚠️ Не удалось распарсить повестку от LLM. Включаем План Б (Резервная повестка).")
            agenda_data = {
                "focus_topic": "Деградация рынка",
                "daily_thesis": "Все корпорации скупают остатки контента, потому что сами производить ничего не могут.",
                "conversational_agenda": "При любом упоминании медиа или новостей — жаловаться на засилье корпоратов.",
                "agenda_strength": 6
            }
            await db.log_event(SYSTEM_ID, "MORNING_ROUTINE_FALLBACK", "Used hardcoded agenda.")
        
        strength = agenda_data.get('agenda_strength', 'N/A')
        logger.info(f"📋 Повестка сформирована. Тема: '{agenda_data.get('focus_topic')}', Сила: {strength}/10")
        
        agenda_data["news"] = news_text
        agenda_data["date"] = datetime.now(timezone.utc).isoformat()
        
        with open(agenda_path, 'w', encoding='utf-8') as f:
            json.dump(agenda_data, f, ensure_ascii=False, indent=2)
            
        logger.info(f"✅ План на день сохранён в {agenda_path}")
        await db.log_event(SYSTEM_ID, "MORNING_AGENDA_GENERATED", json.dumps({"agenda": agenda_data}, ensure_ascii=False))

if __name__ == "__main__":
    asyncio.run(run_morning_routine())