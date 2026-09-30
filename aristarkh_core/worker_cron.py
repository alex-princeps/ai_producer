import asyncio
import os
import aiohttp
from datetime import datetime
from aristarkh_core.config import config, logger
from aristarkh_core.database import DatabaseService
from aristarkh_core.assistant import AssistantService
from semantic_memory import SemanticMemory
from aristarkh_core.rag_chroma import RAGService
from aristarkh_core.evolution_engine import PersonaEvolutionEngine  # [NEW] Импорт Эволюции Ядра

async def run_nightly_reflection():
    """
    Процесс 'Сна'. Пробегается по всем активным юзерам, 
    консолидирует их память за день и сбрасывает усталость Аристарха.
    По воскресеньям дополнительно запускает глобальную эволюцию ядра личности.
    """
    logger.info("🌙 Аристарх ложится спать. Запуск ночной рефлексии (Continuous Learning)...")
    
    # Инициализация сервисов
    db = DatabaseService(config.DB_URL)
    semantic_memory = SemanticMemory()
    rag_service = RAGService(config.GOOGLE_API_KEY)
    evolution_engine = PersonaEvolutionEngine(config.GOOGLE_API_KEY)  # [NEW] Инициализация эволюции
    
    # Единая HTTP сессия для Flash-модели
    async with aiohttp.ClientSession() as session:
        assistant = AssistantService(config.GOOGLE_API_KEY)
        assistant.http_session = session
        evolution_engine.http_session = session  # [NEW] Пробрасываем сессию для эволюции
        
        # 1. Получаем тех, кто общался с ботом сегодня
        active_users = await db.get_active_users_last_24h()
        logger.info(f"👥 Найдено активных пользователей за 24ч: {len(active_users)}")
        
        for user_id in active_users:
            logger.info(f"🔍 Рефлексия для юзера {user_id}...")
            
            # 2. Собираем диалог за день
            daily_transcript = await db.get_user_dialogue_last_24h(user_id)
            
            if len(daily_transcript) > 200: # Если было хотя бы пару осмысленных сообщений
                # 3. Синтез опыта через Flash-модель
                reflection_data = await assistant.run_daily_reflection(daily_transcript)
                
                if reflection_data:
                    new_facts = reflection_data.get("new_core_facts", [])
                    insight = reflection_data.get("relationship_insight", "")
                    
                    # 4. Сохраняем новые фундаментальные факты в Семантический Граф
                    if new_facts:
                        await semantic_memory.add_facts(user_id, new_facts)
                        logger.info(f"   🕸️ Добавлено {len(new_facts)} фундаментальных фактов в граф.")
                    
                    # 5. Инсайт закидываем в эпизодическую память с высоким эмоциональным весом
                    if insight:
                        await rag_service.add_episodic_memory(user_id, f"[НОЧНОЙ ИНСАЙТ]: {insight}", emotion_weight=1.0)
                        logger.info(f"   🧠 Записан ночной инсайт: {insight}")
            else:
                logger.info(f"   ⏩ Слишком короткий диалог, пропускаем рефлексию.")

            # 6. ВАЖНО: Физиологический "сон" Аристарха. 
            # Сбрасываем усталость и возвращаем нейтральное настроение на утро
            await db.reset_fatigue(user_id)
            
            # Пишем в аналитику, что рефлексия пройдена
            await db.log_event(user_id, "NIGHTLY_REFLECTION_COMPLETED", f"facts_added: {len(reflection_data.get('new_core_facts', [])) if 'reflection_data' in locals() and reflection_data else 0}")
            
            # Спим 2 секунды, чтобы не упереться в Rate Limits гугла (если юзеров много)
            await asyncio.sleep(2)
        
        # === ЕЖЕНЕДЕЛЬНАЯ ЭВОЛЮЦИЯ ЯДРА (ЗАПУСК ПО ВОСКРЕСЕНЬЯМ) ===
        # Раньше здесь стояло `if True:` — эволюция шла каждую ночь и раскачивала убеждения.
        # EVOLUTION_MODE: "sunday" (по умолчанию), "force" (запустить сейчас), "off" (не запускать).
        evolution_mode = os.getenv("EVOLUTION_MODE", "sunday")
        if evolution_mode == "force" or (evolution_mode == "sunday" and datetime.now().weekday() == 6):
            logger.info("📅 Сегодня воскресенье. Аристарх подводит итоги недели и эволюционирует...")
            try:
                await evolution_engine.run_weekly_evolution(db, rag_service)
                logger.info("🧬 Эволюция ядра завершена. Аристарх обновил свои глобальные убеждения.")
            except Exception as e:
                logger.error(f"❌ Ошибка еженедельной эволюции: {e}")
            
    logger.info("☀️ Рефлексия завершена. Аристарх проснулся, полон сил и новых знаний.")

if __name__ == "__main__":
    asyncio.run(run_nightly_reflection())