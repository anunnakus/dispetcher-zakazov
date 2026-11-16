# -*- coding: utf-8 -*-
"""Локальный диспетчер заказов. Слушает только этот компьютер."""

import json
import mimetypes
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import logic

WEB = Path(__file__).resolve().parent / "web"
HOST = "127.0.0.1"
PORT = 8765


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _send(self, code, payload, content_type="application/json; charset=utf-8"):
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        elif isinstance(payload, str):
            body = payload.encode("utf-8")
        else:
            body = payload
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 2_000_000:
            raise logic.DeskError("Файл слишком большой")
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise logic.DeskError("Не понял данные формы")

    def _guard(self, action):
        try:
            action()
        except logic.DeskError as exc:
            self._send(400, {"error": exc.message})
        except Exception as exc:
            self._send(500, {"error": f"Сбой программы: {exc}"})

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/today":
            self._guard(lambda: self._send(200, logic.today_view()))
            return
        if path == "/api/orders":
            query = parse_qs(parsed.query)
            status = (query.get("status") or [""])[0]
            text = (query.get("q") or [""])[0]
            self._guard(lambda: self._send(200, {"orders": logic.list_orders(status, text)}))
            return
        if path.startswith("/api/orders/"):
            order_id = path.rsplit("/", 1)[-1]
            self._guard(lambda: self._send(200, logic.get_order(int(order_id))))
            return
        if path == "/api/templates":
            self._guard(lambda: self._send(200, {"templates": logic.list_templates()}))
            return
        if path == "/api/settings":
            self._guard(lambda: self._send(200, logic.public_settings()))
            return
        if path == "/api/journal":
            self._guard(lambda: self._send(200, {"journal": logic.journal_list()}))
            return
        if path == "/api/sample.csv":
            sample = (Path(__file__).resolve().parent / "пример-импорта.csv").read_text(encoding="utf-8")
            self._send(200, sample, "text/csv; charset=utf-8")
            return
        self._file(path)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        def finish():
            if path == "/api/orders":
                self._send(200, logic.create_order(self._json_body()))
                return
            if path == "/api/import":
                payload = self._json_body()
                self._send(200, logic.import_csv(payload.get("csv") or ""))
                return
            if path == "/api/demo":
                self._send(200, logic.load_demo())
                return
            if path == "/api/demo/clear":
                logic.clear_demo()
                self._send(200, {"ok": True})
                return
            if path == "/api/tick":
                self._send(200, logic.run_tick())
                return
            if path.startswith("/api/orders/") and path.endswith("/status"):
                order_id = int(path.split("/")[3])
                self._send(200, logic.change_status(order_id, self._json_body()))
                return
            if path.startswith("/api/orders/") and path.endswith("/update"):
                order_id = int(path.split("/")[3])
                self._send(200, logic.update_order(order_id, self._json_body()))
                return
            if path.startswith("/api/orders/") and path.endswith("/delete"):
                order_id = int(path.split("/")[3])
                logic.delete_order(order_id)
                self._send(200, {"ok": True})
                return
            if path.startswith("/api/tasks/") and path.endswith("/done"):
                task_id = int(path.split("/")[3])
                logic.complete_task(task_id)
                self._send(200, {"ok": True})
                return
            if path.startswith("/api/messages/") and path.endswith("/send"):
                message_id = int(path.split("/")[3])
                self._send(200, logic.send_message(message_id))
                return
            if path.startswith("/api/messages/") and path.endswith("/mark"):
                message_id = int(path.split("/")[3])
                self._send(200, logic.mark_message(message_id, (self._json_body().get("status") or "copied")))
                return
            if path.startswith("/api/templates/"):
                key = path.rsplit("/", 1)[-1]
                logic.save_template(key, self._json_body().get("body"))
                self._send(200, {"ok": True})
                return
            if path == "/api/settings":
                self._send(200, logic.save_settings(self._json_body()))
                return
            self._send(404, {"error": "Неизвестный адрес"})

        self._guard(finish)

    def _file(self, path):
        relative = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB / relative).resolve()
        if not str(target).startswith(str(WEB.resolve())) or not target.is_file():
            self._send(404, "Не найдено", "text/plain; charset=utf-8")
            return
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix in (".js", ".css", ".html"):
            mime = {"js": "text/javascript", "css": "text/css", "html": "text/html"}[target.suffix[1:]]
        self._send(200, target.read_bytes(), f"{mime}; charset=utf-8")


def _loop():
    while True:
        time.sleep(60)
        try:
            logic.run_tick()
        except Exception:
            continue


def _listen():
    last_error = None
    for port in range(PORT, PORT + 10):
        try:
            return ThreadingHTTPServer((HOST, port), Handler), port
        except OSError as exc:
            last_error = exc
    print("Не удалось открыть локальную страницу.")
    print(last_error)
    print("Закройте второе окно диспетчера, если оно уже запущено, и откройте Запуск ещё раз.")
    raise SystemExit(1)


def main():
    logic.init_db()
    try:
        logic.run_tick()
    except Exception:
        pass
    threading.Thread(target=_loop, daemon=True).start()
    server, port = _listen()
    url = f"http://{HOST}:{port}"
    threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    print(f"Диспетчер запущен: {url}")
    print("Это окно не закрывайте, пока работаете. Заказы лежат в папке data рядом с программой.")
    print("Если Windows спросит разрешение для брандмауэра, его можно не давать: страница открывается только на этом компьютере.")
    server.serve_forever()


if __name__ == "__main__":
    main()
