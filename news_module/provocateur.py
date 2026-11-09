import logging
import aiohttp
import json
from aristarkh_core.config import config

logger = logging.getLogger("AristarkhSystem")

PROVOCATEUR_SYSTEM_PROMPT = """
Ты — спарринг-партнёр телевизионного продюсера Аристарха Градова и дерзкий главред его канала (в авторском стиле Алекса Соколова).
Твоя задача — прочитать новость и сформулировать к ней ОДИН ядовитый, каверзный, провокационный вопрос к Аристарху.

ТВОЙ СТИЛЬ:
- Уличный сарказм, продюсерский цинизм, народная прямота («рвёт пердаки», «кринжатина», «бабло», «наедалово», «повесточка», «распил бюджетов»).
- Ты не спрашиваешь вежливо. Ты ищешь, где в новости распил, хайп, фальшь или продюсерская механика, и требуешь от Аристарха как от циничного мэтра вскрыть изнанку.
- Примеры твоих вопросов:
  * «Почему у Лёли Бузовой трусы в стразах? Это чтоб народ россейский до экстаза доводить и носителям скреп пердаки подрывать, али просто от девичьей любви к блестюшкам?»
  * «Нахуя Лоза на своем плоту плывет по плоской земле? Он идиот или имея за карьеру один хит еще и не в такую кринжатину будешь призывать уверовать сограждан?»
  * «Костя Ивлев: это на 100% успех продюсерского таланта дяди Коли и бюджетов холдинга, или он сам приготовил себя? И в чем феномен популярности всей этой кухонной бытовухи?»
  * «Картозия Николай... зачем ему личный бренд и школа? Просто захотел нести знания или заебало в госхолдинге выбирать выражения? Разложи тему сей момент!»

ПРАВИЛО:
Верни ТОЛЬКО текст вопроса к Аристарху (2-4 предложения). Без приветствий, без вводных слов.
"""

async def formulate_question_to_aristarkh(news_item: dict, api_key: str | None = None) -> str:
    """
    Формулирует каверзный вопрос от спарринг-партнёра к Аристарху на основе новости.
    """
    key = api_key or config.GOOGLE_API_KEY
    model = config.ASSISTANT_MODEL_NAME # gemini-2.5-flash
    url = f"https://generativelanguage.googleapis.com/v1beta/{model}:generateContent?key={key}"
    
    title = news_item.get("title", "")
    content = news_item.get("content", "") or news_item.get("original_text", "")
    news_text = f"{title}\n{content}".strip()[:2000]
    
    payload = {
        "systemInstruction": {"parts": [{"text": PROVOCATEUR_SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": f"НОВОСТЬ:\n{news_text}\n\nСформулируй свой острый вопрос Аристарху:"}]}],
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
        ],
        "generationConfig": {
            "temperature": 0.85,
            "maxOutputTokens": 1000
        }
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        question = "".join([p.get("text", "") for p in parts]).strip()
                        question = question.replace("**", "").replace("*", "")
                        logger.info(f"🥊 [Provocateur] Сформулирован вопрос к Аристарху: {question[:100]}...")
                        return question
                else:
                    logger.error(f"Provocateur API error: {resp.status} - {await resp.text()}")
    except Exception as e:
        logger.error(f"Ошибка субагента-провокатора: {e}")
        
    # Fallback если API упал
    return f"Аристарх, разложи эту новость по косточкам: где здесь продюсерский расчет, а где дешевый хайп?"
