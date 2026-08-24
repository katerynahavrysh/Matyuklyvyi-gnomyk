"""
Найпростіше сховище для мапінгу "хто є хто": telegram chat_id <-> ім'я у Notion
(як воно записане в колонці "Відповідальний"). Ніякої бази даних не треба —
команда з 4 людей, JSON-файл впорається.
"""
import json
import os
import threading
from typing import Optional

from config import USERS_FILE

_lock = threading.Lock()


def _load() -> dict:
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def register(user_id: int, notion_name: str, telegram_username: Optional[str]) -> None:
    """user_id — це Telegram user id людини (НЕ chat_id групи!). Для приватних
    чатів вони збігаються, тому це значення однаково годиться і для тегання
    (tg://user?id=...), і для надсилання особистих повідомлень."""
    with _lock:
        data = _load()
        data[str(user_id)] = {
            "notion_name": notion_name,
            "telegram_username": telegram_username,
        }
        _save(data)


def get_by_chat_id(user_id: int) -> Optional[dict]:
    data = _load()
    return data.get(str(user_id))


def get_chat_id_by_notion_name(notion_name: str) -> Optional[int]:
    data = _load()
    for chat_id, info in data.items():
        if info.get("notion_name", "").strip().lower() == notion_name.strip().lower():
            return int(chat_id)
    return None


def all_users() -> dict:
    return _load()
