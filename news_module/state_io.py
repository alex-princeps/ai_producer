"""
Безопасная работа с tg_news.json.

Файл пишут три процесса: парсер новостей, публикатор и добивки. Публикатор и добивки
ждут случайную паузу перед отправкой, поэтому их копии файла могут устареть. Любая запись
делается так: взять блокировку, перечитать файл, применить изменение, атомарно записать.
"""

import fcntl
import json
import os
from contextlib import contextmanager


@contextmanager
def locked(path: str):
    with open(path + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {} if default is None else default


def update_json(path: str, mutate) -> dict:
    """Перечитать файл под блокировкой, применить mutate(data) и атомарно записать."""
    with locked(path):
        data = read_json(path)
        mutate(data)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return data
