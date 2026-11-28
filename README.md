# django-tasks

Django-App mit einer Kachel, die die offenen Tasks des lokal installierten
[Taskwarrior](https://taskwarrior.org) (Version 3) anzeigt – sortiert nach Urgency, mit Projekt, Tags,
Priorität und Fälligkeit. Standardmäßig nur, was überfällig, heute oder morgen fällig ist; „Alle (N)“
zeigt jeden offenen Task. Ein Snooze-Button verschiebt die Fälligkeit um 7 Tage, das Häkchen hakt den
Task ab, das + im Footer legt einen neuen an.

Ist auf dem Mac kein Taskwarrior zu finden, zeigt die Kachel einen Hinweis statt eines Fehlers.

Voraussetzungen im Projekt: Django ≥ 5.2, Python ≥ 3.12, [HTMX](https://htmx.org) 2, CSRF-Middleware.
Keine Datenbank, keine Migrationen, keine Inline-Skripte oder -Styles (läuft mit strikter CSP).

## Woher die Tasks kommen

Mit `TASKS_SOURCE = "auto"` (Standard) in dieser Reihenfolge:

1. **`task export`** – wenn das Programm `task` hier läuft, also Django direkt auf dem Mac.
   Ergebnis wie im Terminal, samt Urgency aus der eigenen `.taskrc`. Gesucht wird im `PATH` und
   zusätzlich in `/opt/homebrew/bin` und `/usr/local/bin`.
2. **Tasks-Brücke** – für Django im Linux-Container, in dem das Mac-Programm nicht läuft. Ein kleiner
   Dienst auf dem Mac ruft dort `task` auf (siehe unten). Ergebnis wie unter 1, Snooze und Abhaken inklusive.
3. **`taskchampion.sqlite3`** – Notbehelf im Container, wenn keine Brücke eingerichtet ist oder sie nicht
   antwortet. Nur lesend, also ohne Snooze, Häkchen und +; die Urgency rechnet das Paket mit den Standardgewichten von
   Taskwarrior nach (eigene `urgency.*`-Werte aus der `.taskrc` fehlen dann).

Wartende Tasks (`wait` in der Zukunft) blendet die Kachel aus, wie `task next`.

## Snooze

Der Button **+7** an jedem Task mit Fälligkeit verschiebt sie um `TASKS_SNOOZE_DAYS` Tage – gezählt ab dem
späteren von Fälligkeit und heute, die Uhrzeit bleibt:

- überfällig seit 30 Tagen → fällig in 7 Tagen
- heute fällig → in 7 Tagen
- morgen fällig → in 8 Tagen

Er ruft `task <uuid> modify due:…` auf – direkt, wenn `task` hier läuft, sonst über die Brücke. Mit der
Datenbank allein (nur lesend) fehlt der Button. Tasks ohne Fälligkeit haben keinen.

## Abhaken

Das Häkchen rechts an jedem Task ruft `task <uuid> done` auf (direkt oder über die Brücke) und meldet
„Erledigt: …“. Es fragt nicht nach; ein versehentliches Abhaken macht `task undo` rückgängig. Wiederkehrende
Tasks legt Taskwarrior dabei wie gewohnt neu an. Mit der Datenbank allein fehlt das Häkchen.

## Anlegen

Das **+** im Footer ersetzt die Liste durch ein Formular (wie bei den Routinen; solange es offen ist, lädt die Kachel
nicht neu): Beschreibung (Pflicht, eine Zeile, höchstens 300 Zeichen), Projekt (vorhandene als Vorschlag),
Priorität, Tags (mit Leerzeichen oder Komma getrennt, `+` davor erlaubt) und Fälligkeit (Datum; Taskwarrior setzt
Mitternacht in der Ortszeit des Macs). „Anlegen“ ruft

```sh
task add project:… +tag priority:H due:2026-12-03 -- Beschreibung
```

auf – direkt oder über die Brücke – und meldet „Angelegt: 14 Beschreibung“. Die Beschreibung steht hinter `--`, was darin
wie `project:x` oder `+tag` aussieht, bleibt also Text. Projekt und Tags erlauben nur Buchstaben, Ziffern, `-`, `_`
(Projekt auch `.`); bei falschen Angaben bleibt das Formular mit den Eingaben offen. Ohne Fälligkeit erscheint der
neue Task nur unter „Alle“. Mit der Datenbank allein fehlt das +.

## Einbauen

```toml
# pyproject.toml – Version festnageln; das Archiv braucht kein git im Docker-Image
dependencies = ["django-tasks @ https://github.com/torbenpe/django-tasks/archive/refs/tags/v0.5.0.tar.gz"]
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

### Im Container (OrbStack/Docker): Tasks-Brücke

Die Brücke läuft auf dem Mac (als LaunchAgent) und kann genau vier Dinge: offene Tasks liefern, eine
Fälligkeit verschieben, einen Task abhaken und einen anlegen. Sie hört nur auf 127.0.0.1 und verlangt einen Schlüssel, den sie beim ersten Start
selbst anlegt (Rechte 600). Django liest ihn aus derselben Datei – den Ordner dafür in den Container
einbinden.

```sh
python -m tasks.bridge --key-file /pfad/zum/projekt/data/tasks-bruecke.schluessel   # Port 8504
```

```yaml
# docker-compose.yml – ~/.task zusätzlich lesend, als Notbehelf ohne Brücke
volumes:
  - ./data:/app/data
  - ${HOME}/.task:/mac/taskwarrior:ro
environment:
  TASKS_BRIDGE_URL: http://host.docker.internal:8504
  TASKS_DATA_DIR: /mac/taskwarrior
```

```python
# settings.py
TASKS_BRIDGE_URL = os.environ.get("TASKS_BRIDGE_URL")
TASKS_BRIDGE_KEY_FILE = BASE_DIR / "data" / "tasks-bruecke.schluessel"
TASKS_DATA_DIR = os.environ.get("TASKS_DATA_DIR")
```

Für `~/.task` den Ordner einbinden, nicht nur die Datei: SQLite braucht die Nebendateien (`-wal`) daneben.
Antwortet die Brücke nicht, liest die Kachel die Datenbank und sagt „Tasks-Brücke nicht erreichbar – nur
lesend“.

| Einstellung | Standard | |
|---|---|---|
| `TASKS_SOURCE` | `"auto"` | `"cli"` nur `task`, `"bridge"` nur die Brücke, `"db"` nur die Datenbank |
| `TASKS_COMMAND` | `"task"` | Programmname oder absoluter Pfad |
| `TASKS_DATA_DIR` | `~/.task` | Ordner mit `taskchampion.sqlite3` |
| `TASKS_BRIDGE_URL` | – | Adresse der Brücke, etwa `http://host.docker.internal:8504` |
| `TASKS_BRIDGE_KEY_FILE` | – | Datei mit dem Schlüssel der Brücke |
| `TASKS_SNOOZE_DAYS` | `7` | so weit verschiebt der Snooze-Button |
| `TASKS_HOME_URL` | `"/"` | Ziel nach Snooze, Abhaken oder Anlegen ohne HTMX (Pfad oder URL-Name) |
| `TASKS_DAYS_AHEAD` | `1` | standardmäßig gezeigt: fällig bis heute + so viele Tage (0 = nur bis heute) |
| `TASKS_POLL_SECONDS` | `300` | Abstand, in dem sich die Kachel neu lädt |
| `TASKS_TIMEOUT` | `10` | Sekunden, die `task` oder die Brücke höchstens brauchen |

`task` läuft mit `rc.gc=off` (das Anzeigen nummeriert die IDs nicht um); `task export` zusätzlich mit
`rc.hooks=off`, `task modify`, `task done` und `task add` mit den eigenen Hooks.
Die Kachel hat keinen Zugriffsschutz; den muss das Projekt mitbringen.

## Kachel

- Statuspunkt (quadratisch, wie bei den Routinen): rot, wenn etwas überfällig ist; gelb, wenn etwas heute fällig ist; sonst grün. Grau ohne Daten.
- Kopf: „3 fällig · 13 offen“. Nach dem Snooze: „Verschoben auf Mi, 2. Dez.“, nach dem Abhaken: „Erledigt: …“,
  nach dem Anlegen: „Angelegt: …“. Ist nichts fällig: „Nichts fällig bis morgen.“ und darunter „Alle (N)“.
- Je Task: ID, Beschreibung (höchstens zwei Zeilen), Priorität, „läuft“ bei gestarteten Tasks,
  Projekt und Tags, Fälligkeit („überfällig seit 2 Tagen“, „heute fällig“, „fällig Mi, 2. Dez“);
  rechts Snooze und Häkchen.
- Footer: „Alle (N)“ bzw. „Nur fällige“ oder die Quelle links; rechts + (Neuer Task) und Neu laden.

## Entwickeln

```sh
uv run python -m django test tests --settings tests.settings --pythonpath .
```

Die Tests fragen nie das echte `task` des Rechners, sondern ein Ersatzskript und eine Testdatenbank;
die Brücke läuft dabei als echter HTTP-Dienst auf einem freien Port.

Neue Version: `version` in `pyproject.toml` erhöhen, committen, `git tag vX.Y.Z`, pushen.
In den Projekten den Tag in der URL ändern, `uv lock`, neu bauen.
