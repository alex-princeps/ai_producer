import re
import aiohttp
import json
from tenacity import retry, stop_after_attempt, wait_exponential
from aristarkh_core.config import config, logger

class LLMService:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.model = config.MODEL_NAME
        self.http_session = None # [FIX] Инжектируем единую сессию снаружи

        # [CRITICAL FIX] Глобальное отключение цензуры для ВСЕХ генераций
        self.safety_settings = [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
        ]

    def _clean_html(self, text):
        text = re.sub(r'<style>.*?</style>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', '', text)
        return text.strip()

    # === [NEW] БРОНЕБОЙНЫЙ ПАРСЕР JSON (ДЛЯ MICRO-ROUTER И AGENDA) ===
    def _parse_json_safe(self, text: str) -> dict:
        """
        Гарантированно вытаскивает JSON-объект, переваривая:
        - markdown-обёртки (```json ... ```)
        - JSON-массивы [ { ... } ] (LLM любит их выдавать)
        - случайный мусор до/после JSON
        """
        text = text.strip()
        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                try:
                    parsed = json.loads(match.group(0))
                except json.JSONDecodeError:
                    return {}
            else:
                return {}
        
        # Защита от «шизофрении списков» — LLM отдала массив вместо объекта
        if isinstance(parsed, list) and len(parsed) > 0:
            parsed = parsed[0]
            
        if isinstance(parsed, dict):
            return parsed
            
        return {}

    # === [NEW] ШАГ 0: MICRO-ROUTER — АНАЛИЗИРУЕМ, НУЖЕН ЛИ ПОИСК ===
    async def analyze_search_need(self, user_query: str, history_context: list) -> dict:
        """
        Дешёвая Flash-модель решает, стоит ли тратить деньги на Grounding.
        Возвращает: {"needs_search": true/false, "query": "строка поиска"}
        """
        from aristarkh_core.prompts import Prompts
        
        dialogue = "=== КОНТЕКСТ ===\n"
        for msg in history_context[-4:]:
            role = "АРИСТАРХ" if msg.get("role") == "model" else "ПОЛЬЗОВАТЕЛЬ"
            text_val = msg.get("parts", [{}])[0].get("text", "")
            dialogue += f"[{role}]: {text_val[:500]}\n"
        dialogue += f"[ТЕКУЩИЙ ЗАПРОС]: {user_query}"

        url = f"https://generativelanguage.googleapis.com/v1beta/{config.ASSISTANT_MODEL_NAME}:generateContent?key={self.api_key}"
        payload = {
            "systemInstruction": {"parts": [{"text": Prompts.ROUTER_SYS_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": dialogue}]}],
            "safetySettings": self.safety_settings,
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 200,
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
                        result = self._parse_json_safe(text_resp)
                        if result:
                            logger.info(f"🔀 [Micro-Router] needs_search={result.get('needs_search')}, query='{result.get('query', '')}'")
                            return result
        except Exception as e:
            logger.error(f"Router Error: {e}")
        finally:
            if not self.http_session:
                await session.close()
        
        return {"needs_search": False, "query": ""}

    # === [NEW] ШАГ 0.5: EXECUTE SEARCH — ЧИСТЫЙ GROUNDING ===
    async def execute_grounding_search(self, search_query: str):
        """
        Выполняет поиск в интернете и возвращает сухую выжимку фактов.
        Отдельный вызов — не связан с монологом или финальным ответом.
        """
        url = f"https://generativelanguage.googleapis.com/v1beta/{config.ASSISTANT_MODEL_NAME}:generateContent?key={self.api_key}"
        
        prompt = (
            f"Ты поисковый агент 2026 года. Выполни поиск в интернете по запросу: '{search_query}'. "
            "Собери самые важные, актуальные факты, цифры, имена и даты. "
            "Верни сухую выжимку фактов без воды (1-2 абзаца). Если информации нет, так и скажи."
        )
        
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "tools": [{"googleSearch": {}}],
            "safetySettings": self.safety_settings,
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1000}
        }
        
        session = self.http_session or aiohttp.ClientSession()
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    candidates = data.get('candidates', [])
                    usage = data.get("usageMetadata", {})
                    if candidates:
                        text = candidates[0].get('content', {}).get('parts', [{}])[0].get('text', '')
                        tokens = usage.get("totalTokenCount", 0)
                        logger.info(f"🔍 [Grounding Search] Найдено фактов ({len(text)} символов, {tokens} токенов)")
                        return text, tokens
        except Exception as e:
            logger.error(f"Grounding Search Error: {e}")
        finally:
            if not self.http_session:
                await session.close()
            
        return "", 0

    # === [NEW] МЕТОДЫ УТРЕННЕЙ РУТИНЫ (PROACTIVE AGENDA) ===
    
    @retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(3))
    async def scout_morning_news(self):
        from aristarkh_core.prompts import Prompts
        url = f"https://generativelanguage.googleapis.com/v1beta/{self.model}:generateContent?key={self.api_key}"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": Prompts.MORNING_NEWS_PROMPT}]}],
            "tools": [{"googleSearch": {}}],
            "safetySettings": self.safety_settings,
            "generationConfig": {"temperature": 0.4, "maxOutputTokens": 1024}
        }
        
        session = self.http_session or aiohttp.ClientSession()
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    text = self._parse_response(data)
                    usage = data.get("usageMetadata", {})
                    if text:
                        logger.info(f"📰 [Утренняя рутина] Найдена новость ({len(text)} символов)")
                        return text, usage
                    else:
                        logger.warning("⚠️ [News Scout] Получен пустой ответ, идем на ретрай...")
                        raise ValueError("Empty news text returned")
                else:
                    err = await resp.text()
                    logger.error(f"News Scout API Error: {resp.status} - {err}")
                    raise ValueError(f"News Scout API Error: {resp.status}")
        finally:
            if not self.http_session:
                await session.close()

    @retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(3))
    async def generate_daily_agenda(self, news: str, core_beliefs: list):
        from aristarkh_core.prompts import Prompts
        url = f"https://generativelanguage.googleapis.com/v1beta/{self.model}:generateContent?key={self.api_key}"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": Prompts.get_agenda_prompt(news, core_beliefs)}]}],
            "safetySettings": self.safety_settings,
            "generationConfig": {
                "temperature": 0.6,
                "maxOutputTokens": 2048
                # [КРИТИЧЕСКИЙ ФИКС] Убрали responseMimeType, чтобы Google не обрывал текст!
                # Увеличили maxOutputTokens с 1024 до 2048 — даём место для полного JSON
            }
        }
        
        session = self.http_session or aiohttp.ClientSession()
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    candidates = data.get('candidates', [])
                    usage = data.get("usageMetadata", {})
                    
                    if candidates:
                        content = candidates[0].get('content', {})
                        if not content:
                            raise ValueError("Empty content from API")
                            
                        text_resp = content.get('parts', [{}])[0].get('text', '{}')
                        agenda = self._parse_json_safe(text_resp)
                        
                        # Проверяем, что JSON не только распарсился, но и содержит главный ключ
                        if agenda and "daily_thesis" in agenda:
                            logger.info(f"📋 [Утренняя рутина] Повестка дня сформирована: {agenda.get('focus_topic', 'N/A')}")
                            return agenda, usage
                        else:
                            # Логируем сломанный кусок и выбрасываем ошибку, чтобы @retry перезапустил процесс
                            logger.warning(f"⚠️ [Agenda] Оборванный JSON. Идем на ретрай. Raw text:\n{text_resp}")
                            raise ValueError("Broken JSON in Agenda")
                else:
                    err = await resp.text()
                    logger.error(f"Agenda API Error: {resp.status} - {err}")
                    raise ValueError(f"API Error {resp.status}")
        finally:
            if not self.http_session:
                await session.close()

    # === [NEW] МЕТОД ГЕНЕРАЦИИ АВТОРСКИХ ПОСТОВ ДЛЯ КАНАЛА (С RAG-БАЗОЙ!) ===
    @retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(3))
    async def generate_standalone(self, system_prompt: str, user_query: str, rag_context: str = "") -> str:
        """
        Изолированный метод для написания постов в канал DeusExMedia.
        
        БЕЗ:
        - Истории диалога
        - Локации (ContextSimulator)
        - Усталости (fatigue)
        - Памяти о пользователе
        
        НО С:
        - RAG-базой (профессиональные лекции) — обязательный профессиональный бэкграунд!
        
        Это позволяет Аристарху быть собой (циничным экспертом), но не тащить
        в пост контекст личных переписок с юзерами.
        """
        # [CRITICAL FIX] Вставляем RAG-базу в systemInstruction
        memory_block = ""
        if rag_context:
            memory_block = (
                f"\n\n[ТВОЯ ПРОФЕССИОНАЛЬНАЯ БАЗА ЗНАНИЙ — ЛЕКЦИИ И ОПЫТ]\n"
                f"{rag_context}\n\n"
                f"Опирайся на эти лекции, методологию и опыт при написании поста!"
            )
        
        final_system_prompt = system_prompt + memory_block
        
        url = f"https://generativelanguage.googleapis.com/v1beta/{self.model}:generateContent?key={self.api_key}"
        payload = {
            "systemInstruction": {"parts": [{"text": final_system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_query}]}],
            "safetySettings": self.safety_settings,
            "generationConfig": {
                "temperature": 0.8,
                "maxOutputTokens": 4096
            }
        }
        
        session = self.http_session or aiohttp.ClientSession()
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    text = self._parse_response(data)
                    usage = data.get("usageMetadata", {})
                    if text:
                        logger.info(f"📝 [Standalone] Сгенерирован пост для канала (Токенов: {usage.get('totalTokenCount', 0)})")
                        return text
                    return "⛔️ Пустой ответ"
                else:
                    err = await resp.text()
                    logger.error(f"Standalone API Error: {resp.status} - {err}")
                    raise ValueError(f"API Error {resp.status}")
        except Exception as e:
            logger.error(f"Standalone generation error: {e}")
            raise
        finally:
            if not self.http_session:
                await session.close()

    # === [CRITICAL FIX] МЕТОД ГЕНЕРАЦИИ ВНУТРЕННЕГО МОНОЛОГА — XML ПАРСЕР С ТОЛЕРАНТНОСТЬЮ К ОБРЫВАМ ===
    @retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(2))
    async def generate_monologue(self, system_prompt, user_query, episodic_context, semantic_context, history_context, search_results=""):
        """
        Генерирует внутренний монолог Аристарха в XML-формате.
        [CRITICAL FIX] Парсер толерантен к незакрытым тегам:
        - Если </THOUGHTS> отсутствует — выкусываем всё до </STRATEGY> или конца текста.
        - Если </STRATEGY> отсутствует — выкусываем всё до конца текста.
        Кавычки, ёлочки, переносы строк НЕ ломают синтаксис.
        """
        contents_array = history_context.copy() if history_context else []
        
        memory_block = "\n\n[ТВОЯ ВНУТРЕННЯЯ ПАМЯТЬ (ЮЗЕР ЭТОГО НЕ ПИСАЛ)]\n"
        if semantic_context:
            memory_block += f"=== ФАКТЫ О НЕМ ===\n{semantic_context}\n"
        if episodic_context:
            memory_block += f"=== ПРОШЛЫЕ РАЗГОВОРЫ ===\n{episodic_context}\n"
        if search_results:
            memory_block += f"=== СВЕЖИЕ ФАКТЫ ИЗ ИНТЕРНЕТА ===\n{search_results}\n"
            
        final_system_prompt = system_prompt + memory_block

        current_request_text = user_query
        # [CRITICAL FIX] Инъекция задачи просит XML-теги
        current_request_text += "\n\n[СИСТЕМНАЯ ЗАДАЧА ДЛЯ ЭТОГО ШАГА: Проанализируй этот запрос в контексте диалога и верни свои скрытые мысли СТРОГО в XML тегах <THOUGHTS> и <STRATEGY>. Отвечай только разметкой, никаких лишних разговоров.]"
        
        contents_array.append({"role": "user", "parts": [{"text": current_request_text}]})
        
        url = f"https://generativelanguage.googleapis.com/v1beta/{self.model}:generateContent?key={self.api_key}"
        
        payload = {
            "systemInstruction": {"parts": [{"text": final_system_prompt}]},
            "contents": contents_array,
            "safetySettings": self.safety_settings,
            "generationConfig": {
                "temperature": 0.8,
                "maxOutputTokens": 2048  # [CRITICAL FIX] Увеличен с 1024 — даём модели место закрыть теги
            }
        }
        
        session = self.http_session or aiohttp.ClientSession()
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    candidates = data.get('candidates', [])
                    usage = data.get("usageMetadata", {})
                    tokens = usage.get("totalTokenCount", 0)
                    
                    if candidates:
                        text_resp = candidates[0].get('content', {}).get('parts', [{}])[0].get('text', '')
                        
                        # [CRITICAL FIX] Толерантный XML-парсер.
                        thoughts_match = re.search(
                            r'<THOUGHTS>(.*?)(?:</THOUGHTS>|<STRATEGY>|$)',
                            text_resp,
                            re.DOTALL | re.IGNORECASE
                        )
                        strategy_match = re.search(
                            r'<STRATEGY>(.*?)(?:</STRATEGY>|$)',
                            text_resp,
                            re.DOTALL | re.IGNORECASE
                        )
                        
                        thoughts = thoughts_match.group(1).strip() if thoughts_match else ""
                        strategy = strategy_match.group(1).strip() if strategy_match else ""
                        
                        if thoughts or strategy:
                            if not strategy:
                                strategy = "Без чёткого плана, действовать по ситуации."
                            if not thoughts:
                                thoughts = "Мыслей нет, но раздражение присутствует."
                            
                            final_monologue = f"МЫСЛИ: {thoughts}\nПЛАН ДЕЙСТВИЙ: {strategy}"
                            logger.info(f"🧠 [Монолог] Успешно сгенерирован (Токенов: {tokens})")
                            return final_monologue
                        
                        clean_raw = text_resp.replace('\n', ' ')[:500]
                        logger.warning(f"⚠️ [Монолог] Не удалось найти XML теги вообще. Raw text: {clean_raw}")
                        return "Мыслей нет, просто отвечаю."
                        
                    return "Мыслей нет, просто отвечаю."
                else:
                    err = await resp.text()
                    logger.error(f"Monologue API Error: {resp.status} - {err}")
                    return "Сбой внутреннего монолога."
        except json.JSONDecodeError as e:
            logger.error(f"Monologue JSON Error: {e}. Raw: {text_resp[:200]}")
            return "Ошибка парсинга мыслей."
        except Exception as e:
            logger.error(f"Monologue generation error: {e}")
            return "Ошибка генерации мыслей."
        finally:
            if not self.http_session:
                await session.close()

    @retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(3))
    async def generate(self, system_prompt, user_query, rag_context, episodic_context, semantic_context, history_context, temp, image_data=None, search_results=""):
        contents_array = history_context.copy() if history_context else []

        memory_block = "\n\n[ТВОЯ ПАМЯТЬ И БАЗА ЗНАНИЙ]\n"
        if semantic_context:
            memory_block += f"=== ФАКТЫ О ПОЛЬЗОВАТЕЛЕ ===\n{semantic_context}\n"
        if episodic_context:
            memory_block += f"=== ПРОШЛЫЕ РАЗГОВОРЫ ===\n{episodic_context}\n"
        if rag_context:
            memory_block += f"=== БАЗА ЗНАНИЙ ===\n{rag_context}\n"
        if search_results:
            memory_block += f"=== СВЕЖИЕ ФАКТЫ ИЗ ИНТЕРНЕТА ===\n{search_results}\n"

        final_system_prompt = system_prompt + memory_block

        user_parts = [{"text": user_query}]
        if image_data:
            user_parts.append({
                "inlineData": {
                    "mimeType": image_data['mime_type'],
                    "data": image_data['data']
                }
            })

        contents_array.append({"role": "user", "parts": user_parts})

        url = f"https://generativelanguage.googleapis.com/v1beta/{self.model}:generateContent?key={self.api_key}"

        payload = {
            "systemInstruction": {"parts": [{"text": final_system_prompt}]},
            "contents": contents_array,
            "safetySettings": self.safety_settings,
            "generationConfig": {
                "temperature": temp,
                "maxOutputTokens": 8192
            }
        }

        session = self.http_session or aiohttp.ClientSession()
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    usage = data.get("usageMetadata", {})
                    tokens = usage.get("totalTokenCount", 0)
                    text = self._parse_response(data)
                    if text:
                        logger.info(f"📤 [Генерация] Финальный ответ готов (Токенов: {tokens})")
                        return text
                    return "⛔️ Пустой ответ"
                else:
                    err = await resp.text()
                    logger.error(f"LLM API Error: {resp.status} - {err}")
                    return "⚠️ Аристарх временно недоступен (API Error)."
        except Exception as e:
            logger.error(f"Generate error: {e}")
            return "⚠️ Аристарх временно недоступен."
        finally:
            if not self.http_session:
                await session.close()

    def _parse_response(self, raw_response):
        candidates = raw_response.get('candidates', [])
        if not candidates: return None
        
        finish_reason = candidates[0].get('finishReason')
        if finish_reason == 'SAFETY': return "⛔️ Цензура Google заблокировала этот ответ."
        
        parts = candidates[0].get('content', {}).get('parts', [])
        full_text = self._clean_html("\n".join([p.get('text', '') for p in parts]))
        
        grounding = candidates[0].get('groundingMetadata')
        if grounding and grounding.get('searchEntryPoint'): 
            full_text += "\n\n_(🔎 Фактчекинг выполнен)_"
            
        return full_text