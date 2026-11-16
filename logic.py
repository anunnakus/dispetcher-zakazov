# -*- coding: utf-8 -*-
"""Правила диспетчера заказов: статусы, шаблоны, напоминания, отправка."""

import csv
import io
import json
import re
import smtplib
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "desk.sqlite"
INBOX = ROOT / "входящие"
DONE_DIR = INBOX / "обработано"

LOCK = threading.Lock()

STATUSES = {
    "new": "Новый",
    "awaiting_payment": "Ждёт оплату",
    "paid": "Оплачен",
    "assembling": "В сборке",
    "shipped": "В доставке",
    "ready_pickup": "Готов к выдаче",
    "problem": "Проблема",
    "closed": "Закрыт",
}

DELIVERY = {
    "courier": "курьер",
    "pickup": "самовывоз",
    "transport": "транспортная компания",
}

STATUS_ALIASES = {}
for key, label in STATUSES.items():
    STATUS_ALIASES[key] = key
    STATUS_ALIASES[label.lower()] = key
    STATUS_ALIASES[label.lower().replace("ё", "е")] = key
STATUS_ALIASES["ждет оплату"] = "awaiting_payment"
STATUS_ALIASES["самовывоз"] = "ready_pickup"
STATUS_ALIASES["отгружен"] = "shipped"
STATUS_ALIASES["доставка"] = "shipped"
STATUS_ALIASES["оплачен"] = "paid"
STATUS_ALIASES["проблема"] = "problem"
STATUS_ALIASES["закрыт"] = "closed"
STATUS_ALIASES["сборка"] = "assembling"
STATUS_ALIASES["в сборке"] = "assembling"

ENTRY_TEMPLATES = {
    "new": ["order_accepted"],
    "awaiting_payment": ["awaiting_payment"],
    "paid": ["paid_client"],
    "shipped": ["shipped"],
    "ready_pickup": ["ready_pickup"],
}

DEFAULT_TEMPLATES = [
    (
        "order_accepted",
        "Заказ принят",
        "Здравствуйте, {customer}! Заказ {number} принят. Состав: {items}. Сумма {amount} ₽. "
        "Вопросы: {phone}. {store}.",
    ),
    (
        "awaiting_payment",
        "Ждём оплату",
        "{customer}, заказ {number} на сумму {amount} ₽ ждёт оплату. {payment_details} "
        "После оплаты сразу передадим в сборку. {store}, {phone}.",
    ),
    (
        "payment_reminder",
        "Напоминание об оплате",
        "Напоминаем: заказ {number} на {amount} ₽ ещё не оплачен. Если уже оплатили, "
        "пришлите чек в ответ. {payment_details} {store}, {phone}.",
    ),
    (
        "paid_client",
        "Оплата получена",
        "{customer}, оплата по заказу {number} получена. Собираем и напишем, когда "
        "передадим в доставку или подготовим к выдаче. {store}.",
    ),
    (
        "shipped",
        "Передан в доставку",
        "{customer}, заказ {number} передан в доставку. Трек: {track}. {store}, {phone}.",
    ),
    (
        "ready_pickup",
        "Готов к выдаче",
        "{customer}, заказ {number} готов к выдаче. Адрес: {pickup_address}. "
        "Часы: {pickup_hours}. Назовите номер {number}. {phone}.",
    ),
    (
        "pickup_reminder",
        "Напоминание о самовывозе",
        "{customer}, заказ {number} ждёт вас: {pickup_address}, {pickup_hours}. "
        "Если планы изменились, позвоните {phone}.",
    ),
    (
        "warehouse",
        "Задание складу",
        "Собрать заказ {number}. {customer}, {phone}. {items}. Сумма {amount} ₽. "
        "Доставка: {delivery}. Адрес: {address}. Комментарий: {comment}.",
    ),
]

DEFAULT_SETTINGS = {
    "store_name": "Магазин аудиотехники",
    "manager_name": "",
    "pickup_address": "",
    "pickup_hours": "ежедневно с 10:00 до 20:00",
    "store_phone": "",
    "payment_details": "Ссылку на оплату пришлём отдельно, если её ещё нет в заказе.",
    "payment_reminder_hours": "24",
    "payment_task_hours": "48",
    "pickup_reminder_hours": "24",
    "pickup_task_hours": "72",
    "auto_send": "0",
    "sms_enabled": "0",
    "smsru_api_id": "",
    "email_enabled": "0",
    "smtp_host": "smtp.gmail.com",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",
    "telegram_enabled": "0",
    "telegram_token": "",
    "telegram_warehouse_chat": "",
}


class DeskError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def now():
    return datetime.now().replace(microsecond=0)


def iso(dt):
    return dt.isoformat(timespec="seconds")


def parse_dt(value):
    if not value:
        return None
    value = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def format_amount(value):
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return str(value or "0")
    if number.is_integer():
        text = f"{int(number):,}".replace(",", " ")
    else:
        text = f"{number:,.2f}".replace(",", " ").replace(".", ",")
    return text


def normalize_phone(value):
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    return digits


