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


def snooze(command, uuid, days, now, timeout=10):
    """Verschiebt die Fälligkeit eines offenen Tasks. Gibt die neue Fälligkeit (UTC) zurück."""
    if not UUID_RE.fullmatch(uuid or ""):
        raise TaskError("ungültige Task-Kennung")
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= SNOOZE_DAYS_MAX:
        raise TaskError(f"Snooze nur um 1–{SNOOZE_DAYS_MAX} Tage")
    task = next((t for t in export(command, (uuid,), timeout) if t.get("uuid") == uuid), None)
    if task is None or task.get("status") != "pending":
        raise TaskError("Task nicht gefunden oder nicht mehr offen")
    due = parse_date(task.get("due"))
    if due is None:
        raise TaskError("Task hat kein Fälligkeitsdatum")
    new_due = snoozed_due(due, days, now)
    _run(command, [uuid, "modify", f"due:{new_due.strftime('%Y%m%dT%H%M%SZ')}"], timeout, "task modify")
    return new_due
