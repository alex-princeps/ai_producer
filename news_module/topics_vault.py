import os
import re
import json
import logging
from datetime import datetime

logger = logging.getLogger("AristarkhSystem")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VAULT_FILE = os.path.join(BASE_DIR, "topics_vault.json")
VAULT_MD_FILE = os.path.join(BASE_DIR, "Золотой_фонд_тем.md")

_MD_HEADER_RE = re.compile(r"^##\s*Тема\s*#(\d+)\s*\[([^\]]+)\]", re.MULTILINE)


def _write_md(items: list[dict]):
    lines = [
        "# Золотой фонд тем для рубрики «Вопрос из зрительного зала»",
        "",
        "> **Как редактировать:**",
        "> - Вы можете свободно править формулировки любых вопросов прямо в этом файле.",
        "> - `[В ОЧЕРЕДИ]` — тема стоит в очереди на публикацию (каждый 5-й день).",
        "> - `[ОПУБЛИКОВАНО]` — тема уже выходила в канале (повторно браться не будет).",
        "> - Чтобы отключить слишком острую тему, удалите её блок или замените `[В ОЧЕРЕДИ]` на `[ОПУБЛИКОВАНО]`.",
        "> - Модуль `topics_vault.py` автоматически синхронизирует изменения из этого `.md` файла в `topics_vault.json`.",
        "",
        "---",
        ""
    ]
    for item in items:
        status = "ОПУБЛИКОВАНО" if item.get("used") else "В ОЧЕРЕДИ"
        tid = item.get("id")
        lines.append(f"## Тема #{tid} [{status}]")
        lines.append((item.get("question") or "").strip())
        lines.append("")
    try:
        with open(VAULT_MD_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception as e:
        logger.error(f"Ошибка записи Золотой_фонд_тем.md: {e}")


def sync_from_md_if_needed(json_items: list[dict]) -> list[dict]:
    """Если Золотой_фонд_тем.md редактировался позже topics_vault.json, подтягивает правки текста и статусов."""
    if not os.path.exists(VAULT_MD_FILE):
        return json_items
    try:
        if os.path.exists(VAULT_FILE) and os.path.getmtime(VAULT_MD_FILE) <= os.path.getmtime(VAULT_FILE):
            return json_items
        with open(VAULT_MD_FILE, "r", encoding="utf-8") as f:
            md_text = f.read()
        matches = list(_MD_HEADER_RE.finditer(md_text))
        if not matches:
            return json_items
        by_id = {int(i.get("id", 0)): i for i in json_items}
        synced = []
        for idx, m in enumerate(matches):
            tid = int(m.group(1))
            status_raw = m.group(2).strip().upper()
            start = m.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(md_text)
            q_text = md_text[start:end].strip()
            if not q_text:
                continue
            prev = by_id.get(tid, {})
            # Если в JSON тема уже помечена использованной ботом, сохраняем used=True,
            # кроме случая когда в MD явно вернули [В ОЧЕРЕДИ] при ручной правке
            is_used_md = status_raw != "В ОЧЕРЕДИ"
            synced.append({
                "id": tid,
                "title": q_text[:80],
                "question": q_text,
                "published": bool(prev.get("published", is_used_md)),
                "used": is_used_md,
                "used_at": prev.get("used_at") if is_used_md else None,
            })
        with open(VAULT_FILE, "w", encoding="utf-8") as f:
            json.dump(synced, f, ensure_ascii=False, indent=2)
        logger.info(f"🔄 [Topics Vault] Синхронизировано {len(synced)} тем из Золотой_фонд_тем.md")
        return synced
    except Exception as e:
        logger.error(f"Ошибка синхронизации из Золотой_фонд_тем.md: {e}")
        return json_items


def load_vault() -> list[dict]:
    items = []
    if os.path.exists(VAULT_FILE):
        try:
            with open(VAULT_FILE, "r", encoding="utf-8") as f:
                items = json.load(f)
        except Exception as e:
            logger.error(f"Ошибка загрузки topics_vault.json: {e}")
    return sync_from_md_if_needed(items)


def save_vault(items: list[dict]):
    try:
        with open(VAULT_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        _write_md(items)
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


def append_vault_topic(question: str) -> int:
    """Добавляет новый вопрос (например, одобренный админом из бота ?start=hall) в конец фонда."""
    q_clean = (question or "").strip()
    if not q_clean:
        return 0
    vault = load_vault()
    next_id = max((int(i.get("id", 0)) for i in vault), default=0) + 1
    vault.append({
        "id": next_id,
        "title": q_clean[:80],
        "question": q_clean,
        "published": False,
        "used": False,
        "used_at": None,
    })
    save_vault(vault)
    logger.info(f"➕ [Topics Vault] Добавлена новая тема #{next_id} из бота: {q_clean[:60]}...")
    return next_id
