# -*- coding: utf-8 -*-
"""Чтение заказов из 1С и передача сообщения в учётную запись 1С."""

import base64
import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from pathlib import Path

import logic

ROOT = Path(__file__).resolve().parent
BRIDGE = ROOT / "onec_bridge.ps1"
PULL_GAP_SECONDS = 300
ODATA_DOCUMENTS = ("Document_ЗаказКлиента", "Document_ЗаказПокупателя")

STATUS_RULES = (
    ("готов к выдаче", "готов к выдаче"),
    ("к выдаче", "готов к выдаче"),
    ("ожида", "ждёт оплату"),
    ("к оплате", "ждёт оплату"),
    ("не оплач", "ждёт оплату"),
    ("к отгруз", "в сборке"),
    ("к обеспеч", "в сборке"),
    ("к сбор", "в сборке"),
    ("сборк", "в сборке"),
    ("обеспеч", "в сборке"),
    ("отгруж", "в доставке"),
    ("в достав", "в доставке"),
    ("передан", "в доставке"),
    ("закрыт", "закрыт"),
    ("выполнен", "закрыт"),
    ("заверш", "закрыт"),
    ("оплачен", "оплачен"),
    ("в работе", "оплачен"),
    ("проблем", "проблема"),
    ("отмен", "проблема"),
    ("нов", "новый"),
)


def connection_ready(settings):
    if (settings.get("onec_file") or "").strip():
        return True
    if (settings.get("onec_server") or "").strip() and (settings.get("onec_ref") or "").strip():
        return True
    return bool((settings.get("onec_url") or "").strip())


def pull_due(settings):
    last = logic.parse_dt(settings.get("onec_last_pull"))
    if not last:
        return True
    return (logic.now() - last).total_seconds() >= PULL_GAP_SECONDS


def _mode(settings):
    if not connection_ready(settings):
        raise logic.DeskError("Укажите папку базы 1С, сервер с именем базы или адрес публикации")
    file_base = (settings.get("onec_file") or "").strip()
    server = (settings.get("onec_server") or "").strip() and (settings.get("onec_ref") or "").strip()
    if file_base or server:
        return "com"
    return "http"


def _days(settings):
    try:
        days = int(settings.get("onec_days") or 7)
    except (TypeError, ValueError):
        days = 7
    return min(60, max(1, days))


def map_status(value):
    text = str(value or "").lower().replace("ё", "е")
    for needle, label in STATUS_RULES:
        if needle in text:
            return label
    return "ждёт оплату"


def normalize_order(raw):
    number = str(raw.get("number") or "").strip()
    if not number:
        return None
    created = str(raw.get("created_at") or "")
    moment = logic.parse_dt(created)
    address = str(raw.get("address") or "")
    delivery = raw.get("delivery_type") or ""
    blob = f"{address} {raw.get('status') or ''} {raw.get('comment') or ''}".lower().replace("ё", "е")
    if not delivery:
        delivery = "pickup" if "самовывоз" in blob else "transport"
    return {
        "number": number,
        "customer": raw.get("customer") or "",
        "phone": raw.get("phone") or "",
        "email": raw.get("email") or "",
        "items": raw.get("items") or "",
        "amount": raw.get("amount") or 0,
        "delivery_type": delivery,
        "address": address,
        "track": raw.get("track") or "",
        "status": map_status(raw.get("status")),
        "comment": raw.get("comment") or "",
        "created_at": created,
        "silent": bool(moment and moment.date() < logic.now().date()),
    }


def fetch_orders(settings):
    if _mode(settings) == "http":
        flat = http_orders(settings)
    else:
        answer = com_call(settings, {"action": "pull", "days": _days(settings)})
        flat = answer.get("orders") or []
        if isinstance(flat, dict):
            flat = [flat]
    orders = []
    for item in flat:
        row = normalize_order(item if "number" in item else flat_from_odata(item))
        if row:
            orders.append(row)
    return orders


def ping(settings):
    if _mode(settings) == "http":
        count = len(http_orders(settings))
        return {"ok": True, "message": f"Публикация 1С открылась, за выбранный срок видно заказов: {count}"}
    answer = com_call(settings, {"action": "ping"})
    return {"ok": True, "message": answer.get("message") or "База 1С открылась"}


def send_message(settings, order, body):
    phone = logic.normalize_phone(order.get("phone"))
    email = (order.get("email") or "").strip()
    if not phone and not email:
        raise logic.DeskError("В заказе нет телефона и почты, 1С некуда отправлять")
    subject = f"Заказ {order.get('number')}"
    if _mode(settings) == "http":
        http_send(settings, phone, email, subject, body)
        return
    com_call(settings, {
        "action": "send",
        "phone": phone,
        "email": email,
        "customer": order.get("customer") or "",
        "subject": subject,
        "text": body,
    })


