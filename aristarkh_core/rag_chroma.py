import os
import sys
import pypdf
import docx
import aiohttp
import asyncio
import shutil
import re
import time
import chromadb
from aristarkh_core.config import config, logger

class SmartTextSplitter:
    """
    Кастомный рекурсивный сплиттер (аналог LangChain).
    Режет текст по логическим блокам (абзацы -> предложения -> слова),
    сохраняя перекрытие (overlap), чтобы не терять контекст на стыках.
    """
    def __init__(self, chunk_size=1200, chunk_overlap=200):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        # Приоритет разделителей: от самых крупных логических блоков к мелким
        self.separators = ["\n\n", "\n", ". ", "? ", "! ", " "]

    def split_text(self, text: str) -> list[str]:
        return self._split_recursive(text, self.separators)

    def _split_recursive(self, text: str, separators: list[str]) -> list[str]:
        final_chunks = []
        if len(text) <= self.chunk_size:
            return [text]

        separator = separators[0]
        for s in separators:
            if s in text:
                separator = s
                break
        
        # Бьем текст по выбранному разделителю
        splits = text.split(separator)
        good_splits = []
        
        for s in splits:
            if len(s) < self.chunk_size:
                good_splits.append(s)
            else:
                if good_splits:
                    final_chunks.extend(self._merge_splits(good_splits, separator))
                    good_splits = []
                # Если кусок все еще огромный, проваливаемся на уровень глубже
                next_separators = separators[separators.index(separator) + 1:] if separator in separators else []
                if next_separators:
                    final_chunks.extend(self._split_recursive(s, next_separators))
                else:
                    # Если разделителей больше нет, режем жестко
                    final_chunks.extend([s[i:i+self.chunk_size] for i in range(0, len(s), self.chunk_size)])
                    
        if good_splits:
            final_chunks.extend(self._merge_splits(good_splits, separator))
            
        return final_chunks

    def _merge_splits(self, splits: list[str], separator: str) -> list[str]:
        # Собираем куски так, чтобы они не превышали chunk_size, и добавляем overlap
        docs = []
        current_doc = []
        total_len = 0
        
        for s in splits:
            _len = len(s) + (len(separator) if current_doc else 0)
            if total_len + _len > self.chunk_size and current_doc:
                docs.append(separator.join(current_doc))
                # Оставляем хвост для overlap
                while total_len > self.chunk_overlap and len(current_doc) > 1:
                    total_len -= len(current_doc[0]) + len(separator)
                    current_doc.pop(0)
            current_doc.append(s)
            total_len += _len
            
        if current_doc:
            docs.append(separator.join(current_doc))
        return docs


