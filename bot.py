"""
Матюкливий гномик — Telegram-бот, інтегрований з Notion.
Стежить за статусами тасок, нагадує про дедлайни, тегає винних у груповому чаті,
і вміє створювати таски у Notion прямо з Telegram (двосторонній зв'язок).

Запуск:  python bot.py
Налаштування: .env (див. .env.example)
"""
import asyncio
import hashlib
import hmac
import html
import logging
from datetime import date, datetime, timedelta, time as dtime

import pytz
from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import config
import messages
import notion_service
import storage

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("gnomyk")

TZ = pytz.timezone(config.TIMEZONE)


# ------------------------------------------------------------------
# Тегання людей і доставка повідомлень у груповий чат
# ------------------------------------------------------------------

def _parse_mode():
    """HTML-режим вмикається тільки коли є TEAM_CHAT_ID — саме тоді ми
    використовуємо HTML-теги для тегання людини через tg://user?id=..."""
    return ParseMode.HTML if config.TEAM_CHAT_ID else None


def _esc(text: str) -> str:
    """Екранує спецсимволи, коли повідомлення піде в HTML-режимі,
    інакше повертає текст без змін (щоб не сипати &amp; в звичайні DM)."""
    return html.escape(text) if config.TEAM_CHAT_ID and text else (text or "")


def _mention_or_name(user_id, name: str) -> str:
    """Якщо є груповий чат — повертає HTML-посилання, яке Telegram показує
    як тег людини (і надсилає їй нотифікацію). Якщо групи нема — просто ім'я."""
    if config.TEAM_CHAT_ID and user_id:
        return f'<a href="tg://user?id={user_id}">{html.escape(name)}</a>'
    return name


async def _deliver(context: ContextTypes.DEFAULT_TYPE, user_id, text: str):
    """Надіслати повідомлення в груповий чат команди (тегаючи людину), або,
    якщо груповий чат не налаштований, особисто людині в приваті."""
    target = config.TEAM_CHAT_ID or user_id
    if not target:
        log.warning("Нема куди надсилати: ні TEAM_CHAT_ID, ні user_id")
        return
    try:
        await context.bot.send_message(chat_id=target, text=text, parse_mode=_parse_mode())
    except Exception as e:
        log.warning("Не вдалось надіслати повідомлення в %s: %s", target, e)


# ------------------------------------------------------------------
# Допоміжне
# ------------------------------------------------------------------

def _week_bounds(today: date | None = None) -> tuple[date, date]:
    """Понеділок і неділя поточного тижня (тиждень рахується пн–нд)."""
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def _format_task_line(t: "notion_service.Task") -> str:
    deadline_str = t.deadline.strftime("%d.%m") if t.deadline else "без дедлайну"
    tag = ""
    if t.deadline:
        if t.days_overdue > 0:
            tag = f" 🔥 прострочено {t.days_overdue} дн."
        elif t.days_overdue == 0:
            tag = " ⏰ сьогодні"
    return f"«{_esc(t.title)}» — {_esc(t.status)} · дедлайн {deadline_str}{tag}"


def _parse_date(raw: str):
    raw = raw.strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%d.%m"):
        try:
            parsed = datetime.strptime(raw, fmt)
            if fmt == "%d.%m":
                parsed = parsed.replace(year=date.today().year)
            return parsed.date()
        except ValueError:
            continue
    return None


# ------------------------------------------------------------------
# Команди
# ------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(messages.WELCOME)


async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Використання: /register Ім'я\n(точно так, як записано в колонці «Відповідальний» у Notion)\n\n"
            "Пиши мені це в приватному чаті, навіть якщо нагадування потім будуть у груповому."
        )
        return

    name = " ".join(context.args).strip()
    known = notion_service.list_known_assignees()
    known_lower = {k.lower(): k for k in known}

    if name.lower() not in known_lower:
        await update.message.reply_text(
            messages.REGISTER_UNKNOWN_NAME.format(
                name=name, known=", ".join(known) if known else "(база порожня або назви колонок не збігаються)"
            )
        )
        return

    canonical_name = known_lower[name.lower()]
    storage.register(
        user_id=update.effective_user.id,
        notion_name=canonical_name,
        telegram_username=update.effective_user.username,
    )
    await update.message.reply_text(messages.pick(messages.REGISTER_OK).format(name=canonical_name))

