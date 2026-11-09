import os
import json
import logging
from datetime import datetime

logger = logging.getLogger("AristarkhSystem")

VAULT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "topics_vault.json")

def load_vault() -> list[dict]:
    if not os.path.exists(VAULT_FILE):
        return []
    try:
        with open(VAULT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Ошибка загрузки topics_vault.json: {e}")
        return []

def save_vault(items: list[dict]):
    try:
        with open(VAULT_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения topics_vault.json: {e}")

def get_next_vault_topic() -> dict | None:
    """
    Возвращает следующую неиспользованную тему из золотого фонда Алекса,
    помечает её использованной и сохраняет дату.
    """
    vault = load_vault()
    for item in vault:
        if not item.get("used"):
            item["used"] = True
            item["used_at"] = datetime.now().isoformat()
            save_vault(vault)
            logger.info(f"🎯 [Topics Vault] Взята тема #{item.get('id')}: {item.get('title')[:60]}...")
            return item
    logger.warning("⚠️ [Topics Vault] Все темы из золотого фонда использованы!")
    return None

def peek_next_vault_topic() -> dict | None:
    """Смотрит следующую тему без изменения статуса."""
    vault = load_vault()
    for item in vault:
        if not item.get("used"):
            return item
    return None

def count_stats() -> tuple[int, int]:
    vault = load_vault()
    total = len(vault)
    unused = sum(1 for i in vault if not i.get("used"))
    return total, unused
