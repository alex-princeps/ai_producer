import aiohttp
import json
import re  # [NEW] Бронебойный парсер JSON — выкусываем чистый объект из маркдаун-обёрток LLM
from tenacity import retry, stop_after_attempt, wait_exponential
from aristarkh_core.config import config, logger
from aristarkh_core.prompts import Prompts

class AssistantService:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.model = config.ASSISTANT_MODEL_NAME
        self.http_session = None

        # [CRITICAL FIX] Глобальное отключение цензуры для ВСЕХ системных вызовов ассистента
        self.safety_settings = [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
        ]

    # === [NEW] БРОНЕБОЙНЫЙ ПАРСЕР JSON (ПЕРЕВАРИВАЕТ МАССИВЫ И МУСОР + ОБОРВАННЫЙ JSON) ===
    def _parse_json_safe(self, text: str) -> dict:
        """
        Гарантированно вытаскивает JSON-объект, переваривая:
        - markdown-обёртки (```json ... ```)
        - JSON-массивы [ { ... } ] (LLM любит их выдавать)
        - случайный мусор до/после JSON
        - оборванный JSON (когда LLM не успела закрыть скобки)
        """
        text = text.strip()
        # Снимаем markdown-обёртки
        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        
        # Пытаемся распарсить напрямую
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # Fallback: выкусываем первый JSON-объект через Regex
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                try:
                    parsed = json.loads(match.group(0))
                except json.JSONDecodeError:
                    # [CRITICAL FIX] JSON оборван — пробуем частичное восстановление
                    return self._recover_truncated_json(text)
            else:
                # [CRITICAL FIX] JSON оборван — пробуем частичное восстановление
                return self._recover_truncated_json(text)
        
        # Защита от «шизофрении списков» — LLM отдала массив вместо объекта
        if isinstance(parsed, list) and len(parsed) > 0:
            parsed = parsed[0]
            
        if isinstance(parsed, dict):
            return parsed
            
        return {}

    def _recover_truncated_json(self, text: str) -> dict:
        """
        Восстанавливает частично оборванный JSON.
        Выкусывает отдельные ключи (facts, summary, mood и т.д.),
        даже если объект не закрыт.
        """
        recovered = {}
        
        # Выкусываем facts (массив строк) — даже если он не закрыт
        facts_match = re.search(r'"facts"\s*:\s*\[(.*?)(?:\]|$)', text, re.DOTALL)
        if facts_match:
            facts_raw = facts_match.group(1)
            # Извлекаем все строки в кавычках из массива
            facts = re.findall(r'"([^"]*)"', facts_raw)
            recovered["facts"] = facts
        
        # Выкусываем summary (строка)
        summary_match = re.search(r'"summary"\s*:\s*"([^"]*)"', text, re.DOTALL)
        if summary_match:
            recovered["summary"] = summary_match.group(1)
        
        # Выкусываем trust_delta (число)
        trust_match = re.search(r'"trust_delta"\s*:\s*(-?\d+)', text)
        if trust_match:
            recovered["trust_delta"] = int(trust_match.group(1))
        
        # Выкусываем mood (строка)
        mood_match = re.search(r'"mood"\s*:\s*"([^"]*)"', text)
        if mood_match:
            recovered["mood"] = mood_match.group(1)
        
        # Выкусываем emotion_weight (число с плавающей точкой)
        emotion_match = re.search(r'"emotion_weight"\s*:\s*(\d+\.?\d*)', text)
        if emotion_match:
            recovered["emotion_weight"] = float(emotion_match.group(1))
        
        # Выкусываем new_core_facts (массив строк)
        facts_core_match = re.search(r'"new_core_facts"\s*:\s*\[(.*?)(?:\]|$)', text, re.DOTALL)
        if facts_core_match:
            facts_core_raw = facts_core_match.group(1)
            facts_core = re.findall(r'"([^"]*)"', facts_core_raw)
            recovered["new_core_facts"] = facts_core
        
        # Выкусываем relationship_insight (строка)
        insight_match = re.search(r'"relationship_insight"\s*:\s*"([^"]*)"', text, re.DOTALL)
        if insight_match:
            recovered["relationship_insight"] = insight_match.group(1)
        
        return recovered

    @retry(wait=wait_exponential(multiplier=1, min=1, max=5), stop=stop_after_attempt(2))
    async def generate_suggestion(self, history_context: list) -> str:
        """
        Легковесный метод. Берет историю, ВЫРЕЗАЕТ все тяжелые документы,
        собирает диалог в единый текстовый транскрипт и запрашивает подсказку.
        """
        if not history_context:
            return ""

        dialogue_transcript = "=== ТРАНСКРИПТ ДИАЛОГА (КЛИЕНТ И ПРОДЮСЕР АРИСТАРХ) ===\n"
        
        has_content = False
        for msg in history_context:
            role = msg.get('role', 'user')
            speaker = "КЛИЕНТ" if role == 'user' else "АРИСТАРХ"
            
            parts_text = ""
            for p in msg.get('parts', []):
                if 'text' in p:
                    text_content = p['text']
                    # Обрезаем длинные "простыни" файлов, оставляем только суть
                    if len(text_content) > 1500:
                        text_content = text_content[:1500] + "\n...[Текст документа сокращен]..."
                    parts_text += text_content + "\n"
            
            if parts_text.strip():
                dialogue_transcript += f"[{speaker}]: {parts_text.strip()}\n\n"
                has_content = True

        if not has_content:
            return ""

        # [FIX] Инъекция жесткого правила в самый конец промпта.
        dialogue_transcript += (
            "=== КОНЕЦ ТРАНСКРИПТА ===\n"
            "Опираясь на свой системный промпт (5 блоков), проанализируй этот диалог и предложи ровно 1 следующий меткий вопрос от лица КЛИЕНТА к АРИСТАРХУ.\n\n"
            "КРИТИЧЕСКИ ВАЖНО: Твой ответ должен состоять ТОЛЬКО из текста вопроса. "
            "Он ДОЛЖЕН начинаться со слова 'Аристарх,'. Никаких рассуждений, никаких 'Вот мой вариант'!"
        )

        url = f"https://generativelanguage.googleapis.com/v1beta/{self.model}:generateContent?key={self.api_key}"
        
        payload = {
            "systemInstruction": {"parts": [{"text": Prompts.ASSISTANT_SYS_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": dialogue_transcript}]}],
            "safetySettings": self.safety_settings,  # [CRITICAL FIX] Цензура отключена
            "generationConfig": {
                "temperature": 0.8,
                "maxOutputTokens": 4000 # [FIX] Бак токенов увеличен в 2 раза!
            }
        }

        session = self.http_session or aiohttp.ClientSession()
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    candidates = data.get('candidates', [])
                    if candidates:
                        parts = candidates[0].get('content', {}).get('parts', [])
                        suggestion_text = "\n".join([p.get('text', '') for p in parts]).strip()
                        
                        # Аппаратная зачистка мусора и форматирования
                        suggestion_text = suggestion_text.replace('**', '').replace('*', '')
                        
                        # Жестко обрубаем все, что идет ДО слова "Аристарх"
                        if "Аристарх," in suggestion_text:
                            suggestion_text = "Аристарх," + suggestion_text.split("Аристарх,", 1)[1]
                            
                        return suggestion_text.strip()
                else:
                    err = await resp.text()
                    logger.error(f"Assistant API Error: {resp.status} - {err}")
        except Exception as e:
            logger.error(f"Assistant Service Error: {e}")
        finally:
            if not self.http_session:
                await session.close()
                
        return ""

    # === [UPGRADED] КОГНИТИВНЫЙ ЭКСТРАКТОР ПАМЯТИ (БРОНЕБОЙНЫЙ PARSER + RECOVERY) ===
    
    async def extract_memory_and_state(self, history_chunk: list) -> dict:
        """
        Анализирует кусок диалога (ровно 10 реплик) и возвращает:
        - facts: список новых фактов о клиенте (Субъект -> Предикат -> Объект)
        - summary: краткая суть разговора для эпизодической памяти
        - trust_delta: как изменилось доверие Аристарха (-5..+5)
        - mood: новое настроение Аристарха
        - emotion_weight: насколько эмоционально значим диалог (0.1..1.0)
        """
        if not history_chunk: 
            return {}

        dialogue_transcript = "=== ФРАГМЕНТ ДИАЛОГА ===\n"
        for msg in history_chunk:
            role = "КЛИЕНТ" if msg.get('role') == 'user' else "АРИСТАРХ"
            text = "".join([p.get('text', '') for p in msg.get('parts', [])])
            dialogue_transcript += f"[{role}]: {text[:1000]}\n"

        prompt = (
            "Ты — невидимый когнитивный анализатор. Твоя задача — извлечь данные из диалога и вернуть их СТРОГО в формате JSON.\n\n"
            "ПРАВИЛА ЭКСТРАКЦИИ:\n"
            "1. facts: Только ВАЖНЫЕ долгосрочные факты (имена, возраст, хобби, боли, бизнес, питомцы). "
            "Формат строки: 'Субъект -> Предикат -> Объект'. Если ничего нового нет — верни пустой список [].\n"
            "2. summary: Выжимка сути разговора (1-2 предложения) для векторной памяти.\n"
            "3. trust_delta: Как изменилось отношение Аристарха к клиенту в этом фрагменте? "
            "Укажи число от -5 (если клиент тупил или хамил) до +5 (если был инсайт, конструктив, гениальная идея).\n"
            "4. mood: Укажи текущее настроение Аристарха. Одно из: 'neutral', 'sarcastic', 'angry', 'bored', 'intrigued'.\n"
            "5. emotion_weight: Насколько эмоционально значим этот кусок для будущих бесед? "
            "Число от 0.1 (скука, болтовня) до 1.0 (жесткий конфликт или прорывной брейншторм).\n\n"
            "ОТВЕТ ДОЛЖЕН БЫТЬ ТОЛЬКО JSON! Никаких рассуждений, никаких блоков markdown (```json).\n"
            + dialogue_transcript
        )

        url = f"https://generativelanguage.googleapis.com/v1beta/{self.model}:generateContent?key={self.api_key}"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "safetySettings": self.safety_settings,  # [CRITICAL FIX] Цензура отключена
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 4000,  # [CRITICAL FIX] Увеличен с 2000 — даём место закрыть JSON
                "responseMimeType": "application/json"
            }
        }

        session = self.http_session or aiohttp.ClientSession()
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    candidates = data.get('candidates', [])
                    if candidates:
                        text_resp = candidates[0].get('content', {}).get('parts', [{}])[0].get('text', '{}')
                        
                        # [CRITICAL FIX] Бронебойный парсер с восстановлением оборванного JSON
                        parsed_data = self._parse_json_safe(text_resp)
                        if parsed_data:
                            logger.debug(f"🧹 [Экстрактор] Извлечено полей: {list(parsed_data.keys())}")
                            return parsed_data
                            
                        # Логируем сломанный JSON в одну строку
                        clean_raw = text_resp.replace('\n', ' ')[:300]
                        logger.warning(f"⚠️ [Экстрактор] Сломанный JSON: {clean_raw}")
                        return {}
        except json.JSONDecodeError as e:
            logger.error(f"❌ Memory Extraction JSON Error: {e}")
        except Exception as e:
            logger.error(f"❌ Memory Extraction Error: {e}")
        finally:
            if not self.http_session: 
                await session.close()
            
        return {}

    # === [NEW] НОЧНАЯ РЕФЛЕКСИЯ — CONTINUOUS LEARNING (СОН АРИСТАРХА) ===
    
    async def run_daily_reflection(self, daily_transcript: str) -> dict:
        """
        Метод для ночного воркера worker_cron.py.
        Анализирует ВЕСЬ диалог пользователя и Аристарха за прошедшие 24 часа.
        
        Возвращает:
        - new_core_facts: список ФУНДАМЕНТАЛЬНЫХ фактов
        - relationship_insight: саммари динамики отношений за день
        """
        if not daily_transcript.strip():
            logger.warning("⚠️ [Ночная рефлексия] Пустой транскрипт, пропускаем")
            return {}

        prompt = (
            "Ты — когнитивный сопроцессор ИИ-Агента (ТВ-Продюсера Аристарха Градова).\n"
            "Твоя задача: проанализировать лог диалога с клиентом за прошедший день "
            "и произвести 'ночную консолидацию памяти'.\n\n"
            "Вытащи только ФУНДАМЕНТАЛЬНЫЕ вещи, которые изменят отношение к клиенту в будущем "
            "(смена профессии, запуск нового проекта, глубокие страхи, бюджеты, семья, "
            "ключевые боли, инсайты клиента о себе).\n\n"
            "ВЕРНИ СТРОГО JSON-ОБЪЕКТ (без обёрток ```json):\n"
            "{\n"
            '  "new_core_facts": ["Субъект -> Предикат -> Объект", ... (СТРОГО МАКСИМУМ 5 ФАКТОВ!)],\n'
            '  "relationship_insight": "Саммари динамики отношений за день (1-2 предложения). '
            'Например: Клиент осознал свои ошибки в монетизации, стал более открытым к критике, '
            'доверяет Аристарху больше. Или: Клиент агрессивно защищал свою непрофессиональную позицию, '
            'уровень доверия упал."\n'
            "}\n\n"
            "=== ЛОГ ДИАЛОГА ЗА ДЕНЬ ===\n"
            f"{daily_transcript[:25000]}"
        )

        url = f"https://generativelanguage.googleapis.com/v1beta/{self.model}:generateContent?key={self.api_key}"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "safetySettings": self.safety_settings,
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 8000,  # Уже большой — места хватает
                "responseMimeType": "application/json"
            }
        }

        session = self.http_session or aiohttp.ClientSession()
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    candidates = data.get('candidates', [])
                    if candidates:
                        text_resp = candidates[0].get('content', {}).get('parts', [{}])[0].get('text', '{}')
                        
                        # [CRITICAL FIX] Бронебойный парсер с восстановлением
                        reflection = self._parse_json_safe(text_resp)
                        if reflection:
                            logger.info(
                                f"🌙 [Ночная рефлексия] Извлечено фактов: {len(reflection.get('new_core_facts', []))}, "
                                f"инсайт: {reflection.get('relationship_insight', '')[:100]}..."
                            )
                            return reflection
                            
                        # Логируем сломанный JSON в одну строку
                        clean_raw = text_resp.replace('\n', ' ')[:300]
                        logger.warning(f"⚠️ [Ночная рефлексия] Сломанный JSON от LLM: {clean_raw}")
                        return {}
        except json.JSONDecodeError as e:
            logger.error(f"❌ [Ночная рефлексия] JSON Error: {e}")
        except Exception as e:
            logger.error(f"❌ [Ночная рефлексия] Error: {e}")
        finally:
            if not self.http_session: 
                await session.close()
                
        return {}