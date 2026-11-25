"""Offene Tasks des lokalen Taskwarrior (Version 3) als Kachel – vorerst nur lesend.

Zwei Wege an die Daten, in dieser Reihenfolge (`TASKS_SOURCE = "auto"`):

1. `task export` – wenn das Programm hier läuft (Django direkt auf dem Mac). Genau wie im Terminal,
   samt Urgency aus der eigenen `.taskrc`.
2. `taskchampion.sqlite3` lesen – für Django im Linux-Container, in dem das Mac-Programm nicht läuft.
   Dafür `~/.task` schreibgeschützt einbinden und `TASKS_DATA_DIR` setzen. Die Urgency wird mit den
   Standardgewichten von Taskwarrior nachgerechnet (eigene `urgency.*`-Werte aus der `.taskrc` fehlen).

Findet sich keins von beiden, zeigt die Kachel einen Hinweis statt eines Fehlers.
"""

import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.utils import timezone

DB_NAME = "taskchampion.sqlite3"
DAYS_AHEAD = 1  # Standard: überfällig, heute und morgen fällig
POLL_SECONDS = 300
TIMEOUT = 10
# Beim Start aus launchd oder dem Finder fehlt Homebrew oft im PATH
EXTRA_PATH = ("/opt/homebrew/bin", "/usr/local/bin")
WEEKDAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
MONTHS = ("Jan", "Feb", "März", "Apr", "Mai", "Juni", "Juli", "Aug", "Sept", "Okt", "Nov", "Dez")
PRIORITY_LABELS = {"H": "hoch", "M": "mittel", "L": "niedrig"}


class SourceError(Exception):
    """Taskwarrior ist da, liefert aber nichts Brauchbares (Fehler, Zeitüberschreitung, kaputte Daten)."""


class NotInstalled(Exception):
    """Weder das Programm `task` noch eine Datenbank gefunden."""


def _setting(name, default):
    return getattr(settings, name, default)


# --- Quellen ---------------------------------------------------------------------------------------

def find_command():
    command = _setting("TASKS_COMMAND", "task")
    if os.sep in command:
        return command if os.access(command, os.X_OK) else None
    search = os.pathsep.join([os.environ.get("PATH", ""), *EXTRA_PATH])
    return shutil.which(command, path=search)


def data_dir():
    configured = _setting("TASKS_DATA_DIR", None)
    return Path(configured).expanduser() if configured else Path.home() / ".task"


def export_cli(command):
    """Alle Tasks mit Status pending über `task export`."""
    args = [
        command,
        "rc.confirmation=off", "rc.verbose=nothing", "rc.hooks=off", "rc.gc=off", "rc.json.array=on",
        "status:pending", "export",
    ]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=_setting("TASKS_TIMEOUT", TIMEOUT),
                                stdin=subprocess.DEVNULL, check=False)
    except subprocess.TimeoutExpired:
        raise SourceError("task export antwortet nicht") from None
    except OSError as exc:
        raise SourceError(f"task nicht startbar ({exc.strerror})") from None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise SourceError(f"task export fehlgeschlagen: {detail[0] if detail else f'Exit {result.returncode}'}")
    try:
        raw = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        raise SourceError("task export liefert kein JSON") from None
    return [_from_cli(entry) for entry in raw if isinstance(entry, dict)]


def _parse_cli_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt_timezone.utc)
    except ValueError:
        return None


def _from_cli(entry):
    return {
        "id": entry.get("id") or None,
        "uuid": entry.get("uuid", ""),
        "description": entry.get("description", ""),
        "project": entry.get("project", ""),
        "tags": sorted(entry.get("tags") or []),
        "priority": entry.get("priority", ""),
        "due": _parse_cli_date(entry.get("due")),
        "wait": _parse_cli_date(entry.get("wait")),
        "entry": _parse_cli_date(entry.get("entry")),
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
    """(Tasks, Quelle). Wirft NotInstalled oder SourceError."""
    now = now or timezone.now()
    source = _setting("TASKS_SOURCE", "auto")
    if source not in ("auto", "cli", "db"):
        raise SourceError(f"TASKS_SOURCE muss auto, cli oder db sein, nicht „{source}“")
    if source in ("auto", "cli"):
        command = find_command()
        if command:
            return export_cli(command), "task export"
        if source == "cli":
            raise NotInstalled
    db = data_dir() / DB_NAME
    if db.is_file():
        return export_db(db, now), DB_NAME
    raise NotInstalled


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
        "meta": " · ".join(meta),
        "priority": task["priority"],
        "priority_label": PRIORITY_LABELS.get(task["priority"], ""),
        "active": task["active"],
        "urgency": task["urgency"],
    }


STATUS_LABELS = {"red": "überfällig", "amber": "heute fällig", "green": "nichts fällig", "unknown": "keine Daten"}


def get_tile(show_all=False, now=None):
    now = now or timezone.now()
    today = timezone.localtime(now).date()
    tile = {
        "status": "unknown", "status_label": STATUS_LABELS["unknown"], "summary": "", "error": None,
        "missing": False, "source": "", "items": [], "count_all": 0, "count_soon": 0, "show_all": show_all,
        "days_ahead": _setting("TASKS_DAYS_AHEAD", DAYS_AHEAD), "poll_seconds": _setting("TASKS_POLL_SECONDS", POLL_SECONDS),
    }
    try:
        tasks, source = load(now)
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
        items=items if show_all else soon,  # standardmäßig nur, was bis morgen fällig ist
        count_all=len(items),
        count_soon=len(soon),
    )
    return tile
