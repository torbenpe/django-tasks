"""Tasks-Brücke: ein kleiner HTTP-Dienst auf dem Mac, damit Django im Container Taskwarrior nutzen kann.

Im Linux-Container läuft das Mac-Programm `task` nicht, und `~/.task` ist dort nur lesend eingebunden.
Die Brücke läuft deshalb auf dem Mac, als der angemeldete Benutzer, und kann genau zwei Dinge:

    GET  /tasks                    offene Tasks wie `task status:pending export`
    POST /tasks/<uuid>/snooze      Fälligkeit verschieben, Body {"days": 7}

Sonst nichts – keine beliebigen Befehle, kein Löschen, kein Ändern anderer Felder.

Sie hört nur auf 127.0.0.1 und verlangt einen Schlüssel (Header `Authorization: Bearer …`). Ohne ihn
könnte jede Webseite im Browser des Macs per fetch() Tasks verschieben. Den Schlüssel legt sie beim
ersten Start selbst an (nur für den Benutzer lesbar); Django liest ihn aus derselben Datei – im
Container über einen eingebundenen Ordner. So muss niemand einen Schlüssel von Hand übertragen.

    python -m tasks.bridge --key-file /pfad/tasks-bruecke.schluessel [--port 8504]

Braucht kein Django, nur Python ≥ 3.12 und `task`.
"""

import argparse
import hmac
import json
import os
import re
import secrets
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import taskwarrior

DEFAULT_PORT = 8504
BODY_MAX = 1024
_SNOOZE_PATH = re.compile(r"/tasks/([0-9a-f-]{36})/snooze")


def read_key(path):
    try:
        key = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return key or None


def create_key(path):
    """Vorhandenen Schlüssel lesen, sonst einen neuen anlegen (Rechte 600)."""
    if key := read_key(path):
        return key
    key = secrets.token_urlsafe(32)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(key + "\n")
    return key


class Handler(BaseHTTPRequestHandler):
    server_version = "tasks-bridge"
    # Vom Server gesetzt
    key = ""
    task_command = "task"  # nicht `command`: das belegt BaseHTTPRequestHandler mit der HTTP-Methode
    lock = threading.Lock()  # nie zwei task-Aufrufe gleichzeitig

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method):
        given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not given or not hmac.compare_digest(given.encode(), self.key.encode()):
            return self._reply(401, {"error": "Schlüssel fehlt oder falsch"})
        path = self.path.split("?", 1)[0].rstrip("/")
        try:
            if method == "GET" and path == "/tasks":
                with self.lock:
                    return self._reply(200, {"tasks": taskwarrior.export(self.task_command)})
            match = _SNOOZE_PATH.fullmatch(path)
            if method == "POST" and match:
                days = self._body().get("days")
                with self.lock:
                    due = taskwarrior.snooze(self.task_command, match.group(1), days, datetime.now().astimezone())
                return self._reply(200, {"due": due.strftime("%Y%m%dT%H%M%SZ")})
        except taskwarrior.TaskError as exc:
            return self._reply(502, {"error": str(exc)})
        except ValueError as exc:
            return self._reply(400, {"error": str(exc)})
        return self._reply(404, {"error": "unbekannte Adresse"})

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > BODY_MAX:
            raise ValueError("Anfrage zu groß")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            raise ValueError("kein JSON") from None
        if not isinstance(data, dict):
            raise ValueError("JSON-Objekt erwartet")
        return data

    def _reply(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_server(key, command, host="127.0.0.1", port=DEFAULT_PORT):
    handler = type("BoundHandler", (Handler,), {"key": key, "task_command": command, "lock": threading.Lock()})
    return ThreadingHTTPServer((host, port), handler)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m tasks.bridge", description=__doc__.splitlines()[0])
    parser.add_argument("--key-file", required=True, help="Schlüsseldatei; wird angelegt, wenn sie fehlt")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1", help="Standard 127.0.0.1 – nicht ins Netz öffnen")
    parser.add_argument("--command", default="task", help="Programm task oder sein Pfad")
    args = parser.parse_args(argv)

    command = taskwarrior.find_command(args.command)
    if not command:
        print(f"Taskwarrior nicht gefunden ({args.command}) – brew install task", file=sys.stderr)
        return 1
    key = create_key(args.key_file)
    server = make_server(key, command, args.host, args.port)
    print(f"Tasks-Brücke auf {args.host}:{args.port} ({command})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
