"""Offene Tasks des lokalen Taskwarrior (Version 3) als Kachel, mit Snooze, Abhaken und Anlegen.

Drei Wege an die Daten, in dieser Reihenfolge (`TASKS_SOURCE = "auto"`):

1. `task export` – wenn das Programm hier läuft (Django direkt auf dem Mac). Genau wie im Terminal,
   samt Urgency aus der eigenen `.taskrc`. Snooze, Abhaken und Anlegen rufen `task … modify`/`done`/`add` direkt auf.
2. Die Tasks-Brücke (`tasks.bridge`) – für Django im Linux-Container, in dem das Mac-Programm nicht
   läuft. Sie läuft auf dem Mac und ruft dort `task` auf; Ergebnis wie unter 1, Ändern inklusive.
   Gesetzt über `TASKS_BRIDGE_URL` und `TASKS_BRIDGE_KEY_FILE`.
3. `taskchampion.sqlite3` lesen – Notbehelf im Container, wenn die Brücke fehlt oder nicht antwortet.
   Nur lesend; die Urgency wird mit den Standardgewichten von Taskwarrior nachgerechnet.

Findet sich nichts davon, zeigt die Kachel einen Hinweis statt eines Fehlers.
"""

import json
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from . import taskwarrior

DB_NAME = "taskchampion.sqlite3"
DAYS_AHEAD = 1  # Standard: überfällig, heute und morgen fällig
SNOOZE_DAYS = 7
POLL_SECONDS = 300
TIMEOUT = 10
WEEKDAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
MONTHS = ("Jan", "Feb", "März", "Apr", "Mai", "Juni", "Juli", "Aug", "Sept", "Okt", "Nov", "Dez")
PRIORITY_LABELS = {"H": "hoch", "M": "mittel", "L": "niedrig"}
SOURCE_CLI, SOURCE_BRIDGE = "task export", "Tasks-Brücke"


class SourceError(Exception):
    """Taskwarrior ist da, liefert aber nichts Brauchbares (Fehler, Zeitüberschreitung, kaputte Daten)."""


class NotInstalled(Exception):
    """Weder das Programm `task` noch die Brücke noch eine Datenbank gefunden."""


class ReadOnly(Exception):
    """Ändern geht nicht: weder `task` hier noch eine Brücke eingerichtet."""


def _setting(name, default):
    return getattr(settings, name, default)


# --- Quellen ---------------------------------------------------------------------------------------

def find_command():
    return taskwarrior.find_command(_setting("TASKS_COMMAND", "task"))


def data_dir():
    configured = _setting("TASKS_DATA_DIR", None)
    return Path(configured).expanduser() if configured else Path.home() / ".task"


def export_cli(command):
    """Alle Tasks mit Status pending über `task export`."""
    try:
        raw = taskwarrior.export(command, timeout=_setting("TASKS_TIMEOUT", TIMEOUT))
    except taskwarrior.TaskError as exc:
        raise SourceError(str(exc)) from None
    return [_from_cli(entry) for entry in raw]


# --- Brücke ----------------------------------------------------------------------------------------

def bridge_config():
    """(URL, Schlüssel) oder None, wenn keine Brücke eingerichtet ist. Fehlt nur der Schlüssel: Fehler."""
    url = _setting("TASKS_BRIDGE_URL", None)
    if not url:
        return None
    key_file = _setting("TASKS_BRIDGE_KEY_FILE", None)
    try:
        key = Path(key_file).read_text(encoding="utf-8").strip() if key_file else ""
    except OSError:
        key = ""
    if not key:
        raise SourceError("Schlüssel der Tasks-Brücke fehlt (TASKS_BRIDGE_KEY_FILE) – läuft die Brücke schon?")
    return url.rstrip("/"), key