def flat_from_odata(item):
    contacts = {}
    _collect_contacts(item, contacts)
    goods = item.get("Товары") or item.get("Запасы") or []
    names = []
    if isinstance(goods, list):
        for good in goods[:8]:
            if isinstance(good, dict):
                title = _text(good.get("Номенклатура") or good.get("Description") or good.get("Наименование"))
                if title:
                    names.append(title)
    return {
        "number": _text(item.get("Number") or item.get("Номер")),
        "created_at": _text(item.get("Date") or item.get("Дата")),
        "customer": _text(item.get("Контрагент") or item.get("Партнер") or item.get("Клиент")),
        "phone": contacts.get("phone") or "",
        "email": contacts.get("email") or "",
        "amount": item.get("СуммаДокумента") or item.get("Сумма") or 0,
        "status": _text(item.get("Статус") or item.get("СостояниеЗаказа") or item.get("Состояние")),
        "address": _text(item.get("АдресДоставки") or item.get("Адрес")),
        "comment": _text(item.get("Комментарий") or item.get("Comment")),
        "items": ", ".join(names),
        "track": _text(item.get("ТрекНомер") or item.get("Трек")),
    }


def _text(value):
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("Description", "Наименование", "Представление", "value"):
            if value.get(key):
                return str(value[key])
        return ""
    return str(value)


def _collect_contacts(node, found, depth=0):
    if depth > 4:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            label = str(key).lower()
            if isinstance(value, str):
                if ("email" in label or "почт" in label or "mail" in label) and "@" in value:
                    found.setdefault("email", value)
                digits = "".join(ch for ch in value if ch.isdigit())
                if ("тел" in label or "phone" in label) and len(digits) >= 10:
                    found.setdefault("phone", value)
            else:
                _collect_contacts(value, found, depth + 1)
    elif isinstance(node, list):
        for item in node[:40]:
            _collect_contacts(item, found, depth + 1)


def http_orders(settings):
    since = logic.now() - timedelta(days=_days(settings))
    last_error = "Публикация 1С не ответила списком заказов"
    for document in ODATA_DOCUMENTS:
        path = (
            f"{document}?$format=json&$top=200&$orderby=Date desc"
        )
        try:
            payload = _odata(settings, path)
        except logic.DeskError as exc:
            last_error = exc.message
            continue
        rows = payload.get("value") or []
        fresh = []
        for item in rows:
            flat = flat_from_odata(item)
            moment = logic.parse_dt(flat.get("created_at"))
            if moment and moment < since:
                continue
            fresh.append(flat)
        return fresh
    raise logic.DeskError(last_error)


def http_send(settings, phone, email, subject, body):
    moment = logic.iso(logic.now())
    attempts = []
    if phone:
        attempts.append(("Document_СообщениеSMS", {
            "Date": moment,
            "ТекстСообщения": body,
            "Комментарий": subject,
        }))
    if email:
        attempts.append(("Document_ЭлектронноеПисьмоИсходящее", {
            "Date": moment,
            "Тема": subject,
            "Текст": body,
            "Комментарий": email,
        }))
    errors = []
    for document, payload in attempts:
        try:
            _odata(settings, f"{document}?$format=json", payload)
            return
        except logic.DeskError as exc:
            errors.append(exc.message)
    raise logic.DeskError(errors[-1] if errors else "В публикации 1С некуда записать сообщение")


def _odata(settings, path, payload=None):
    base = (settings.get("onec_url") or "").strip().rstrip("/")
    if not base:
        raise logic.DeskError("Не заполнен адрес публикации 1С")
    if not base.lower().endswith("odata/standard.odata"):
        base = base + "/odata/standard.odata"
    url = base + "/" + urllib.parse.quote(path, safe="?&=$")
    data = None
    headers = {"Accept": "application/json"}
    user = settings.get("onec_user") or ""
    password = settings.get("onec_password") or ""
    if user:
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise logic.DeskError(f"1С ответила {exc.code}: {detail or exc.reason}")
    except urllib.error.URLError as exc:
        raise logic.DeskError(f"Нет связи с публикацией 1С: {exc.reason}")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise logic.DeskError("1С ответила не списком заказов. Проверьте, что в адресе открыта публикация базы.")


def com_call(settings, command):
    if not BRIDGE.is_file():
        raise logic.DeskError("Не найден мост к 1С")
    payload = {
        "file": (settings.get("onec_file") or "").strip(),
        "server": (settings.get("onec_server") or "").strip(),
        "ref": (settings.get("onec_ref") or "").strip(),
        "user": settings.get("onec_user") or "",
        "password": settings.get("onec_password") or "",
    }
    payload.update(command)
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(BRIDGE)],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            timeout=120,
            check=False,
        )
    except FileNotFoundError:
        raise logic.DeskError("Связь с файловой или серверной базой 1С работает на Windows, где 1С уже установлена")
    except subprocess.TimeoutExpired:
        raise logic.DeskError("1С не ответила за две минуты. Проверьте, что база открывается с этого компьютера")
    text = proc.stdout.decode("utf-8", errors="replace")
    answer = None
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith("{"):
            try:
                answer = json.loads(stripped)
            except json.JSONDecodeError:
                answer = None
            break
    if not answer:
        detail = proc.stderr.decode("utf-8", errors="replace").strip() or text.strip()
        raise logic.DeskError(detail[:400] or "1С ничего не ответила")
    if not answer.get("ok"):
        raise logic.DeskError(answer.get("error") or "1С отказала")
    return answer
