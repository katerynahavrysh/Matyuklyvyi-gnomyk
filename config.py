"""
Конфігурація Матюкливого гномика.
Усі значення читаються зі змінних середовища (.env) — жодних токенів у коді.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str = None, required: bool = False) -> str:
    val = os.getenv(name, default)
    if required and not val:
        raise RuntimeError(f"Відсутня обов'язкова змінна середовища: {name}. Дивись .env.example")
    return val


def _get_list(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


# --- Telegram ---
TELEGRAM_BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN", required=True)
# Груповий чат команди, куди гномик шле ВСІ нагадування і тегає людину.
# Якщо не задано — бот падає назад на приватні повідомлення (без тегання).
TEAM_CHAT_ID = os.getenv("TEAM_CHAT_ID") or os.getenv("SHAME_GROUP_CHAT_ID")  # друге — для сумісності зі старим .env

# --- Notion ---
NOTION_TOKEN = _get("NOTION_TOKEN", required=True)
NOTION_DATABASE_ID = _get("NOTION_DATABASE_ID", required=True)

# Назви властивостей (properties) у базі Notion — підлаштуй під свою базу,
# якщо назви колонок відрізняються від запропонованих у README.
PROP_TASK = _get("NOTION_PROP_TASK", "Завдання")
PROP_STATUS = _get("NOTION_PROP_STATUS", "Статус")
PROP_ASSIGNEE = _get("NOTION_PROP_ASSIGNEE", "Відповідальний")
PROP_DEADLINE = _get("NOTION_PROP_DEADLINE", "Дедлайн")
PROP_PILLAR = _get("NOTION_PROP_PILLAR", "Зона/пілон")

# Статуси, які вважаються "завершено" — такі таски гномик НЕ чіпає.
DONE_STATUSES = _get_list("NOTION_DONE_STATUSES", "Готово,Опубліковано")
# Статуси, які вважаються "ще навіть не почато" — саме на них реагує
# нагадування "2 дні до дедлайну, а віз і нині там".
NOT_STARTED_STATUSES = _get_list("NOTION_NOT_STARTED_STATUSES", "Заплановано,Не почато")
# Повний список статусів для кнопок перемикання в /tasks
STATUS_OPTIONS = _get_list(
    "NOTION_STATUS_OPTIONS", "Заплановано,В роботі,На перевірці,Готово"
)

# --- Розклад ---
TIMEZONE = _get("TIMEZONE", "Europe/Kyiv")
# Щоденний дайджест: список тасок людини на поточний тиждень (пн–нд)
DAILY_DIGEST_HOUR = int(_get("DAILY_DIGEST_HOUR", "10"))
DAILY_DIGEST_MINUTE = int(_get("DAILY_DIGEST_MINUTE", "0"))
# Перевірка "2 дні / 1 день до дедлайну" запускається одразу після дайджесту
DEADLINE_WATCH_MINUTE_OFFSET = int(_get("DEADLINE_WATCH_MINUTE_OFFSET", "5"))
NAG_HOURS = [int(h.strip()) for h in _get("NAG_HOURS", "12,18").split(",")]
WORKDAY_START_HOUR = int(_get("WORKDAY_START_HOUR", "9"))
WORKDAY_END_HOUR = int(_get("WORKDAY_END_HOUR", "21"))

# --- Рівень токсичності: 1 = м'яко-саркастично, 2 = жорсткіше, 3 = без цензури ---
SPICE_LEVEL = int(_get("SPICE_LEVEL", "1"))

# --- Локальне сховище реєстрації користувачів ---
USERS_FILE = _get("USERS_FILE", "users.json")

# --- Двосторонній зв'язок з Notion (вебхук: Notion -> бот) ---
# Публічний порт, який слухає вбудований міні-сервер для вебхука від Notion.
# Хостинг (Railway/Render) сам підставить свій PORT — локально можна лишити 8080.
WEBHOOK_PORT = int(_get("PORT", "8080"))
# Шлях, на який Notion шле події (повний публічний URL = https://ваш-домен + цей шлях)
NOTION_WEBHOOK_PATH = _get("NOTION_WEBHOOK_PATH", "/notion-webhook")
# Секрет для перевірки підпису вхідних запитів від Notion.
# Це той самий "verification_token", який Notion видасть один раз під час
# підключення вебхука в налаштуваннях інтеграції — див. README, розділ "Двосторонній зв'язок".
NOTION_WEBHOOK_SECRET = os.getenv("NOTION_WEBHOOK_SECRET")  # порожньо = вебхук вимкнено
# Якщо вебхук ще не підтверджений (немає секрету), міні-сервер все одно піднімається,
# щоб прийняти перший запит із verification_token і показати його в логах.