def _status_keyboard(page_id: str, current_status: str) -> InlineKeyboardMarkup:
    buttons = []
    for idx, status in enumerate(config.STATUS_OPTIONS):
        if status == current_status:
            continue
        buttons.append(
            InlineKeyboardButton(text=status, callback_data=f"st|{page_id}|{idx}")
        )
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = storage.get_by_chat_id(update.effective_user.id)
    if not user:
        await update.message.reply_text(
            "Я тебе не знаю. Спочатку напиши мені в приваті /register Ім'я — так, як у Notion."
        )
        return

    tasks = notion_service.get_tasks_for_person(user["notion_name"])
    if not tasks:
        await update.message.reply_text(messages.pick(messages.NO_TASKS))
        return

    tasks.sort(key=lambda t: (-t.days_overdue if t.deadline else -999))
    for t in tasks:
        deadline_str = t.deadline.strftime("%d.%m") if t.deadline else "без дедлайну"
        tag = ""
        if t.deadline:
            if t.days_overdue > 0:
                tag = f" 🔥 прострочено {t.days_overdue} дн."
            elif t.days_overdue == 0:
                tag = " ⏰ сьогодні"
        text = f"«{t.title}» — {t.status} · дедлайн {deadline_str}{tag}"
        await update.message.reply_text(text, reply_markup=_status_keyboard(t.page_id, t.status))


async def on_status_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Записую...")
    try:
        _, page_id, idx_str = query.data.split("|", 2)
        new_status = config.STATUS_OPTIONS[int(idx_str)]
    except (ValueError, IndexError):
        await query.answer("Щось зламалось, спробуй /tasks ще раз.")
        return

    notion_service.update_status(page_id, new_status)

    old_text = query.message.text or ""
    title = old_text.split("»")[0].lstrip("«") if "«" in old_text else "таска"
    new_line = f"«{title}» — {new_status}"

    await query.edit_message_text(new_line, reply_markup=_status_keyboard(page_id, new_status))

    if new_status in config.DONE_STATUSES:
        user_id = update.effective_user.id
        user = storage.get_by_chat_id(user_id)
        name = user["notion_name"] if user else "хтось"
        name_repr = _mention_or_name(user_id, name)
        text = messages.pick(messages.TASK_DONE_PRAISE).format(name=name_repr, task=_esc(title))
        await _deliver(context, user_id, text)


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Загальний звіт по команді — хто скільки прострочив. Доступно всім, тегає боржників."""
    tasks = notion_service.get_all_tasks()
    overdue = [t for t in tasks if t.deadline and t.days_overdue > 0]
    if not overdue:
        await update.message.reply_text(messages.pick(messages.SHAME_GROUP_CLEAN))
        return
    overdue.sort(key=lambda t: -t.days_overdue)
    lines = [messages.pick(messages.SHAME_GROUP_HEADER)]
    for t in overdue[:15]:
        uid = storage.get_chat_id_by_notion_name(t.assignee) if t.assignee else None
        name_repr = _mention_or_name(uid, t.assignee or "хтозна-хто")
        lines.append(messages.SHAME_GROUP_LINE.format(name=name_repr, task=_esc(t.title), days=t.days_overdue))
    await update.message.reply_text("\n".join(lines), parse_mode=_parse_mode())


async def cmd_roast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Розважальна команда: гномик миттєво когось соромить (найгіршого боржника)."""
    tasks = notion_service.get_all_tasks()
    overdue = [t for t in tasks if t.deadline and t.days_overdue > 0]
    if not overdue:
        await update.message.reply_text("Нема кого палити, всі встигають. Підозріло чесна команда.")
        return
    worst = max(overdue, key=lambda t: t.days_overdue)
    uid = storage.get_chat_id_by_notion_name(worst.assignee) if worst.assignee else None
    name_repr = _mention_or_name(uid, worst.assignee or "хтозна-хто")
    text = messages.render_reminder(name_repr, _esc(worst.title), worst.days_overdue)
    await update.message.reply_text(text, parse_mode=_parse_mode())


