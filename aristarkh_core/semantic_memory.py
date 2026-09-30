import json
import os
import asyncio

class SemanticMemory:
    """Граф знаний (Семантическая память) для хранения жестких фактов о пользователе."""
    def __init__(self, storage_filename="semantic_memory.json"):
        # [КРИТИЧЕСКИЙ ФИКС] Абсолютный путь — больше не зависит от cwd
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.storage_path = os.path.join(base_dir, storage_filename)
        
        if not os.path.exists(self.storage_path):
            with open(self.storage_path, 'w', encoding='utf-8') as f:
                json.dump({}, f)

    async def add_facts(self, user_id: int, facts: list):
        """Добавляет новые триплеты (Субъект -> Предикат -> Объект) в память."""
        if not facts: return
        
        def _write():
            with open(self.storage_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            uid = str(user_id)
            if uid not in data:
                data[uid] = []
            
            for fact in facts:
                if fact not in data[uid]:
                    data[uid].append(fact)
            
            with open(self.storage_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

        await asyncio.to_thread(_write)

    async def get_facts(self, user_id: int) -> str:
        """Извлекает все жесткие факты о пользователе для инъекции в промпт."""
        def _read():
            with open(self.storage_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data.get(str(user_id), [])
            
        facts = await asyncio.to_thread(_read)
        return "\n".join([f"- {fact}" for fact in facts]) if facts else ""