def display_phone(value):
    digits = normalize_phone(value)
    if len(digits) == 11 and digits.startswith("7"):
        return f"+7 {digits[1:4]} {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"
    return value or ""


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    INBOX.mkdir(parents=True, exist_ok=True)
    DONE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with LOCK:
        conn = connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY,
                    number TEXT UNIQUE NOT NULL,
                    customer TEXT DEFAULT '',
                    phone TEXT DEFAULT '',
                    email TEXT DEFAULT '',
                    items TEXT DEFAULT '',
                    amount REAL DEFAULT 0,
                    delivery_type TEXT DEFAULT 'transport',
                    address TEXT DEFAULT '',
                    track TEXT DEFAULT '',
                    status TEXT NOT NULL,
                    comment TEXT DEFAULT '',
                    source TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    status_changed_at TEXT NOT NULL,
                    paid_at TEXT DEFAULT '',
                    reminder_payment_count INTEGER DEFAULT 0,
                    reminder_pickup_count INTEGER DEFAULT 0,
                    demo INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY,
                    order_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    done INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY,
                    order_id INTEGER NOT NULL,
                    template_key TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    recipient TEXT DEFAULT '',
                    body TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS templates (
                    key TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS journal (
                    id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    text TEXT NOT NULL
                );
                """
            )
            for key, title, body in DEFAULT_TEMPLATES:
                conn.execute(
                    "INSERT OR IGNORE INTO templates(key, title, body) VALUES (?, ?, ?)",
                    (key, title, body),
                )
            for key, value in DEFAULT_SETTINGS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                    (key, value),
                )
            conn.commit()
        finally:
            conn.close()


def _journal(conn, text):
    conn.execute(
        "INSERT INTO journal(created_at, text) VALUES (?, ?)",
        (iso(now()), text),
    )


def _settings_map(conn):
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    data = dict(DEFAULT_SETTINGS)
    data.update({row["key"]: row["value"] for row in rows})
    return data


def _order_dict(row):
    if row is None:
        return None
    data = dict(row)
    data["status_label"] = STATUSES.get(data["status"], data["status"])
    data["delivery_label"] = DELIVERY.get(data["delivery_type"], data["delivery_type"])
    data["phone_display"] = display_phone(data.get("phone"))
    data["amount_display"] = format_amount(data.get("amount"))
    return data


def _render(body, order, settings):
    values = {
        "customer": order.get("customer") or "клиент",
        "number": order.get("number") or "",
        "items": order.get("items") or "состав уточняется",
        "amount": format_amount(order.get("amount")),
        "track": order.get("track") or "",
        "address": order.get("address") or "адрес не указан",
        "comment": order.get("comment") or "—",
        "delivery": DELIVERY.get(order.get("delivery_type"), "доставка"),
        "phone": settings.get("store_phone") or "",
        "store": settings.get("store_name") or "",
        "manager": settings.get("manager_name") or "",
        "pickup_address": settings.get("pickup_address") or "адрес пункта выдачи не заполнен в настройках",
        "pickup_hours": settings.get("pickup_hours") or "",
        "payment_details": settings.get("payment_details") or "",
    }
    return re.sub(r"\{([a-z_]+)\}", lambda match: str(values.get(match.group(1), "")), body)


def _template(conn, key):
    row = conn.execute("SELECT key, title, body FROM templates WHERE key = ?", (key,)).fetchone()
    if row is None:
        raise DeskError(f"Нет шаблона {key}")
    return dict(row)


def _has_message(conn, order_id, template_key):
    row = conn.execute(
        "SELECT id FROM messages WHERE order_id = ? AND template_key = ? AND status != 'failed'",
        (order_id, template_key),
    ).fetchone()
    return row is not None


def _open_task(conn, order_id, kind):
    row = conn.execute(
        "SELECT id FROM tasks WHERE order_id = ? AND kind = ? AND done = 0",
        (order_id, kind),
    ).fetchone()
    return row is not None


def _add_task(conn, order, kind, title):
    if _open_task(conn, order["id"], kind):
        return
    conn.execute(
        "INSERT INTO tasks(order_id, kind, title, done, created_at) VALUES (?, ?, ?, 0, ?)",
        (order["id"], kind, title, iso(now())),
    )
    _journal(conn, f"Заказ {order['number']}: задача «{title}»")


def _channel_ready(settings, channel):
    if channel == "sms":
        return settings.get("sms_enabled") == "1" and bool(settings.get("smsru_api_id"))
    if channel == "email":
        return (
            settings.get("email_enabled") == "1"
            and bool(settings.get("smtp_host"))
            and bool(settings.get("smtp_user"))
            and bool(settings.get("smtp_password"))
        )
    if channel == "telegram":
        return (
            settings.get("telegram_enabled") == "1"
            and bool(settings.get("telegram_token"))
            and bool(settings.get("telegram_warehouse_chat"))
        )
    return False


def _choose_channel(order, settings, template_key):
    if template_key == "warehouse":
        return "telegram", settings.get("telegram_warehouse_chat") or "склад"
    if _channel_ready(settings, "sms") and normalize_phone(order.get("phone")):
        return "sms", normalize_phone(order.get("phone"))
    if _channel_ready(settings, "email") and order.get("email"):
        return "email", order.get("email")
    recipient = display_phone(order.get("phone")) or order.get("email") or "клиент"
    return "manual", recipient


def _queue_template(conn, order, settings, template_key):
    if template_key == "shipped" and not (order.get("track") or "").strip():
        _add_task(conn, order, "track", "Вписать трек, иначе клиент не получит сообщение")
        return None
    if _has_message(conn, order["id"], template_key):
        return None
    template = _template(conn, template_key)
    body = _render(template["body"], order, settings).strip()
    channel, recipient = _choose_channel(order, settings, template_key)
    status = "draft"
    error = ""
    if settings.get("auto_send") == "1" and channel in ("sms", "email", "telegram") and _channel_ready(settings, channel):
        try:
            _deliver(settings, channel, recipient, body, order)
            status = "sent"
        except DeskError as exc:
            status = "failed"
            error = exc.message
    conn.execute(
        """INSERT INTO messages(order_id, template_key, channel, recipient, body, status, error, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (order["id"], template_key, channel, recipient, body, status, error, iso(now())),
    )
    label = {"sent": "отправлено", "failed": "ошибка отправки", "draft": "готово к копированию"}[status]
    _journal(conn, f"Заказ {order['number']}: {template['title']} — {label}")
    return status


def _entry_rules(conn, order, settings):
    for key in ENTRY_TEMPLATES.get(order["status"], []):
        _queue_template(conn, order, settings, key)
    if order["status"] == "paid":
        _add_task(conn, order, "warehouse", f"Собрать заказ {order['number']}")
        _queue_template(conn, order, settings, "warehouse")
    if order["status"] == "problem":
        _add_task(conn, order, "manager", f"Разобрать проблему по заказу {order['number']}")
    if order["status"] == "shipped" and not (order.get("track") or "").strip():
        _add_task(conn, order, "track", "Вписать трек, иначе клиент не получит сообщение")


def _clean_text(value, limit=2000):
    return re.sub(r"\s+", " ", (value or "")).strip()[:limit]


def _require_number(value):
    number = _clean_text(value, 40)
    if not number:
        raise DeskError("Нужен номер заказа")
    return number


def create_order(payload):
    with LOCK:
        conn = connect()
        try:
            settings = _settings_map(conn)
            number = _require_number(payload.get("number"))
            exists = conn.execute("SELECT id FROM orders WHERE number = ?", (number,)).fetchone()
            if exists:
                raise DeskError(f"Заказ {number} уже есть")
            status = payload.get("status") or "awaiting_payment"
            if status not in STATUSES:
                raise DeskError("Неизвестный статус")
            if status == "shipped" and not _clean_text(payload.get("track"), 80):
                raise DeskError("Для доставки нужен трек-номер")
            if status == "problem" and not _clean_text(payload.get("comment"), 1000):
                raise DeskError("Для проблемы напишите, что случилось")
            moment = iso(parse_dt(payload.get("created_at")) or now())
            delivery = payload.get("delivery_type") or "transport"
            if delivery not in DELIVERY:
                delivery = "transport"
            try:
                amount = float(str(payload.get("amount") or "0").replace(" ", "").replace(",", "."))
            except ValueError:
                raise DeskError("Сумма должна быть числом")
            conn.execute(
                """INSERT INTO orders(
                    number, customer, phone, email, items, amount, delivery_type, address,
                    track, status, comment, source, created_at, status_changed_at, paid_at, demo
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    number,
                    _clean_text(payload.get("customer"), 200),
                    normalize_phone(payload.get("phone")),
                    _clean_text(payload.get("email"), 200),
                    _clean_text(payload.get("items"), 1000),
                    amount,
                    delivery,
                    _clean_text(payload.get("address"), 400),
                    _clean_text(payload.get("track"), 80),
                    status,
                    _clean_text(payload.get("comment"), 1000),
                    _clean_text(payload.get("source"), 40) or "вручную",
                    moment,
                    moment,
                    moment if status in ("paid", "assembling", "shipped", "ready_pickup", "closed") else "",
                    1 if payload.get("demo") else 0,
                ),
            )
            order = _order_dict(conn.execute("SELECT * FROM orders WHERE number = ?", (number,)).fetchone())
            _journal(conn, f"Заказ {number} создан, статус «{STATUSES[status]}»")
            _entry_rules(conn, order, settings)
            conn.commit()
            return get_order_in(conn, order["id"])
        finally:
            conn.close()


def get_order_in(conn, order_id):
    order = _order_dict(conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())
    if not order:
        raise DeskError("Заказ не найден")
    order["tasks"] = [dict(row) for row in conn.execute(
        "SELECT * FROM tasks WHERE order_id = ? ORDER BY done, id DESC", (order_id,)
    )]
    order["messages"] = [dict(row) for row in conn.execute(
        "SELECT * FROM messages WHERE order_id = ? ORDER BY id DESC", (order_id,)
    )]
    return order


def get_order(order_id):
    conn = connect()
    try:
        return get_order_in(conn, order_id)
    finally:
        conn.close()


def list_orders(status="", query=""):
    conn = connect()
    try:
        sql = "SELECT * FROM orders WHERE 1=1"
        args = []
        if status and status != "all":
            sql += " AND status = ?"
            args.append(status)
        if query:
            sql += " AND (number LIKE ? OR customer LIKE ? OR phone LIKE ? OR items LIKE ? OR track LIKE ?)"
            like = f"%{query.strip()}%"
            phone = f"%{normalize_phone(query) or query.strip()}%"
            args.extend([like, like, phone, like, like])
        sql += " ORDER BY CASE status WHEN 'problem' THEN 0 WHEN 'awaiting_payment' THEN 1 ELSE 2 END, id DESC"
        return [_order_dict(row) for row in conn.execute(sql, args)]
    finally:
        conn.close()


def change_status(order_id, payload):
    with LOCK:
        conn = connect()
        try:
            order = _order_dict(conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())
            if not order:
                raise DeskError("Заказ не найден")
            status = payload.get("status")
            if status not in STATUSES:
                raise DeskError("Неизвестный статус")
            track = order["track"]
            if "track" in payload:
                track = _clean_text(payload.get("track"), 80)
            comment = order["comment"]
            if payload.get("comment"):
                comment = _clean_text(payload.get("comment"), 1000)
            if status == "shipped" and not track:
                raise DeskError("Сначала впишите трек-номер. Без него программа не пишет клиенту.")
            if status == "problem" and not comment:
                raise DeskError("Напишите, в чём проблема. Это единственное, что программа не придумывает сама.")
            moment = iso(now())
            paid_at = order["paid_at"]
            if status == "paid" and not paid_at:
                paid_at = moment
            reminder_payment = 0 if status == "awaiting_payment" else order["reminder_payment_count"]
            reminder_pickup = 0 if status == "ready_pickup" else order["reminder_pickup_count"]
            same = status == order["status"] and track == order["track"] and comment == order["comment"]
            conn.execute(
                """UPDATE orders SET status = ?, track = ?, comment = ?, status_changed_at = ?,
                   paid_at = ?, reminder_payment_count = ?, reminder_pickup_count = ? WHERE id = ?""",
                (
                    status,
                    track,
                    comment,
                    order["status_changed_at"] if same else moment,
                    paid_at,
                    reminder_payment,
                    reminder_pickup,
                    order_id,
                ),
            )
            updated = _order_dict(conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())
            settings = _settings_map(conn)
            if status != order["status"]:
                _journal(conn, f"Заказ {order['number']}: {STATUSES[order['status']]} → {STATUSES[status]}")
                _entry_rules(conn, updated, settings)
            elif track and not order["track"] and status == "shipped":
                _queue_template(conn, updated, settings, "shipped")
                _journal(conn, f"Заказ {order['number']}: трек добавлен")
            conn.commit()
            return get_order_in(conn, order_id)
        finally:
            conn.close()


def update_order(order_id, payload):
    with LOCK:
        conn = connect()
        try:
            order = _order_dict(conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())
            if not order:
                raise DeskError("Заказ не найден")
            delivery = payload.get("delivery_type") or order["delivery_type"]
            if delivery not in DELIVERY:
                raise DeskError("Неизвестный способ доставки")
            try:
                amount = float(str(payload.get("amount") if payload.get("amount") is not None else order["amount"]).replace(" ", "").replace(",", "."))
            except ValueError:
                raise DeskError("Сумма должна быть числом")
            track = _clean_text(payload.get("track", order["track"]), 80)
            conn.execute(
                """UPDATE orders SET customer = ?, phone = ?, email = ?, items = ?, amount = ?,
                   delivery_type = ?, address = ?, track = ?, comment = ? WHERE id = ?""",
                (
                    _clean_text(payload.get("customer", order["customer"]), 200),
                    normalize_phone(payload.get("phone", order["phone"])),
                    _clean_text(payload.get("email", order["email"]), 200),
                    _clean_text(payload.get("items", order["items"]), 1000),
                    amount,
                    delivery,
                    _clean_text(payload.get("address", order["address"]), 400),
                    track,
                    _clean_text(payload.get("comment", order["comment"]), 1000),
                    order_id,
                ),
            )
            updated = _order_dict(conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())
            if track and not order["track"] and updated["status"] == "shipped":
                _queue_template(conn, updated, _settings_map(conn), "shipped")
            conn.commit()
            return get_order_in(conn, order_id)
        finally:
            conn.close()


def delete_order(order_id):
    with LOCK:
        conn = connect()
        try:
            order = conn.execute("SELECT number FROM orders WHERE id = ?", (order_id,)).fetchone()
            if not order:
                raise DeskError("Заказ не найден")
            conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
            _journal(conn, f"Заказ {order['number']} удалён")
            conn.commit()
        finally:
            conn.close()


def complete_task(task_id):
    with LOCK:
        conn = connect()
        try:
            task = conn.execute(
                """SELECT tasks.id, tasks.title, orders.number AS number
                   FROM tasks JOIN orders ON orders.id = tasks.order_id WHERE tasks.id = ?""",
                (task_id,),
            ).fetchone()
            if not task:
                raise DeskError("Задача не найдена")
            conn.execute("UPDATE tasks SET done = 1 WHERE id = ?", (task_id,))
            _journal(conn, f"Заказ {task['number']}: задача закрыта — {task['title']}")
            conn.commit()
        finally:
            conn.close()


def list_templates():
    conn = connect()
    try:
        return [dict(row) for row in conn.execute("SELECT key, title, body FROM templates ORDER BY rowid")]
    finally:
        conn.close()


def save_template(key, body):
    with LOCK:
        conn = connect()
        try:
            row = conn.execute("SELECT key FROM templates WHERE key = ?", (key,)).fetchone()
            if not row:
                raise DeskError("Шаблон не найден")
            text = (body or "").strip()
            if not text:
                raise DeskError("Текст шаблона пустой")
            conn.execute("UPDATE templates SET body = ? WHERE key = ?", (text, key))
            conn.commit()
        finally:
            conn.close()


def public_settings():
    conn = connect()
    try:
        data = _settings_map(conn)
    finally:
        conn.close()
    secret_keys = ("smsru_api_id", "smtp_password", "telegram_token")
    view = dict(data)
    for key in secret_keys:
        view[key + "_set"] = "1" if data.get(key) else "0"
        view[key] = ""
    view["statuses"] = STATUSES
    view["delivery"] = DELIVERY
    return view


def save_settings(payload):
    with LOCK:
        conn = connect()
        try:
            current = _settings_map(conn)
            allowed = set(DEFAULT_SETTINGS)
            for key, value in payload.items():
                if key not in allowed:
                    continue
                if key in ("smsru_api_id", "smtp_password", "telegram_token") and not str(value or "").strip():
                    continue
                if key.endswith("_hours"):
                    try:
                        hours = int(value)
                    except (TypeError, ValueError):
                        raise DeskError("Часы должны быть целым числом")
                    if hours < 1 or hours > 240:
                        raise DeskError("Интервал напоминания: от 1 до 240 часов")
                    value = str(hours)
                if key in ("auto_send", "sms_enabled", "email_enabled", "telegram_enabled"):
                    value = "1" if str(value) in ("1", "true", "True", "on") else "0"
                current[key] = str(value or "")
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, current[key]),
                )
            _journal(conn, "Настройки сохранены")
            conn.commit()
        finally:
            conn.close()
    return public_settings()


def _deliver(settings, channel, recipient, body, order):
    if channel == "sms":
        _send_sms(settings, recipient, body)
    elif channel == "email":
        _send_email(settings, recipient, body, order)
    elif channel == "telegram":
        _send_telegram(settings, body)
    else:
        raise DeskError("Канал не настроен")


def _send_sms(settings, phone, body):
    phone = normalize_phone(phone)
    if len(phone) < 11:
        raise DeskError("В заказе нет нормального телефона")
    query = urllib.parse.urlencode({
        "api_id": settings.get("smsru_api_id"),
        "to": phone,
        "msg": body,
        "json": 1,
    })
    request = urllib.request.Request(
        "https://sms.ru/sms/send?" + query,
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise DeskError(f"SMS.ru не ответил: {exc.reason}")
    if str(payload.get("status")) != "OK":
        raise DeskError(payload.get("status_text") or "SMS.ru отклонил сообщение")
    sms = (payload.get("sms") or {}).get(phone) or {}
    if sms and sms.get("status") != "OK":
        raise DeskError(sms.get("status_text") or "SMS не принято")


def _send_email(settings, recipient, body, order):
    if not recipient or "@" not in recipient:
        raise DeskError("В заказе нет почты")
    message = EmailMessage()
    message["Subject"] = f"Заказ {order.get('number')} — {settings.get('store_name') or 'магазин'}"
    message["From"] = settings.get("smtp_from") or settings.get("smtp_user")
    message["To"] = recipient
    message.set_content(body)
    try:
        with smtplib.SMTP(settings.get("smtp_host") or "smtp.gmail.com", int(settings.get("smtp_port") or 587), timeout=20) as smtp:
            smtp.starttls()
            smtp.login(settings.get("smtp_user"), settings.get("smtp_password"))
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise DeskError(f"Почта не ушла: {exc}")


def _send_telegram(settings, body):
    url = f"https://api.telegram.org/bot{settings.get('telegram_token')}/sendMessage"
    data = json.dumps({
        "chat_id": settings.get("telegram_warehouse_chat"),
        "text": body,
    }).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise DeskError(f"Telegram отказал: {detail[:300]}")
    except urllib.error.URLError as exc:
        raise DeskError(f"Telegram не ответил: {exc.reason}")
    if not payload.get("ok"):
        raise DeskError(payload.get("description") or "Telegram отказал")


def send_message(message_id):
    with LOCK:
        conn = connect()
        try:
            row = conn.execute(
                """SELECT messages.*, orders.number AS order_number, orders.customer, orders.phone,
                          orders.email, orders.items, orders.amount, orders.delivery_type, orders.address,
                          orders.track, orders.comment, orders.status
                   FROM messages JOIN orders ON orders.id = messages.order_id
                   WHERE messages.id = ?""",
                (message_id,),
            ).fetchone()
            if not row:
                raise DeskError("Сообщение не найдено")
            message = dict(row)
            settings = _settings_map(conn)
            channel = message["channel"]
            if channel == "manual":
                channel, recipient = _choose_channel(message, settings, message["template_key"])
            else:
                recipient = message["recipient"]
            if channel == "manual" or not _channel_ready(settings, channel):
                raise DeskError("Канал не включён. Скопируйте текст или заполните настройки отправки.")
            try:
                _deliver(settings, channel, recipient, message["body"], message)
            except DeskError as exc:
                conn.execute(
                    "UPDATE messages SET status = 'failed', error = ?, channel = ?, recipient = ? WHERE id = ?",
                    (exc.message, channel, recipient, message_id),
                )
                conn.commit()
                raise
            conn.execute(
                "UPDATE messages SET status = 'sent', error = '', channel = ?, recipient = ? WHERE id = ?",
                (channel, recipient, message_id),
            )
            _journal(conn, f"Заказ {message['order_number']}: сообщение отправлено ({channel})")
            conn.commit()
            return get_order_in(conn, message["order_id"])
        finally:
            conn.close()


def mark_message(message_id, status):
    if status not in ("copied", "sent", "skipped"):
        raise DeskError("Неизвестный статус сообщения")
    with LOCK:
        conn = connect()
        try:
            row = conn.execute(
                """SELECT messages.id, messages.order_id, orders.number AS number
                   FROM messages JOIN orders ON orders.id = messages.order_id WHERE messages.id = ?""",
                (message_id,),
            ).fetchone()
            if not row:
                raise DeskError("Сообщение не найдено")
            conn.execute(
                "UPDATE messages SET status = ?, error = '' WHERE id = ?",
                (status, message_id),
            )
            labels = {"copied": "скопировано вручную", "sent": "отмечено отправленным", "skipped": "пропущено"}
            _journal(conn, f"Заказ {row['number']}: сообщение {labels[status]}")
            conn.commit()
            return get_order_in(conn, row["order_id"])
        finally:
            conn.close()


def _age_hours(value):
    moment = parse_dt(value)
    if not moment:
        return 0
    return (now() - moment).total_seconds() / 3600


def run_tick():
    with LOCK:
        conn = connect()
        try:
            imported = _import_inbox(conn)
            settings = _settings_map(conn)
            payment_reminder = int(settings.get("payment_reminder_hours") or 24)
            payment_task = int(settings.get("payment_task_hours") or 48)
            pickup_reminder = int(settings.get("pickup_reminder_hours") or 24)
            pickup_task = int(settings.get("pickup_task_hours") or 72)
            orders = [_order_dict(row) for row in conn.execute("SELECT * FROM orders")]
            for order in orders:
                age = _age_hours(order["status_changed_at"])
                if order["status"] == "awaiting_payment":
                    if age >= payment_reminder and order["reminder_payment_count"] < 1:
                        _queue_template(conn, order, settings, "payment_reminder")
                        conn.execute(
                            "UPDATE orders SET reminder_payment_count = 1 WHERE id = ?",
                            (order["id"],),
                        )
                    if age >= payment_task:
                        _add_task(conn, order, "manager", f"Оплата заказа {order['number']} не пришла после напоминания")
                if order["status"] == "ready_pickup":
                    if age >= pickup_reminder and order["reminder_pickup_count"] < 1:
                        _queue_template(conn, order, settings, "pickup_reminder")
                        conn.execute(
                            "UPDATE orders SET reminder_pickup_count = 1 WHERE id = ?",
                            (order["id"],),
                        )
                    if age >= pickup_task:
                        _add_task(conn, order, "manager", f"Заказ {order['number']} не забрали")
                if order["status"] == "shipped" and not (order.get("track") or "").strip():
                    _add_task(conn, order, "track", "Вписать трек, иначе клиент не получит сообщение")
            conn.commit()
            return {"imported": imported}
        finally:
            conn.close()


def _import_inbox(conn):
    count = 0
    for path in sorted(INBOX.glob("*.csv")):
        text = _read_text(path)
        count += _import_csv_text(conn, text, source=path.name)
        target = DONE_DIR / f"{now().strftime('%Y%m%d-%H%M%S')}-{path.name}"
        path.replace(target)
    return count


def _read_text(path):
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def import_csv(text):
    with LOCK:
        conn = connect()
        try:
            count = _import_csv_text(conn, text, source="импорт")
            conn.commit()
            return {"imported": count}
        finally:
            conn.close()


def _import_csv_text(conn, text, source):
    sample = text[:2000]
    delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames:
        raise DeskError("В файле нет строки заголовков")
    aliases = {
        "номер": "number",
        "номер заказа": "number",
        "заказ": "number",
        "number": "number",
        "клиент": "customer",
        "фио": "customer",
        "имя": "customer",
        "customer": "customer",
        "телефон": "phone",
        "тел": "phone",
        "phone": "phone",
        "email": "email",
        "почта": "email",
        "e-mail": "email",
        "состав": "items",
        "товары": "items",
        "items": "items",
        "сумма": "amount",
        "amount": "amount",
        "доставка": "delivery_type",
        "способ доставки": "delivery_type",
        "адрес": "address",
        "трек": "track",
        "трек-номер": "track",
        "track": "track",
        "статус": "status",
        "status": "status",
        "комментарий": "comment",
        "дата": "created_at",
        "created_at": "created_at",
    }
    mapping = {}
    for field in reader.fieldnames:
        key = aliases.get((field or "").strip().lower().replace("ё", "е"))
        if key:
            mapping[field] = key
    if "number" not in mapping.values():
        raise DeskError("В файле нужна колонка «номер»")
    settings = _settings_map(conn)
    count = 0
    for raw in reader:
        row = {}
        for field, key in mapping.items():
            row[key] = (raw.get(field) or "").strip()
        if not row.get("number"):
            continue
        _upsert_csv_row(conn, settings, row, source)
        count += 1
    if count:
        _journal(conn, f"Импорт {source}: {count} строк")
    return count


def _map_delivery(value):
    text = (value or "").strip().lower().replace("ё", "е")
    if text in ("pickup", "самовывоз", "выдача"):
        return "pickup"
    if text in ("courier", "курьер", "курьером"):
        return "courier"
    if text in DELIVERY:
        return text
    return "transport"


def _map_status(value):
    text = (value or "").strip().lower().replace("ё", "е")
    if not text:
        return "awaiting_payment"
    if text not in STATUS_ALIASES:
        raise DeskError(f"Не понял статус «{value}»")
    return STATUS_ALIASES[text]


def _amount(value, fallback=0):
    try:
        return float(str(value if value not in (None, "") else fallback).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return float(fallback or 0)


def _upsert_csv_row(conn, settings, row, source):
    number = _require_number(row.get("number"))
    existing = conn.execute("SELECT * FROM orders WHERE number = ?", (number,)).fetchone()
    status = _map_status(row.get("status"))
    incoming_track = _clean_text(row.get("track"), 80)
    asked_to_ship_without_track = status == "shipped" and not incoming_track
    if asked_to_ship_without_track and not existing:
        status = "assembling"
    if existing:
        order = _order_dict(existing)
        track = incoming_track or order["track"]
        comment = _clean_text(row.get("comment"), 1000) or order["comment"]
        if status == "shipped" and not track:
            status = order["status"]
        conn.execute(
            """UPDATE orders SET customer = ?, phone = ?, email = ?, items = ?, amount = ?,
               delivery_type = ?, address = ?, track = ?, comment = ? WHERE id = ?""",
            (
                _clean_text(row.get("customer"), 200) or order["customer"],
                normalize_phone(row.get("phone")) or order["phone"],
                _clean_text(row.get("email"), 200) or order["email"],
                _clean_text(row.get("items"), 1000) or order["items"],
                _amount(row.get("amount"), order["amount"]),
                _map_delivery(row.get("delivery_type")) if row.get("delivery_type") else order["delivery_type"],
                _clean_text(row.get("address"), 400) or order["address"],
                track,
                comment,
                order["id"],
            ),
        )
        if asked_to_ship_without_track and not track:
            fresh = _order_dict(conn.execute("SELECT * FROM orders WHERE id = ?", (order["id"],)).fetchone())
            _add_task(conn, fresh, "track", "Вписать трек, иначе клиент не получит сообщение")
        if status != order["status"]:
            moment = iso(now())
            conn.execute(
                """UPDATE orders SET status = ?, status_changed_at = ?,
                   reminder_payment_count = ?, reminder_pickup_count = ?, paid_at = ?
                   WHERE id = ?""",
                (
                    status,
                    moment,
                    0 if status == "awaiting_payment" else order["reminder_payment_count"],
                    0 if status == "ready_pickup" else order["reminder_pickup_count"],
                    order["paid_at"] or (moment if status == "paid" else ""),
                    order["id"],
                ),
            )
            updated = _order_dict(conn.execute("SELECT * FROM orders WHERE id = ?", (order["id"],)).fetchone())
            _journal(conn, f"Заказ {number}: {STATUSES[order['status']]} → {STATUSES[status]} (импорт)")
            _entry_rules(conn, updated, settings)
        elif track and not order["track"] and status == "shipped":
            updated = _order_dict(conn.execute("SELECT * FROM orders WHERE id = ?", (order["id"],)).fetchone())
            _queue_template(conn, updated, settings, "shipped")
        return
    create_payload = {
        "number": number,
        "customer": row.get("customer"),
        "phone": row.get("phone"),
        "email": row.get("email"),
        "items": row.get("items"),
        "amount": row.get("amount") or 0,
        "delivery_type": _map_delivery(row.get("delivery_type")),
        "address": row.get("address"),
        "track": row.get("track"),
        "status": status,
        "comment": row.get("comment"),
        "created_at": row.get("created_at"),
        "source": source,
    }
    # Вставка без повторного захвата замка.
    moment = iso(parse_dt(create_payload.get("created_at")) or now())
    amount = _amount(create_payload.get("amount"), 0)
    final_status = create_payload["status"]
    conn.execute(
        """INSERT INTO orders(
            number, customer, phone, email, items, amount, delivery_type, address,
            track, status, comment, source, created_at, status_changed_at, paid_at, demo
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
        (
            number,
            _clean_text(create_payload.get("customer"), 200),
            normalize_phone(create_payload.get("phone")),
            _clean_text(create_payload.get("email"), 200),
            _clean_text(create_payload.get("items"), 1000),
            amount,
            create_payload["delivery_type"],
            _clean_text(create_payload.get("address"), 400),
            _clean_text(create_payload.get("track"), 80),
            final_status,
            _clean_text(create_payload.get("comment"), 1000),
            source,
            moment,
            moment,
            moment if final_status in ("paid", "assembling", "shipped", "ready_pickup", "closed") else "",
        ),
    )
    order = _order_dict(conn.execute("SELECT * FROM orders WHERE number = ?", (number,)).fetchone())
    _journal(conn, f"Заказ {number} загружен, статус «{STATUSES[final_status]}»")
    _entry_rules(conn, order, settings)
    if asked_to_ship_without_track:
        _add_task(conn, order, "track", "Вписать трек, иначе клиент не получит сообщение")


def today_view():
    run_tick()
    conn = connect()
    try:
        tasks = [dict(row) for row in conn.execute(
            """SELECT tasks.*, orders.number AS number, orders.customer AS customer, orders.status AS status
               FROM tasks JOIN orders ON orders.id = tasks.order_id
               WHERE tasks.done = 0 ORDER BY tasks.id DESC"""
        )]
        drafts = [dict(row) for row in conn.execute(
            """SELECT messages.*, orders.number AS number, orders.customer AS customer
               FROM messages JOIN orders ON orders.id = messages.order_id
               WHERE messages.status IN ('draft', 'failed') ORDER BY messages.id DESC"""
        )]
        attention = [_order_dict(row) for row in conn.execute(
            """SELECT * FROM orders WHERE status IN ('problem', 'awaiting_payment', 'ready_pickup')
               OR (status = 'shipped' AND (track IS NULL OR track = ''))
               ORDER BY status_changed_at"""
        )]
        journal = [dict(row) for row in conn.execute(
            "SELECT * FROM journal ORDER BY id DESC LIMIT 12"
        )]
        counts = {
            "open": conn.execute("SELECT COUNT(*) c FROM orders WHERE status != 'closed'").fetchone()["c"],
            "tasks": len(tasks),
            "drafts": len(drafts),
            "problem": conn.execute("SELECT COUNT(*) c FROM orders WHERE status = 'problem'").fetchone()["c"],
        }
        return {"tasks": tasks, "drafts": drafts, "attention": attention, "journal": journal, "counts": counts}
    finally:
        conn.close()


def journal_list():
    conn = connect()
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM journal ORDER BY id DESC LIMIT 200")]
    finally:
        conn.close()


def load_demo():
    samples = [
        {
            "number": "ПРИМЕР-101",
            "customer": "Анна Ковалева",
            "phone": "89031234567",
            "email": "",
            "items": "Полочные колонки, пара",
            "amount": 42900,
            "delivery_type": "transport",
            "status": "awaiting_payment",
            "demo": 1,
            "source": "пример",
        },
        {
            "number": "ПРИМЕР-102",
            "customer": "Игорь Семёнов",
            "phone": "+7 903 111-22-33",
            "items": "Интегральный усилитель",
            "amount": 87500,
            "delivery_type": "courier",
            "address": "Москва, пример адреса",
            "status": "paid",
            "demo": 1,
            "source": "пример",
        },
        {
            "number": "ПРИМЕР-103",
            "customer": "Марина Лебедева",
            "phone": "79160001122",
            "items": "Наушники",
            "amount": 19990,
            "delivery_type": "pickup",
            "status": "ready_pickup",
            "demo": 1,
            "source": "пример",
        },
    ]
    created = 0
    for sample in samples:
        conn = connect()
        try:
            exists = conn.execute("SELECT id FROM orders WHERE number = ?", (sample["number"],)).fetchone()
        finally:
            conn.close()
        if exists:
            continue
        create_order(sample)
        created += 1
    # Учебный самовывоз делаем «вчерашним», чтобы сразу было видно напоминание.
    with LOCK:
        conn = connect()
        try:
            yesterday = iso(now() - timedelta(hours=30))
            conn.execute(
                "UPDATE orders SET status_changed_at = ? WHERE number = 'ПРИМЕР-103'",
                (yesterday,),
            )
            conn.commit()
        finally:
            conn.close()
    run_tick()
    return {"created": created}


def clear_demo():
    with LOCK:
        conn = connect()
        try:
            conn.execute("DELETE FROM orders WHERE demo = 1 OR number LIKE 'ПРИМЕР-%'")
            _journal(conn, "Учебные заказы удалены")
            conn.commit()
        finally:
            conn.close()
