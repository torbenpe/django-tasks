"""Aufrufe des Programms `task` (Taskwarrior 3) – ohne Django, damit auch die Brücke sie nutzen kann."""

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Beim Start aus launchd oder dem Finder fehlt Homebrew oft im PATH
EXTRA_PATH = ("/opt/homebrew/bin", "/usr/local/bin")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
BASE_ARGS = ("rc.confirmation=off", "rc.verbose=nothing", "rc.gc=off", "rc.json.array=on")
SNOOZE_DAYS_MAX = 365


class TaskError(Exception):
    """`task` lief nicht oder lieferte nichts Brauchbares. Die Meldung ist für die Kachel gedacht."""


def find_command(command="task"):
    if os.sep in command:
        return command if os.access(command, os.X_OK) else None
    search = os.pathsep.join([os.environ.get("PATH", ""), *EXTRA_PATH])
    return shutil.which(command, path=search)


def _run(command, args, timeout, what):
    try:
        result = subprocess.run([command, *BASE_ARGS, *args], capture_output=True, text=True,
                                timeout=timeout, stdin=subprocess.DEVNULL, check=False)
    except subprocess.TimeoutExpired:
        raise TaskError(f"{what} antwortet nicht") from None
    except OSError as exc:
        raise TaskError(f"task nicht startbar ({exc.strerror})") from None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise TaskError(f"{what} fehlgeschlagen: {detail[0] if detail else f'Exit {result.returncode}'}")
    return result.stdout


def export(command, filters=("status:pending",), timeout=10):
    """Rohes JSON von `task export` als Liste. Hooks aus: Lesen soll nichts auslösen."""
    out = _run(command, ["rc.hooks=off", *filters, "export"], timeout, "task export")
    try:
        data = json.loads(out or "[]")
    except json.JSONDecodeError:
        raise TaskError("task export liefert kein JSON") from None
    if not isinstance(data, list):
        raise TaskError("task export liefert keine Liste")
    return [entry for entry in data if isinstance(entry, dict)]