def _bridge(config, method, path, payload=None):
    url, key = config
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(f"{url}{path}", data=data, method=method, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=_setting("TASKS_TIMEOUT", TIMEOUT)) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read()).get("error")
        except (ValueError, AttributeError):
            message = None
        raise SourceError(f"Tasks-Brücke: {message or f'HTTP {exc.code}'}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ConnectionError("Tasks-Brücke nicht erreichbar") from None
    except ValueError:
        raise SourceError("Tasks-Brücke liefert kein JSON") from None


def export_bridge(config):
    tasks = _bridge(config, "GET", "/tasks").get("tasks")
    if not isinstance(tasks, list):
        raise SourceError("Tasks-Brücke liefert keine Liste")
    return [_from_cli(entry) for entry in tasks if isinstance(entry, dict)]


def _from_cli(entry):
    return {
        "id": entry.get("id") or None,
        "uuid": entry.get("uuid", ""),
        "description": entry.get("description", ""),
        "project": entry.get("project", ""),
        "tags": sorted(entry.get("tags") or []),
        "priority": entry.get("priority", ""),
        "due": taskwarrior.parse_date(entry.get("due")),
        "wait": taskwarrior.parse_date(entry.get("wait")),
        "entry": taskwarrior.parse_date(entry.get("entry")),
        "active": bool(entry.get("start")),
        "annotations": len(entry.get("annotations") or []),
        "depends": list(entry.get("depends") or []),
        "urgency": float(entry.get("urgency") or 0),
    }


def _connect(path):
    """Nur lesend. Ist der Ordner schreibgeschützt eingebunden, kann SQLite die WAL-Nebendateien nicht
    anlegen – dann als unveränderlich öffnen (Taskwarrior schließt nach jedem Befehl sauber ab)."""
    for query in ("mode=ro", "mode=ro&immutable=1"):
        conn = None
        try:
            conn = sqlite3.connect(f"{path.as_uri()}?{query}", uri=True, timeout=2)
            conn.execute("SELECT 1 FROM tasks LIMIT 1")
            return conn
        except sqlite3.DatabaseError as exc:
            error = exc
            if conn is not None:
                conn.close()
    raise SourceError(f"{DB_NAME} nicht lesbar ({error})")


def export_db(path, now):
    """Alle Tasks mit Status pending direkt aus der TaskChampion-Datenbank."""
    conn = _connect(path)
    try:
        rows = conn.execute("SELECT uuid, data FROM tasks").fetchall()
        ids = dict(conn.execute("SELECT uuid, id FROM working_set WHERE uuid IS NOT NULL").fetchall())
    except sqlite3.DatabaseError as exc:
        raise SourceError(f"{DB_NAME} nicht lesbar ({exc})") from None
    finally:
        conn.close()

    pending = {}
    for uuid, data in rows:
        try:
            fields = json.loads(data)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(fields, dict) and fields.get("status") == "pending":
            pending[uuid] = fields

    tasks = [_from_db(uuid, fields, ids.get(uuid)) for uuid, fields in pending.items()]
    blocking = {dep for task in tasks for dep in task["depends"] if dep in pending}
    for task in tasks:
        task["urgency"] = urgency(task, now, blocked=any(d in pending for d in task["depends"]),
                                  blocking=task["uuid"] in blocking)
    return tasks


def _parse_epoch(value):
    try:
        return datetime.fromtimestamp(int(value), tz=dt_timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _from_db(uuid, fields, task_id):
    return {
        "id": task_id,
        "uuid": uuid,
        "description": fields.get("description", ""),
        "project": fields.get("project", ""),
        "tags": sorted(key[4:] for key in fields if key.startswith("tag_")),
        "priority": fields.get("priority", ""),
        "due": _parse_epoch(fields.get("due")),
        "wait": _parse_epoch(fields.get("wait")),
        "entry": _parse_epoch(fields.get("entry")),
        "active": bool(fields.get("start")),
        "annotations": sum(1 for key in fields if key.startswith("annotation_")),
        "depends": [key[4:] for key in fields if key.startswith("dep_")],
        "urgency": 0.0,
    }


def _count_factor(n):
    return 0.0 if n == 0 else 0.8 if n == 1 else 0.9 if n == 2 else 1.0


def urgency(task, now, blocked=False, blocking=False):
    """Urgency mit den Standardgewichten von Taskwarrior 3 (nur für den Weg über die Datenbank)."""
    value = 0.0
    if task["due"]:
        overdue_days = (now - task["due"]).total_seconds() / 86400
        if overdue_days >= 7:
            scale = 1.0
        elif overdue_days >= -14:
            scale = (overdue_days + 14) * 0.8 / 21 + 0.2
        else:
            scale = 0.2
        value += 12.0 * scale
    value += {"H": 6.0, "M": 3.9, "L": 1.8}.get(task["priority"], 0.0)
    if "next" in task["tags"]:
        value += 15.0
    if task["active"]:
        value += 4.0
    if task["project"]:
        value += 1.0
    value += _count_factor(len(task["tags"])) + _count_factor(task["annotations"])
    if task["entry"]:
        value += 2.0 * min(max((now - task["entry"]).days, 0) / 365, 1.0)
    if blocking:
        value += 8.0
    if blocked:
        value -= 5.0
    return round(value, 4)


def load(now=None):
    """(Tasks, Quelle, Hinweis). Wirft NotInstalled oder SourceError."""
    now = now or timezone.now()
    source = _setting("TASKS_SOURCE", "auto")
    if source not in ("auto", "cli", "bridge", "db"):
        raise SourceError(f"TASKS_SOURCE muss auto, cli, bridge oder db sein, nicht „{source}“")
    if source in ("auto", "cli"):
        command = find_command()
        if command:
            return export_cli(command), SOURCE_CLI, ""
        if source == "cli":
            raise NotInstalled
    note = ""
    if source in ("auto", "bridge"):
        config = bridge_config()
        if config:
            try:
                return export_bridge(config), SOURCE_BRIDGE, ""
            except ConnectionError as exc:
                if source == "bridge":
                    raise SourceError(str(exc)) from None
                note = f"{exc} – nur lesend"
        elif source == "bridge":
            raise SourceError("TASKS_BRIDGE_URL ist nicht gesetzt")
    db = data_dir() / DB_NAME
    if source in ("auto", "db") and db.is_file():
        return export_db(db, now), DB_NAME, note
    if note:
        raise SourceError(note.removesuffix(" – nur lesend"))
    raise NotInstalled


def _change(local, path, payload, remote):
    """Ändert einen Task: über `task` hier (`local(command, timeout)`), sonst über die Brücke
    (`remote(antwort)`). Wirft ReadOnly oder SourceError."""
    source = _setting("TASKS_SOURCE", "auto")
    timeout = _setting("TASKS_TIMEOUT", TIMEOUT)
    if source in ("auto", "cli"):
        command = find_command()
        if command:
            try:
                return local(command, timeout)
            except taskwarrior.TaskError as exc:
                raise SourceError(str(exc)) from None
    if source in ("auto", "bridge"):
        config = bridge_config()
        if config:
            try:
                return remote(_bridge(config, "POST", path, payload))
            except ConnectionError as exc:
                raise SourceError(str(exc)) from None
    raise ReadOnly


def snooze_task(uuid, days=None):
    """Fälligkeit um `days` (Standard TASKS_SNOOZE_DAYS) verschieben – ab dem späteren von Fälligkeit
    und heute. Gibt die neue Fälligkeit zurück."""
    days = days or _setting("TASKS_SNOOZE_DAYS", SNOOZE_DAYS)
    return _change(
        lambda command, timeout: taskwarrior.snooze(command, uuid, days, timezone.localtime(), timeout=timeout),
        f"/tasks/{uuid}/snooze", {"days": days},
        lambda answer: taskwarrior.parse_date(answer.get("due")),
    )


def complete_task(uuid):
    """Task abhaken (`task <uuid> done`). Gibt seine Beschreibung zurück."""
    return _change(
        lambda command, timeout: taskwarrior.done(command, uuid, timeout=timeout),
        f"/tasks/{uuid}/done", {},
        lambda answer: str(answer.get("description") or ""),
    )


def parse_new(data):
    """Formular → (Felder für `taskwarrior.add`, Fehler je Feld). Tags durch Leerzeichen oder Komma
    getrennt, ein führendes + ist erlaubt."""
    description = " ".join(data.get("description", "").split())  # Zeilenumbrüche und Mehrfach-Leerzeichen weg
    tags = []
    for tag in data.get("tags", "").replace(",", " ").split():
        tag = tag.lstrip("+")
        if tag and tag not in tags:
            tags.append(tag)
    fields = {
        "description": description,
        "project": data.get("project", "").strip(),
        "tags": tags,
        "priority": data.get("priority", "").strip().upper(),
        "due": data.get("due", "").strip(),
    }
    return fields, taskwarrior.new_task_errors(fields)


def add_task(fields):
    """Task anlegen. Gibt (ID oder None, Beschreibung) zurück."""
    def from_bridge(answer):
        task_id = answer.get("id")
        return (task_id if isinstance(task_id, int) else None), str(answer.get("description") or "")

    return _change(
        lambda command, timeout: taskwarrior.add(command, fields, timeout=timeout),
        "/tasks", fields, from_bridge,
    )


def can_write(source):
    return source in (SOURCE_CLI, SOURCE_BRIDGE)


# --- Kachel ----------------------------------------------------------------------------------------

def date_label(day):
    return f"{WEEKDAYS[day.weekday()]}, {day.day}. {MONTHS[day.month - 1]}"


def _days(n):
    return "1 Tag" if n == 1 else f"{n} Tagen"


def build_item(task, today, days_ahead=DAYS_AHEAD):
    kind, label, soon = "ok", "", False
    if task["due"]:
        due_day = timezone.localtime(task["due"]).date()
        delta = (due_day - today).days
        soon = delta <= days_ahead
        if delta < 0:
            kind, label = "over", f"überfällig seit {_days(-delta)}"
        elif delta == 0:
            kind, label = "due", "heute fällig"
        elif delta == 1:
            label = "morgen fällig"
        else:
            label = f"fällig {date_label(due_day)}"
    meta = []
    if task["project"]:
        meta.append(task["project"])
    meta.extend(f"+{tag}" for tag in task["tags"])
    return {
        "id": task["id"],
        "uuid": task["uuid"],
        "name": task["description"],
        "kind": kind,
        "label": label,
        "soon": soon,
        "has_due": task["due"] is not None,
        "meta": " · ".join(meta),
        "priority": task["priority"],
        "priority_label": PRIORITY_LABELS.get(task["priority"], ""),
        "active": task["active"],
        "urgency": task["urgency"],
    }


STATUS_LABELS = {"red": "überfällig", "amber": "heute fällig", "green": "nichts fällig", "unknown": "keine Daten"}


NEW_FIELDS = ("description", "project", "tags", "priority", "due")


def _edit_context(tasks, form, errors, message):
    """Formular „Neuer Task“. `form`: zuletzt eingegebene Werte (nach einem Fehler), sonst leer."""
    return {
        "values": form or dict.fromkeys(NEW_FIELDS, ""),
        "errors": errors or {},
        "message": message,
        "projects": sorted({t["project"] for t in tasks if t["project"]}, key=str.casefold),
        "priorities": [("", "keine"), *PRIORITY_LABELS.items()],
    }


def get_tile(show_all=False, now=None, flash="", edit=False, form=None, errors=None, message=""):
    """Daten der Kachel. `edit=True` zeigt statt der Liste das Formular „Neuer Task“ (nur wenn Ändern geht);
    `form`, `errors` und `message` füllen es nach einem fehlgeschlagenen Versuch wieder."""
    now = now or timezone.now()
    today = timezone.localtime(now).date()
    tile = {
        "status": "unknown", "status_label": STATUS_LABELS["unknown"], "summary": "", "error": None,
        "missing": False, "source": "", "items": [], "count_all": 0, "count_soon": 0, "show_all": show_all,
        "days_ahead": _setting("TASKS_DAYS_AHEAD", DAYS_AHEAD), "can_write": False, "note": "", "flash": flash,
        "edit": None,
        "snooze_days": _setting("TASKS_SNOOZE_DAYS", SNOOZE_DAYS), "poll_seconds": _setting("TASKS_POLL_SECONDS", POLL_SECONDS),
    }
    try:
        tasks, source, note = load(now)
    except NotInstalled:
        tile.update(missing=True, status_label="Taskwarrior fehlt", summary="nicht gefunden")
        return tile
    except SourceError as exc:
        tile.update(error=str(exc), source=_setting("TASKS_SOURCE", "auto"))
        return tile

    visible = [t for t in tasks if not (t["wait"] and t["wait"] > now)]  # wartende Tasks blendet task auch aus
    visible.sort(key=lambda t: (-t["urgency"], t["due"] or datetime.max.replace(tzinfo=dt_timezone.utc), t["description"]))
    items = [build_item(task, today, tile["days_ahead"]) for task in visible]
    soon = [i for i in items if i["soon"]]
    status = ("red" if any(i["kind"] == "over" for i in items)
              else "amber" if any(i["kind"] == "due" for i in items) else "green")
    if not items:
        summary = "nichts offen"
    elif soon:
        summary = f"{len(soon)} fällig · {len(items)} offen"
    else:
        summary = f"{len(items)} offen"
    tile.update(
        status=status,
        status_label=STATUS_LABELS[status],
        summary=summary,
        source=source,
        note=note,
        can_write=can_write(source),
        items=items if show_all else soon,  # standardmäßig nur, was bis morgen fällig ist
        count_all=len(items),
        count_soon=len(soon),
    )
    if edit and tile["can_write"]:
        tile["edit"] = _edit_context(tasks, form, errors, message)
    return tile
