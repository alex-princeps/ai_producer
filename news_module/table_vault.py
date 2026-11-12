import os
import re
import json
import logging
from datetime import datetime

logger = logging.getLogger("AristarkhSystem")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TABLE_JSON_FILE = os.path.join(BASE_DIR, "table_vault.json")
TABLE_MD_FILE = os.path.join(BASE_DIR, "Банк_пациентов_Монтажный_стол.md")

_MD_HEADER_RE = re.compile(r"^##\s*Пациент\s*#(\d+)\s*\[([^\]]+)\]\s*(?:\(([^)]+)\))?", re.MULTILINE)


def _write_md(items: list[dict]):
    lines = [
        "# Банк пациентов для рубрики среды «Монтажный стол: Было → Стало»",
        "",
        "> **Статус калибровки:** Ожидается список донорских каналов от Алекса для совместного анализа и формирования эталонного банка типовых ошибок.",
        "> **Как работает авто-фоллбэк:** Пока в этом файле нет элементов со статусом `[В ОЧЕРЕДИ]`, по средам крон автоматически публикует обычный новостной разбор («Аппаратная»). Как только здесь появляются записи `[В ОЧЕРЕДИ]` (после калибровки донорских каналов или по нажатию кнопки `[➕ В Банк Монтажного стола]` в админке бота), среда автоматически переключается на рубрику «Монтажный стол».",
        "> **Формат записи:**",
        "> `## Пациент #1 [В ОЧЕРЕДИ] (Ниша: частная клиника / адвокат / диджитал-агентство)`",
        "> `Текст исходника («Было»), который Аристарх разнесёт и перепишет в «Стало».`",
        "",
        "---",
        "",
    ]
    for item in items:
        status = "ОПУБЛИКОВАНО" if item.get("used") else "В ОЧЕРЕДИ"
        tid = item.get("id")
        niche = item.get("niche") or "Экспертный / B2B-проект"
        lines.append(f"## Пациент #{tid} [{status}] ({niche})")
        lines.append((item.get("raw_text") or "").strip())
        lines.append("")
    try:
        with open(TABLE_MD_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception as e:
        logger.error(f"Ошибка записи {TABLE_MD_FILE}: {e}")


def sync_from_md_if_needed(json_items: list[dict]) -> list[dict]:
    if not os.path.exists(TABLE_MD_FILE):
        _write_md(json_items)
        return json_items
    try:
        if os.path.exists(TABLE_JSON_FILE) and os.path.getmtime(TABLE_MD_FILE) <= os.path.getmtime(TABLE_JSON_FILE):
            return json_items
        with open(TABLE_MD_FILE, "r", encoding="utf-8") as f:
            md_text = f.read()
        matches = list(_MD_HEADER_RE.finditer(md_text))
        if not matches:
            return []
        by_id = {int(i.get("id", 0)): i for i in json_items}
        synced = []
        for idx, m in enumerate(matches):
            tid = int(m.group(1))
            status_raw = m.group(2).strip().upper()
            niche_raw = (m.group(3) or "Экспертный / B2B-проект").strip()
            start = m.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(md_text)
            raw_text = md_text[start:end].strip()
            if not raw_text:
                continue
            prev = by_id.get(tid, {})
            is_used = status_raw != "В ОЧЕРЕДИ"
            synced.append({
                "id": tid,
                "niche": niche_raw,
                "title": raw_text[:80],
                "raw_text": raw_text,
                "used": is_used,
                "used_at": prev.get("used_at") if is_used else None,
            })
        with open(TABLE_JSON_FILE, "w", encoding="utf-8") as f:
            json.dump(synced, f, ensure_ascii=False, indent=2)
        logger.info(f"🔄 [Table Vault] Синхронизировано {len(synced)} пациентов из Банк_пациентов_Монтажный_стол.md")
        return synced
    except Exception as e:
        logger.error(f"Ошибка синхронизации из {TABLE_MD_FILE}: {e}")
        return json_items


def load_table_vault() -> list[dict]:
    items = []
    if os.path.exists(TABLE_JSON_FILE):
        try:
            with open(TABLE_JSON_FILE, "r", encoding="utf-8") as f:
                items = json.load(f)
        except Exception as e:
            logger.error(f"Ошибка загрузки {TABLE_JSON_FILE}: {e}")
    else:
        save_table_vault([])
    return sync_from_md_if_needed(items)


def save_table_vault(items: list[dict]):
    try:
        with open(TABLE_JSON_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        _write_md(items)
    except Exception as e:
        logger.error(f"Ошибка сохранения {TABLE_JSON_FILE}: {e}")


def get_next_table_patient() -> dict | None:
    vault = load_table_vault()
    for item in vault:
        if not item.get("used"):
            item["used"] = True
            item["used_at"] = datetime.now().isoformat()
            save_table_vault(vault)
            logger.info(f"🔪 [Table Vault] Взят пациент #{item.get('id')} ({item.get('niche')}): {item.get('title')[:60]}...")
            return item
    return None


def peek_next_table_patient() -> dict | None:
    vault = load_table_vault()
    for item in vault:
        if not item.get("used"):
            return item
    return None


def count_table_stats() -> tuple[int, int]:
    vault = load_table_vault()
    return len(vault), sum(1 for i in vault if not i.get("used"))


def append_table_patient(raw_text: str, niche: str = "Экземпляр из @media_producer_bot") -> int:
    """Добавляет новый обезличенный текст в Банк Монтажного стола (например, по кнопке админа в боте)."""
    t_clean = (raw_text or "").strip()
    if not t_clean:
        return 0
    vault = load_table_vault()
    next_id = max((int(i.get("id", 0)) for i in vault), default=0) + 1
    vault.append({
        "id": next_id,
        "niche": niche,
        "title": t_clean[:80],
        "raw_text": t_clean,
        "used": False,
        "used_at": None,
    })
    save_table_vault(vault)
    logger.info(f"➕ [Table Vault] Добавлен пациент #{next_id}: {t_clean[:60]}...")
    return next_id