async def cmd_newtask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/newtask Назва | ДД.ММ[.РРРР] | Відповідальний (опційно) | Пілон (опційно)"""
    raw = " ".join(context.args) if context.args else ""
    parts = [p.strip() for p in raw.split("|")]

    if not raw or len(parts) < 2 or not parts[0] or not parts[1]:
        await update.message.reply_text(
            "Використання:\n"
            "/newtask Назва задачі | ДД.ММ[.РРРР] | Відповідальний (опційно) | Пілон (опційно)\n\n"
            "Приклад: /newtask Написати статтю про гранти | 30.08 | Оля | Гранти\n"
            "Якщо не вказати відповідального — таска запишеться на тебе (треба бути зареєстрованим через /register)."
        )
        return

    title = parts[0]
    deadline = _parse_date(parts[1])
    if not deadline:
        await update.message.reply_text(f"Не розібрав дату «{parts[1]}». Формат: ДД.ММ або ДД.ММ.РРРР.")
        return

    assignee_raw = parts[2] if len(parts) > 2 and parts[2] else None
    pillar = parts[3] if len(parts) > 3 else ""

    if assignee_raw:
        known = notion_service.list_known_assignees()
        known_lower = {k.lower(): k for k in known}
        if assignee_raw.lower() not in known_lower:
            await update.message.reply_text(
                messages.REGISTER_UNKNOWN_NAME.format(
                    name=assignee_raw, known=", ".join(known) if known else "(база порожня)"
                )
            )
            return
        assignee = known_lower[assignee_raw.lower()]
    else:
        user = storage.get_by_chat_id(update.effective_user.id)
        if not user:
            await update.message.reply_text(
                "Ти не вказав(-ла) відповідального і сам(-а) не зареєстрований(-а). "
                "Напиши /register Ім'я в приваті, або додай ім'я в команду через «|»."
            )
            return
        assignee = user["notion_name"]

    try:
        task = notion_service.create_task(title=title, assignee=assignee, deadline=deadline, pillar=pillar)
    except Exception as e:
        log.exception("Не вдалось створити таску в Notion")
        await update.message.reply_text(f"Не вийшло записати в Notion: {e}")
        return

    await update.message.reply_text(
        f"Записав(-ла) у Notion: «{task.title}», відповідальний(-а) {assignee}, "
        f"дедлайн {deadline.strftime('%d.%m.%Y')}. Тепер гномик і за цим стежитиме."
    )

    target_uid = storage.get_chat_id_by_notion_name(assignee)
    name_repr = _mention_or_name(target_uid, assignee)
    announce = messages.pick(messages.NEW_TASK_FROM_BOT).format(
        name=name_repr, task=_esc(task.title), deadline=deadline.strftime("%d.%m")
    )
    await _deliver(context, target_uid or update.effective_user.id, announce, thread_id=config.DAILY_DIGEST_THREAD_ID)

# ------------------------------------------------------------------
# Заплановані завдання
# ------------------------------------------------------------------

async def job_daily_digest(context: ContextTypes.DEFAULT_TYPE):
    """О 10:00: список тасок людини на ПОТОЧНИЙ тиждень (пн–нд),
    плюс окремо — що вже прострочено з минулого і досі не закрито."""
    log.info("Running daily digest")
    monday, sunday = _week_bounds()
    users = storage.all_users()
    for uid_str, info in users.items():
        uid = int(uid_str)
        tasks = notion_service.get_tasks_for_person(info["notion_name"])
        if not tasks:
            continue

        this_week = [t for t in tasks if t.deadline and monday <= t.deadline <= sunday]
        stale_overdue = [t for t in tasks if t.deadline and t.deadline < monday and t.days_overdue > 0]
        no_deadline = [t for t in tasks if not t.deadline]

        if not this_week and not stale_overdue and not no_deadline:
            continue

        this_week.sort(key=lambda t: t.deadline)
        name_repr = _mention_or_name(uid, info["notion_name"])

        header = messages.pick(messages.DIGEST_HEADER)
        lines = [f"{name_repr}, {header.lower()}" if config.TEAM_CHAT_ID else header, ""]
        lines.append(f"📅 Твій тиждень ({monday.strftime('%d.%m')}–{sunday.strftime('%d.%m')}):")
        if this_week:
            lines += [f"• {_format_task_line(t)}" for t in this_week]
        else:
            lines.append("— на цей тиждень дедлайнів немає (насолоджуйся, поки можеш)")

        if stale_overdue:
            stale_overdue.sort(key=lambda t: -t.days_overdue)
            lines.append("")
            lines.append("🔥 А ще це досі висить з минулого і не закрито:")
            lines += [f"• {_format_task_line(t)}" for t in stale_overdue]

        if no_deadline:
            lines.append("")
            lines.append("❔ Без дедлайну (не забудь колись його поставити):")
            lines += [f"• «{_esc(t.title)}» — {_esc(t.status)}" for t in no_deadline]

        lines += ["", messages.pick(messages.DIGEST_FOOTER)]
        await _deliver(context, uid, "\n".join(lines))

    if config.TEAM_CHAT_ID:
        all_tasks = notion_service.get_all_tasks()
        overdue = sorted(
            [t for t in all_tasks if t.deadline and t.days_overdue > 0],
            key=lambda t: -t.days_overdue,
        )
        if overdue:
            lines = [messages.pick(messages.SHAME_GROUP_HEADER)]
            for t in overdue[:10]:
                uid = storage.get_chat_id_by_notion_name(t.assignee) if t.assignee else None
                name_repr = _mention_or_name(uid, t.assignee or "хтозна-хто")
                lines.append(messages.SHAME_GROUP_LINE.format(name=name_repr, task=_esc(t.title), days=t.days_overdue))
        else:
            lines = [messages.pick(messages.SHAME_GROUP_CLEAN)]
        try:
            await context.bot.send_message(chat_id=config.TEAM_CHAT_ID, text="\n".join(lines), parse_mode=_parse_mode())
        except Exception as e:
            log.warning("Не вдалось надіслати груповий звіт: %s", e)


async def job_deadline_watch(context: ContextTypes.DEFAULT_TYPE):
    """Раз на день: цільові нагадування "2 дні до дедлайну і ще не почато"
    та "1 день до дедлайну" (незалежно від статусу)."""
    log.info("Running deadline watch (2-day / 1-day)")
    users = storage.all_users()
    for uid_str, info in users.items():
        uid = int(uid_str)
        tasks = notion_service.get_tasks_for_person(info["notion_name"])
        name_repr = _mention_or_name(uid, info["notion_name"])
        for t in tasks:
            if not t.deadline:
                continue
            days_left = -t.days_overdue

            if days_left == 2 and t.status in config.NOT_STARTED_STATUSES:
                text = messages.render_two_days_not_started(name_repr, _esc(t.title), _esc(t.status))
            elif days_left == 1:
                text = messages.render_one_day_left(name_repr, _esc(t.title), _esc(t.status))
            else:
                continue

            await _deliver(context, uid, text)


async def job_periodic_nag(context: ContextTypes.DEFAULT_TYPE):
    now_local = datetime.now(TZ)
    if not (config.WORKDAY_START_HOUR <= now_local.hour < config.WORKDAY_END_HOUR):
        return  # гномик теж має право на сон

    log.info("Running periodic nag")
    users = storage.all_users()
    for uid_str, info in users.items():
        uid = int(uid_str)
        tasks = notion_service.get_tasks_for_person(info["notion_name"])
        overdue = [t for t in tasks if t.deadline and t.days_overdue > 0]
        if not overdue:
            continue
        worst = max(overdue, key=lambda t: t.days_overdue)
        name_repr = _mention_or_name(uid, info["notion_name"])
        text = messages.render_reminder(name_repr, _esc(worst.title), worst.days_overdue)
        if len(overdue) > 1:
            text += f"\n\n(і ще {len(overdue) - 1} прострочен(а/і) таска(и) чекають, до речі)"
        await _deliver(context, uid, text)


# ------------------------------------------------------------------
# Двосторонній зв'язок: вебхук від Notion (Notion -> бот)
# ------------------------------------------------------------------
# Коли хтось створює/змінює таску прямо в Notion, Notion шле сюди POST-запит.
# Це опційна, "просунута" частина — без неї бот все одно повністю робочий,
# просто дізнається про зміни в Notion лише під час планових перевірок,
# а не миттєво. Налаштування — див. README, розділ "Двосторонній зв'язок".

async def _handle_notion_event(application: Application, event_type: str, page_id: str):
    task = notion_service.get_page_by_id(page_id)
    if not task:
        return  # чужа база або сторінку вже видалили

    uid = storage.get_chat_id_by_notion_name(task.assignee) if task.assignee else None
    name_repr = _mention_or_name(uid, task.assignee or "хтозна-хто")
    deadline_str = task.deadline.strftime("%d.%m") if task.deadline else "без дедлайну"

    if event_type == "page.created":
        text = messages.pick(messages.NEW_TASK_FROM_NOTION).format(
            name=name_repr, task=_esc(task.title), deadline=deadline_str
        )
    else:
        text = messages.pick(messages.TASK_UPDATED_FROM_NOTION).format(
            name=name_repr, task=_esc(task.title), status=_esc(task.status)
        )

    dest = config.TEAM_CHAT_ID or uid
    if not dest:
        return
    kwargs = {"chat_id": dest, "text": text, "parse_mode": _parse_mode()}
    if config.TEAM_CHAT_ID and config.DAILY_DIGEST_THREAD_ID:
        kwargs["message_thread_id"] = config.DAILY_DIGEST_THREAD_ID
    try:
        await application.bot.send_message(**kwargs)
    except Exception as e:
        log.warning("Не вдалось надіслати повідомлення про подію з Notion: %s", e)


async def handle_notion_webhook(request: web.Request) -> web.Response:
    body = await request.read()
    try:
        payload = await request.json()
    except Exception:
        return web.Response(status=400, text="bad json")

    # Крок підтвердження: Notion один раз шле verification_token без підпису.
    if "verification_token" in payload:
        log.warning(
            "=== NOTION WEBHOOK VERIFICATION TOKEN ===\n%s\n"
            "Встав це значення в NOTION_WEBHOOK_SECRET у .env і перезапусти бота "
            "(або підтверди підписку в Notion → Settings → Integrations → Webhooks).",
            payload["verification_token"],
        )
        return web.Response(text="ok")

    if config.NOTION_WEBHOOK_SECRET:
        signature = request.headers.get("X-Notion-Signature", "")
        expected = "sha256=" + hmac.new(
            config.NOTION_WEBHOOK_SECRET.encode(), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            log.warning("Notion webhook: підпис не збігається, ігнорую запит")
            return web.Response(status=401, text="bad signature")
    else:
        log.warning("Notion webhook: NOTION_WEBHOOK_SECRET не задано, приймаю без перевірки підпису (небезпечно на проді)")

    event_type = payload.get("type", "")
    entity = payload.get("entity", {}) or {}
    page_id = entity.get("id")

    if page_id and event_type in ("page.created", "page.properties_updated"):
        application: Application = request.app["bot_app"]
        asyncio.create_task(_handle_notion_event(application, event_type, page_id))

    return web.Response(text="ok")


# ------------------------------------------------------------------
# Точка входу
# ------------------------------------------------------------------

async def run():
    application = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("register", cmd_register))
    application.add_handler(CommandHandler("tasks", cmd_tasks))
    application.add_handler(CommandHandler("report", cmd_report))
    application.add_handler(CommandHandler("roast", cmd_roast))
    application.add_handler(CommandHandler("newtask", cmd_newtask))
    application.add_handler(CallbackQueryHandler(on_status_button, pattern=r"^st\|"))

    jq = application.job_queue
    jq.run_daily(
        job_daily_digest,
        time=dtime(hour=config.DAILY_DIGEST_HOUR, minute=config.DAILY_DIGEST_MINUTE, tzinfo=TZ),
    )
    jq.run_daily(
        job_deadline_watch,
        time=dtime(
            hour=config.DAILY_DIGEST_HOUR,
            minute=config.DAILY_DIGEST_MINUTE + config.DEADLINE_WATCH_MINUTE_OFFSET,
            tzinfo=TZ,
        ),
    )
    for hour in config.NAG_HOURS:
        jq.run_daily(job_periodic_nag, time=dtime(hour=hour, minute=0, tzinfo=TZ))
        
    aio_app = web.Application()
    aio_app["bot_app"] = application
    aio_app.router.add_post(config.NOTION_WEBHOOK_PATH, handle_notion_webhook)
    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.WEBHOOK_PORT)

    async with application:
        await application.start()
        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        await site.start()
        log.info(
            "Матюкливий гномик прокинувся. Вебхук від Notion слухаю на порту %s, шлях %s",
            config.WEBHOOK_PORT, config.NOTION_WEBHOOK_PATH,
        )
        try:
            await asyncio.Event().wait()  # тримаємо процес живим, поки не зупинять
        finally:
            await application.updater.stop()
            await application.stop()
            await runner.cleanup()


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
