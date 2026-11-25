# django-tasks

Django-App mit einer Kachel, die die offenen Tasks des lokal installierten
[Taskwarrior](https://taskwarrior.org) (Version 3) anzeigt – sortiert nach Urgency, mit Projekt, Tags,
Priorität und Fälligkeit. Vorerst nur lesend; Abhaken aus der Kachel heraus kommt später.

Ist auf dem Mac kein Taskwarrior zu finden, zeigt die Kachel einen Hinweis statt eines Fehlers.

Voraussetzungen im Projekt: Django ≥ 5.2, Python ≥ 3.12, [HTMX](https://htmx.org) 2.
Keine Datenbank, keine Migrationen, keine Inline-Skripte oder -Styles (läuft mit strikter CSP).

## Woher die Tasks kommen

Mit `TASKS_SOURCE = "auto"` (Standard) in dieser Reihenfolge:

1. **`task export`** – wenn das Programm `task` hier läuft, also Django direkt auf dem Mac.
   Ergebnis wie im Terminal, samt Urgency aus der eigenen `.taskrc`. Gesucht wird im `PATH` und
   zusätzlich in `/opt/homebrew/bin` und `/usr/local/bin`.
2. **`taskchampion.sqlite3`** – für Django im Linux-Container, in dem das Mac-Programm nicht läuft.
   Die Datei wird nur gelesen. Die Urgency rechnet das Paket mit den Standardgewichten von Taskwarrior
   nach; eigene `urgency.*`-Werte aus der `.taskrc` fehlen dann.

Wartende Tasks (`wait` in der Zukunft) blendet die Kachel aus, wie `task next`.

## Einbauen

```toml
# pyproject.toml – Version festnageln; das Archiv braucht kein git im Docker-Image
dependencies = ["django-tasks @ https://github.com/torbenpe/django-tasks/archive/refs/tags/v0.1.0.tar.gz"]
```

```python
# settings.py
INSTALLED_APPS = [..., "django.contrib.staticfiles", "tasks"]

# urls.py
path("tiles/tasks/", include("tasks.urls"))
```

```django
{% load static tasks %}
<script src="{% static 'tasks/collapse.js' %}"></script>
<link rel="stylesheet" href="{% static 'tasks/tasks.css' %}">
…
{% tasks_tile %}
```

`collapse.js` blockierend im `<head>` laden, damit die eingeklappte Kachel nicht aufblitzt. Hat das Projekt schon
ein Skript für `data-collapse` (etwa aus django-routinen), dieses weglassen (sonst schaltet jeder Klick zweimal).

### Im Container (OrbStack/Docker)

`~/.task` des Macs als **Ordner** schreibgeschützt einbinden und den Pfad setzen:

```yaml
# docker-compose.yml
volumes:
  - ${HOME}/.task:/mac/taskwarrior:ro
environment:
  TASKS_DATA_DIR: /mac/taskwarrior
```

```python
# settings.py
TASKS_DATA_DIR = os.environ.get("TASKS_DATA_DIR")
```

Den Ordner einbinden, nicht nur die Datei: SQLite braucht die Nebendateien (`-wal`) daneben.

| Einstellung | Standard | |
|---|---|---|
| `TASKS_SOURCE` | `"auto"` | `"cli"` nur `task export`, `"db"` nur die Datenbank |
| `TASKS_COMMAND` | `"task"` | Programmname oder absoluter Pfad |
| `TASKS_DATA_DIR` | `~/.task` | Ordner mit `taskchampion.sqlite3` |
| `TASKS_LIMIT` | `8` | so viele Tasks, der Rest hinter „Alle (N)“ |
| `TASKS_POLL_SECONDS` | `300` | Abstand, in dem sich die Kachel neu lädt |
| `TASKS_TIMEOUT` | `10` | Sekunden, die `task export` höchstens braucht |

`task export` läuft mit `rc.gc=off` (das Anzeigen nummeriert die IDs nicht um) und `rc.hooks=off`.
Die Kachel hat keinen Zugriffsschutz; den muss das Projekt mitbringen.

## Kachel

- Statuspunkt: rot, wenn etwas überfällig ist; gelb, wenn etwas heute fällig ist; sonst grün. Grau ohne Daten.
- Je Task: ID, Beschreibung (höchstens zwei Zeilen), Priorität, „läuft“ bei gestarteten Tasks,
  Projekt und Tags, Fälligkeit („überfällig seit 2 Tagen“, „heute fällig“, „fällig Mi, 2. Dez“).

## Entwickeln

```sh
uv run python -m django test tests --settings tests.settings --pythonpath .
```

Die Tests fragen nie das echte `task` des Rechners, sondern ein Ersatzskript und eine Testdatenbank.

Neue Version: `version` in `pyproject.toml` erhöhen, committen, `git tag vX.Y.Z`, pushen.
In den Projekten den Tag in der URL ändern, `uv lock`, neu bauen.
