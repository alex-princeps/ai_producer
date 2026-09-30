import json
import os
import aiohttp
import asyncio
from datetime import datetime, timedelta
from aristarkh_core.config import config, logger
from aristarkh_core.assistant import AssistantService
from aristarkh_core.database import DatabaseService
from aristarkh_core.rag_chroma import RAGService

class PersonaEvolutionEngine:
    """
    Модуль еженедельной рефлексии. "Внутренний психоаналитик" Аристарха.
    Не работает с пользователями напрямую. Анализирует только ВЫСОКОЗНАЧИМЫЕ
    события и обновляет глобальные установки персонажа (не более 5 штук).
    """
    def __init__(self, api_key: str):
        self.api_key = api_key
        # [КРИТИЧЕСКИЙ ФИКС] Абсолютный путь — больше не зависит от cwd
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.core_beliefs_path = os.path.join(base_dir, "aristarkh_core_beliefs.json")
        self._init_beliefs_file()

    def _init_beliefs_file(self):
        if not os.path.exists(self.core_beliefs_path):
            initial_beliefs = {
                "beliefs": [
                    "Индустрия полна дилетантов, которые хотят легких денег, не понимая драматургии.",
                    "Я ценю жесткость и честность выше вежливой лжи."
                ],
                "last_evolution_date": datetime.utcnow().isoformat()
            }
            with open(self.core_beliefs_path, 'w', encoding='utf-8') as f:
                json.dump(initial_beliefs, f, ensure_ascii=False, indent=2)

    def get_current_beliefs(self) -> list:
        """Метод для main.py - забрать убеждения для инъекции в System Prompt."""
        try:
            with open(self.core_beliefs_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get("beliefs", [])
        except Exception:
            return []

    def save_new_beliefs(self, new_beliefs: list):
        with open(self.core_beliefs_path, 'w', encoding='utf-8') as f:
            json.dump({
                "beliefs": new_beliefs,
                "last_evolution_date": datetime.utcnow().isoformat()
            }, f, ensure_ascii=False, indent=2)

    async def run_weekly_evolution(self, db: DatabaseService, rag: RAGService):
        """
        Главный метод. Должен запускаться по cron раз в неделю (например, в воскресенье ночью).
        """
        logger.info("🧬 [ЭВОЛЮЦИЯ] Запуск еженедельной трансформации ядра личности Аристарха...")
        
        # 1. Забираем текущие убеждения (Ограничение: 5 штук)
        current_beliefs = self.get_current_beliefs()
        
        # 2. Ищем ТОЛЬКО ВАЖНЫЕ инсайты из БД
        high_impact_events = await self._get_high_impact_events(db)
        
        if not high_impact_events:
            logger.info("🧬 [ЭВОЛЮЦИЯ] За неделю не произошло ничего потрясающего. Личность стабильна.")
            return

        # 3. Синтез через LLM (Stage 2: Перестройка Ядра)
        async with aiohttp.ClientSession() as session:
            assistant = AssistantService(self.api_key)
            assistant.http_session = session
            
            new_beliefs = await self._synthesize_new_core(assistant, current_beliefs, high_impact_events)
            
            if new_beliefs:
                self.save_new_beliefs(new_beliefs)
                logger.info(f"🧬 [ЭВОЛЮЦИЯ] Личность обновлена! Новые установки:\n" + "\n".join(new_beliefs))

    async def _get_high_impact_events(self, db: DatabaseService) -> str:
        """
        Лезет в базу аналитики и достает только ночные инсайты (результаты работы worker_cron).
        Так мы игнорируем рутину (привет-пока) и берем только сухой экстракт опыта.
        """
        from sqlalchemy import select
        from aristarkh_core.database import AnalyticsEvent
        
        async with db.async_session() as session:
            last_week = datetime.utcnow() - timedelta(days=7)
            # Берем только логи ночной консолидации, в которых был добавлен инсайт
            stmt = select(AnalyticsEvent).where(
                AnalyticsEvent.event_type == "MEMORY_CONSOLIDATED",
                AnalyticsEvent.timestamp >= last_week
            )
            result = await session.execute(stmt)
            events = result.scalars().all()
            
            # Склеиваем данные в текстовый блок для LLM
            impact_text = "\n".join([ev.event_data for ev in events if ev.event_data])
            return impact_text[:30000]  # Ограничиваем размер

    async def _synthesize_new_core(self, assistant: AssistantService, current_beliefs: list, events_text: str) -> list:
        """
        Промпт, который заставляет LLM мутировать старые убеждения с учетом нового опыта,
        строго соблюдая лимит в 5 пунктов.
        """
        beliefs_str = "\n".join([f"- {b}" for b in current_beliefs])
        
        prompt = (
            "Ты — модуль когнитивной эволюции Аристарха Градова.\n"
            "Твоя задача — обновить фундаментальные установки личности (Core Beliefs) на основе "
            "важных событий прошедшей недели.\n\n"
            f"ТЕКУЩИЕ УБЕЖДЕНИЯ (ЕГО 'Я'):\n{beliefs_str}\n\n"
            f"ВЫЖИМКА ОПЫТА ЗА НЕДЕЛЮ:\n{events_text}\n\n"
            "ПРАВИЛА ИЗМЕНЕНИЯ (КРИТИЧЕСКИ ВАЖНО):\n"
            "1. У Аристарха может быть СТРОГО НЕ БОЛЕЕ 5 УБЕЖДЕНИЙ. Это жесткий лимит.\n"
            "2. Если новый опыт противоречит старым убеждениям, ИЗМЕНИ старое убеждение.\n"
            "3. Если опыт принес что-то совершенно новое, ДОБАВЬ новое убеждение, но УДАЛИ "
            "или ОБЪЕДИНИ наименее важные старые, чтобы итоговый список состоял максимум из 5 пунктов.\n"
            "4. Убеждения должны быть написаны от первого лица ('Я считаю...', 'Я понял...').\n\n"
            "ВЕРНИ ОТВЕТ СТРОГО В ФОРМАТЕ JSON:\n"
            "{\n"
            '  "updated_beliefs": ["убеждение 1", "убеждение 2", ... (до 5 штук)]\n'
            "}"
        )
        
        url = f"https://generativelanguage.googleapis.com/v1beta/{assistant.model}:generateContent?key={assistant.api_key}"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.3,
                "responseMimeType": "application/json"
            }
        }
        
        try:
            async with assistant.http_session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    text_resp = data['candidates'][0]['content']['parts'][0]['text']
                    
                    import re
                    match = re.search(r'\{.*\}', text_resp, re.DOTALL)
                    if match:
                        parsed = json.loads(match.group(0))
                        new_beliefs = parsed.get("updated_beliefs", [])
                        return new_beliefs[:5]  # Жестко обрубаем, если LLM ошиблась и дала больше 5
        except Exception as e:
            logger.error(f"❌ Ошибка синтеза эволюции: {e}")
            
        return []