class RAGService:
    def __init__(self, api_key: str):
        self.api_key = api_key
        # [КРИТИЧЕСКИЙ ФИКС] Абсолютный путь — больше не зависит от cwd
        base_dir = os.path.dirname(os.path.abspath(__file__))
        # Папка базы знаний: KNOWLEDGE_DIR (абсолютный путь или от корня проекта), по умолчанию knowledge_base/
        kb_dir = os.getenv("KNOWLEDGE_DIR", "knowledge_base")
        self.kb_path = kb_dir if os.path.isabs(kb_dir) else os.path.join(os.path.dirname(base_dir), kb_dir)
        self.http_session = None 
        os.makedirs(self.kb_path, exist_ok=True)
        
        self.splitter = SmartTextSplitter(chunk_size=1200, chunk_overlap=200)
        
        # Механизм Self-Healing v2.0
        try:
            self.chroma_client = chromadb.PersistentClient(path=config.CHROMA_PATH)
            self.chroma_client.heartbeat()
        except Exception as e:
            logger.error(f"⚠️ ОШИБКА БАЗЫ ДАННЫХ: {e}")
            logger.warning("♻️ База повреждена (malformed). Выполняю полную очистку...")
            
            if os.path.exists(config.CHROMA_PATH):
                shutil.rmtree(config.CHROMA_PATH)
            
            logger.info("✅ Битая база удалена.")
            logger.warning("🛑 СИСТЕМА ОСТАНОВЛЕНА ДЛЯ СБРОСА КЭША БИБЛИОТЕК.")
            sys.exit(0)

        self.collection = self.chroma_client.get_or_create_collection(name="aristarkh_knowledge")

    def _read_file(self, path):
        if not os.path.exists(path): return ""
        try:
            if path.endswith('.pdf'):
                reader = pypdf.PdfReader(path)
                return "".join([p.extract_text() for p in reader.pages])
            elif path.endswith('.docx'):
                doc = docx.Document(path)
                return "\n".join([p.text for p in doc.paragraphs])
            else:
                with open(path, 'r', encoding='utf-8') as f: return f.read()
        except Exception as e: 
            logger.error(f"Ошибка чтения {path}: {e}")
            return ""

    async def _get_embedding(self, text: str):
        url = f"https://generativelanguage.googleapis.com/v1beta/{config.EMBEDDING_MODEL}:embedContent?key={self.api_key}"
        # Для эмбеддингов обрезаем лишнее, чтобы влезло в лимит Google
        payload = {"model": config.EMBEDDING_MODEL, "content": {"parts": [{"text": text[:2000]}]}}
        
        session = self.http_session or aiohttp.ClientSession(trust_env=True)
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data['embedding']['values']
                else:
                    logger.error(f"Embedding API Error: {resp.status}")
                    return None
        except Exception as e:
            logger.error(f"Embedding Conn Error: {e}")
            return None
        finally:
            if not self.http_session:
                await session.close()

    async def build_index(self):
        files = []
        for root, _, filenames in os.walk(self.kb_path):
            for f in filenames:
                if f.endswith(('.pdf', '.docx', '.txt', '.md')) and f != 'README.md':
                    files.append(os.path.join(root, f))
        logger.info(f"📚 [RAG] Найдено файлов: {len(files)}")
        
        if self.collection.count() > 0:
            logger.info(f"💾 ChromaDB уже содержит {self.collection.count()} чанков. Пропускаем полную переиндексацию.")
            return

        logger.info("⚙️ Начинаю УМНУЮ индексацию базы знаний...")
        all_ids = []
        all_docs = []
        all_embeddings = []
        
        for file_path in files:
            filename = os.path.basename(file_path)
            logger.info(f"Indexing {filename}...")
            txt = self._read_file(file_path)
            if len(txt) < 50: 
                logger.info(f"  > Пропуск {filename} (слишком короткий)")
                continue
            
            # [FIX] Вытаскиваем "тему" из названия файла
            filename = os.path.basename(file_path)
            clean_name = os.path.splitext(filename)[0].replace('_', ' ')
            source_header = f"[ИСТОЧНИК: {clean_name}]\n"
            
            # Режем текст умным сплиттером
            chunks = self.splitter.split_text(txt)
            logger.info(f"  > Найдено {len(chunks)} чанков. Генерируем векторы...")
            
            for i, chunk in enumerate(chunks):
                # Вклеиваем заголовок источника в каждый чанк!
                enriched_chunk = source_header + chunk.strip()
                
                vec = await self._get_embedding(enriched_chunk)
                if vec:
                    doc_id = f"{filename}_chunk_{i}"
                    all_ids.append(doc_id)
                    all_docs.append(enriched_chunk)
                    all_embeddings.append(vec)
                
                # Batch processing
                if len(all_ids) >= 50:
                    logger.info(f"  > Сохраняем пакет из 50 чанков...")
                    await asyncio.to_thread(
                        self.collection.add,
                        documents=all_docs, embeddings=all_embeddings, ids=all_ids
                    )
                    all_ids, all_docs, all_embeddings = [], [], []
                    await asyncio.sleep(1) # Бережем API лимиты Google
            logger.info(f"  > Завершен файл {filename}")
                    
        if all_ids:
            await asyncio.to_thread(
                self.collection.add,
                documents=all_docs, embeddings=all_embeddings, ids=all_ids
            )
            
        logger.info(f"✅ RAG база обновлена. Всего чанков: {self.collection.count()}")

    async def search(self, query: str, top_k=5) -> str:
        if self.collection.count() == 0: return ""
        
        # [FIX] Рерайт запроса для RAG
        # Добавляем ключевые слова для лучшего поиска векторов
        enhanced_query = f"{query} телевизионный проект шоу продюсирование форматы"
        query_vec = await self._get_embedding(enhanced_query)
        
        if not query_vec: return ""
        
        results = await asyncio.to_thread(
            self.collection.query,
            query_embeddings=[query_vec],
            n_results=top_k
        )
        
        if results and results['documents'] and results['documents'][0]:
            # Склеиваем найденные куски с разделителями для Gemini
            return "\n\n=== СЛЕДУЮЩИЙ ФРАГМЕНТ АРХИВА ===\n\n".join(results['documents'][0])
        return ""

    # === [UPGRADED] ЭПИЗОДИЧЕСКАЯ ПАМЯТЬ С КОГНИТИВНЫМ СКОРИНГОМ (SMALLVILLE) ===
    
    async def add_episodic_memory(self, user_id: int, text: str, emotion_weight: float = 0.5):
        """Сохраняет саммари диалога в персональную коллекцию пользователя."""
        collection = self.chroma_client.get_or_create_collection(name=f"memory_{user_id}")
        vec = await self._get_embedding(text)
        if vec:
            doc_id = f"mem_{int(time.time())}"
            await asyncio.to_thread(
                collection.add,
                documents=[text],
                embeddings=[vec],
                ids=[doc_id],
                metadatas=[{"timestamp": time.time(), "emotion_weight": emotion_weight}]
            )
            logger.info(f"🧠 Сохранено новое воспоминание для {user_id} (Важность: {emotion_weight})")

    async def search_episodic_memory(self, user_id: int, query: str, top_k=3) -> str:
        """
        Ищет воспоминания, используя формулу когнитивного скоринга Smallville:
        
        Score = (Релевантность × 1.5) + (Важность × 1.0) + (Свежесть × 0.5)
        
        Где:
        - Релевантность: косинусное сходство вектора запроса с вектором воспоминания
        - Важность: emotion_weight, присвоенный экстрактором при сохранении
        - Свежесть: экспоненциальное затухание (0.99^часы), не опускается ниже 0.1
        
        Это гарантирует, что Аристарх вспомнит не просто «похожее», а 
        ВАЖНОЕ + СВЕЖЕЕ + РЕЛЕВАНТНОЕ одновременно.
        """
        try:
            collection = self.chroma_client.get_collection(name=f"memory_{user_id}")
        except Exception:
            return ""  # Коллекции еще нет, пользователь новый
            
        if collection.count() == 0: 
            return ""
        
        query_vec = await self._get_embedding(query)
        if not query_vec: 
            return ""
        
        # 1. Извлекаем сырые данные с запасом (топ-15), чтобы было из чего выбирать
        results = await asyncio.to_thread(
            collection.query,
            query_embeddings=[query_vec],
            n_results=min(15, collection.count()),
            include=["documents", "metadatas", "distances"]
        )
        
        if not results or not results['documents'] or not results['documents'][0]:
            return ""

        scored_memories = []
        current_time = time.time()

        docs = results['documents'][0]
        metas = results['metadatas'][0]
        distances = results['distances'][0]

        # 2. Ранжирование по когнитивной формуле Smallville
        for i in range(len(docs)):
            doc = docs[i]
            meta = metas[i]
            dist = distances[i]

            # Релевантность: L2 Distance → Similarity (чем меньше дистанция, тем ближе к 1.0)
            relevance = 1.0 / (1.0 + dist)

            # Важность: извлекается когнитивным экстрактором при записи (0.1–1.0)
            importance = meta.get("emotion_weight", 0.5)

            # Свежесть: экспоненциальное затухание (теряет ~1% важности каждый час)
            mem_time = meta.get("timestamp", current_time)
            hours_ago = (current_time - mem_time) / 3600.0
            decay_factor = 0.99 ** hours_ago  # ~50% через 69 часов, ~10% через 230 часов
            recency = max(0.1, decay_factor)  # Не опускается ниже 0.1 — даже старые воспоминания не исчезают

            # ФОРМУЛА SMALLVILLE
            final_score = (relevance * 1.5) + (importance * 1.0) + (recency * 0.5)
            scored_memories.append((final_score, doc, meta))

        # 3. Сортируем по убыванию финального скора
        scored_memories.sort(key=lambda x: x[0], reverse=True)
        
        # 4. Берём только топ-K после когнитивного ранжирования
        top_docs = [m[1] for m in scored_memories[:top_k]]
        
        logger.debug(f"🧠 [Память] Когнитивный скоринг: извлечено топ-{top_k} "
                     f"из {len(scored_memories)} кандидатов для юзера {user_id}")
        return "\n".join([f"- {doc}" for doc in top_docs])

if __name__ == "__main__":
    async def main():
        service = RAGService(config.GOOGLE_API_KEY)
        await service.build_index()
    asyncio.run(main())