def parse_date(value):
    """Datum aus `task export` (20261127T230000Z) → datetime in UTC."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def snoozed_due(due, days, now):
    """Neue Fälligkeit: `days` Tage ab dem späteren von Fälligkeit und heute, Uhrzeit bleibt.

    `now` muss in der Ortszeit sein (bestimmt „heute“ und die Uhrzeit des Fälligkeitstages).
    """
    local_due = due.astimezone(now.tzinfo)
    base = max(local_due.date(), now.date())
    target = datetime.combine(base + timedelta(days=days), local_due.time())
    # Über die Sommerzeitgrenze hinweg die Ortszeit neu bestimmen, nicht den alten Versatz mitnehmen
    if isinstance(now.tzinfo, ZoneInfo):
        target = target.replace(tzinfo=now.tzinfo)
    else:  # fester Versatz, etwa aus datetime.now().astimezone(): Systemzeitzone des Macs
        target = target.astimezone()
    return target.astimezone(timezone.utc)


def _pending(command, uuid, timeout):
    """Der offene Task mit dieser UUID, sonst TaskError."""
    if not UUID_RE.fullmatch(uuid or ""):
        raise TaskError("ungültige Task-Kennung")
    task = next((t for t in export(command, (uuid,), timeout) if t.get("uuid") == uuid), None)
    if task is None or task.get("status") != "pending":
        raise TaskError("Task nicht gefunden oder nicht mehr offen")
    return task


def snooze(command, uuid, days, now, timeout=10):
    """Verschiebt die Fälligkeit eines offenen Tasks. Gibt die neue Fälligkeit (UTC) zurück."""
    if not UUID_RE.fullmatch(uuid or ""):
        raise TaskError("ungültige Task-Kennung")
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= SNOOZE_DAYS_MAX:
        raise TaskError(f"Snooze nur um 1–{SNOOZE_DAYS_MAX} Tage")
    task = _pending(command, uuid, timeout)
    due = parse_date(task.get("due"))
    if due is None:
        raise TaskError("Task hat kein Fälligkeitsdatum")
    new_due = snoozed_due(due, days, now)
    _run(command, [uuid, "modify", f"due:{new_due.strftime('%Y%m%dT%H%M%SZ')}"], timeout, "task modify")
    return new_due


def done(command, uuid, timeout=10):
    """Hakt einen offenen Task ab (`task <uuid> done`). Gibt seine Beschreibung zurück."""
    task = _pending(command, uuid, timeout)
    _run(command, [uuid, "done"], timeout, "task done")
    return task.get("description", "")


# --- Neuer Task ------------------------------------------------------------------------------------

DESCRIPTION_MAX = 300
PROJECT_MAX = 80
TAGS_MAX = 10
PRIORITIES = ("", "H", "M", "L")
# Bewusst eng: Leerzeichen, Doppelpunkte oder Klammern würde `task` als eigene Argumente oder Filter lesen
PROJECT_RE = re.compile(r"\w[\w.\-]*")
TAG_RE = re.compile(r"\w[\w\-]*")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CREATED_RE = re.compile(r"Created task (\d+)")


def new_task_errors(fields):
    """Fehler je Feld (leer = in Ordnung). `fields`: description, project, tags (Liste), priority, due
    (JJJJ-MM-TT oder leer). Prüft auch die Brücke, damit sie nur Taskwarrior-taugliche Werte weitergibt."""
    errors = {}
    description = fields.get("description")
    if not isinstance(description, str) or not description.strip():
        errors["description"] = "fehlt"
    elif len(description) > DESCRIPTION_MAX or "\n" in description or "\r" in description:
        errors["description"] = f"eine Zeile, höchstens {DESCRIPTION_MAX} Zeichen"
    project = fields.get("project") or ""
    if not isinstance(project, str) or (project and (len(project) > PROJECT_MAX or not PROJECT_RE.fullmatch(project))):
        errors["project"] = "ungültiges Zeichen"
    tags = fields.get("tags") or []
    if not isinstance(tags, list) or not all(isinstance(t, str) and TAG_RE.fullmatch(t) for t in tags):
        errors["tags"] = "ungültiges Zeichen"
    elif len(tags) > TAGS_MAX:
        errors["tags"] = f"höchstens {TAGS_MAX}"
    if (fields.get("priority") or "") not in PRIORITIES:
        errors["priority"] = "H, M, L oder keine"
    due = fields.get("due") or ""
    if not isinstance(due, str) or (due and not _valid_date(due)):
        errors["due"] = "kein Datum"
    return errors


def _valid_date(text):
    if not DATE_RE.fullmatch(text):
        return False
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def add(command, fields, timeout=10):
    """Legt einen Task an (`task add`). Gibt (ID oder None, Beschreibung) zurück.

    Die Beschreibung steht hinter `--`: Was darin wie `project:x`, `+tag` oder `due:…` aussieht, bleibt Text.
    Ein Datum ohne Uhrzeit setzt Taskwarrior auf Mitternacht in der Ortszeit des Macs.
    """
    errors = new_task_errors(fields)
    if errors:
        key, message = next(iter(errors.items()))
        raise TaskError(f"ungültige Angabe ({key}: {message})")
    description = fields["description"].strip()
    args = ["rc.verbose=new-id", "add"]
    if fields.get("project"):
        args.append(f"project:{fields['project']}")
    args.extend(f"+{tag}" for tag in fields.get("tags") or [])
    if fields.get("priority"):
        args.append(f"priority:{fields['priority']}")
    if fields.get("due"):
        args.append(f"due:{fields['due']}")
    out = _run(command, [*args, "--", description], timeout, "task add")
    match = _CREATED_RE.search(out)
    return (int(match.group(1)) if match else None), description
