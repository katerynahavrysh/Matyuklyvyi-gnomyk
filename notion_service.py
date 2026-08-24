"""
Тонка обгортка над Notion API: читання й оновлення тасок із бази трекера.
Очікувана структура бази (назви колонок налаштовуються в config.py):

  Завдання        (title)
  Статус          (select)      напр.: Заплановано / В роботі / На перевірці / Готово
  Відповідальний  (rich_text)   ім'я так само, як під час /register у боті
  Дедлайн         (date)
  Зона/пілон      (select)      опційно, лише для відображення
"""
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from notion_client import Client

import config

notion = Client(auth=config.NOTION_TOKEN)


@dataclass
class Task:
    page_id: str
    title: str
    status: str
    assignee: str
    deadline: Optional[date]
    pillar: str

    @property
    def days_overdue(self) -> int:
        if not self.deadline:
            return 0
        return (date.today() - self.deadline).days

    @property
    def is_done(self) -> bool:
        return self.status in config.DONE_STATUSES


def _text_value(prop: dict) -> str:
    """Дістає текст незалежно від того, rich_text це чи title чи select."""
    if prop is None:
        return ""
    ptype = prop.get("type")
    if ptype == "title":
        parts = prop.get("title", [])
        return "".join(p.get("plain_text", "") for p in parts)
    if ptype == "rich_text":
        parts = prop.get("rich_text", [])
        return "".join(p.get("plain_text", "") for p in parts)
    if ptype == "select":
        sel = prop.get("select")
        return sel.get("name", "") if sel else ""
    if ptype == "people":
        people = prop.get("people", [])
        return ", ".join(p.get("name", "") for p in people if p.get("name"))
    return ""


def _date_value(prop: dict) -> Optional[date]:
    if not prop or prop.get("type") != "date":
        return None
    d = prop.get("date")
    if not d or not d.get("start"):
        return None
    return datetime.fromisoformat(d["start"][:10]).date()


def _page_to_task(page: dict) -> Task:
    props = page["properties"]
    return Task(
        page_id=page["id"],
        title=_text_value(props.get(config.PROP_TASK)),
        status=_text_value(props.get(config.PROP_STATUS)),
        assignee=_text_value(props.get(config.PROP_ASSIGNEE)),
        deadline=_date_value(props.get(config.PROP_DEADLINE)),
        pillar=_text_value(props.get(config.PROP_PILLAR)),
    )


def get_all_tasks(include_done: bool = False) -> list[Task]:
    tasks: list[Task] = []
    cursor = None
    while True:
        kwargs = {"database_id": config.NOTION_DATABASE_ID, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.databases.query(**kwargs)
        for page in resp["results"]:
            t = _page_to_task(page)
            if include_done or not t.is_done:
                tasks.append(t)
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return tasks


def get_tasks_for_person(notion_name: str, include_done: bool = False) -> list[Task]:
    return [
        t for t in get_all_tasks(include_done=include_done)
        if t.assignee.strip().lower() == notion_name.strip().lower()
    ]


def update_status(page_id: str, new_status: str) -> None:
    notion.pages.update(
        page_id=page_id,
        properties={config.PROP_STATUS: {"select": {"name": new_status}}},
    )


def get_page_by_id(page_id: str) -> Optional[Task]:
    """Дістає одну сторінку напряму за id — потрібно для обробки вебхуків
    від Notion, де прилітає лише id сторінки, без вмісту."""
    try:
        page = notion.pages.retrieve(page_id=page_id)
    except Exception:
        return None
    # сторінка може належати іншій базі, якщо інтеграція підключена до кількох —
    # перевіряємо, що це саме наш трекер
    parent = page.get("parent", {})
    if parent.get("type") == "database_id" and parent.get("database_id", "").replace("-", "") != config.NOTION_DATABASE_ID.replace("-", ""):
        return None
    return _page_to_task(page)


def create_task(title: str, assignee: str, deadline: date, pillar: str = "") -> Task:
    properties = {
        config.PROP_TASK: {"title": [{"text": {"content": title}}]},
        config.PROP_STATUS: {"select": {"name": config.NOT_STARTED_STATUSES[0]}},
        config.PROP_ASSIGNEE: {"rich_text": [{"text": {"content": assignee}}]},
        config.PROP_DEADLINE: {"date": {"start": deadline.isoformat()}},
    }
    if pillar and config.PROP_PILLAR:
        properties[config.PROP_PILLAR] = {"select": {"name": pillar}}

    page = notion.pages.create(
        parent={"database_id": config.NOTION_DATABASE_ID},
        properties=properties,
    )
    return _page_to_task(page)


def list_known_assignees() -> list[str]:
    """Унікальні імена з колонки 'Відповідальний' — щоб /register міг звірити правопис."""
    names = {t.assignee for t in get_all_tasks(include_done=True) if t.assignee}
    return sorted(names)
