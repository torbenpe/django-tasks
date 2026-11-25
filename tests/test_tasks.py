import json
import sqlite3
import stat
import tempfile
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from django.template import Context, Template
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from tasks import core

# Mittwoch, 25. Nov. 2026, 12:00 in Berlin
NOW = datetime(2026, 11, 25, 11, 0, tzinfo=dt_timezone.utc)


def epoch(text):
    return str(int(datetime.fromisoformat(text).replace(tzinfo=dt_timezone.utc).timestamp()))


CLI_EXPORT = [
    {"id": 1, "uuid": "a", "description": "Alt und ohne alles", "entry": "20260101T090000Z", "status": "pending", "urgency": 1.5},
    {"id": 2, "uuid": "b", "description": "Überfällig", "due": "20261120T230000Z", "entry": "20261101T090000Z",
     "status": "pending", "project": "projekt-a", "tags": ["kunde", "buero"], "priority": "H", "urgency": 19.2},
    {"id": 3, "uuid": "c", "description": "Heute", "due": "20261125T160000Z", "entry": "20261120T090000Z",
     "status": "pending", "start": "20261125T080000Z", "urgency": 14.0},
    {"id": 4, "uuid": "d", "description": "Wartet noch", "wait": "20261201T000000Z", "entry": "20261120T090000Z",
     "status": "pending", "urgency": 9.0},
    {"id": 6, "uuid": "f", "description": "Morgen", "due": "20261126T100000Z", "entry": "20261120T090000Z",
     "status": "pending", "urgency": 8.5},
    {"id": 5, "uuid": "e", "description": "Nächste Woche", "due": "20261201T230000Z", "entry": "20261120T090000Z",
     "status": "pending", "urgency": 8.0},
]


class Sandbox(SimpleTestCase):
    """Jeder Test bekommt einen leeren Ordner; das echte `task` des Rechners wird nie gefragt."""

    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(override_settings(TASKS_COMMAND=str(self.dir / "task"), TASKS_DATA_DIR=str(self.dir / "data")))

    def fake_task(self, stdout="[]", exit_code=0, stderr=""):
        script = self.dir / "task"
        (self.dir / "out.json").write_text(stdout, encoding="utf-8")
        (self.dir / "args.txt").unlink(missing_ok=True)
        script.write_text(
            "#!/bin/sh\n"
            f'echo "$@" > "{self.dir}/args.txt"\n'
            f'cat "{self.dir}/out.json"\n'
            f'echo "{stderr}" >&2\n'
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

    def fake_db(self, tasks, working_set=()):
        folder = self.dir / "data"
        folder.mkdir(exist_ok=True)
        conn = sqlite3.connect(folder / core.DB_NAME)
        conn.executescript(
            "CREATE TABLE tasks (uuid STRING PRIMARY KEY, data STRING);"
            "CREATE TABLE working_set (id INTEGER PRIMARY KEY, uuid STRING);"
            "PRAGMA journal_mode=wal;"
        )
        conn.executemany("INSERT INTO tasks VALUES (?, ?)", [(u, json.dumps(d)) for u, d in tasks.items()])
        conn.executemany("INSERT INTO working_set VALUES (?, ?)", list(working_set))
        conn.commit()
        conn.close()


class CliTests(Sandbox):
    def test_order_states_and_labels(self):
        self.fake_task(json.dumps(CLI_EXPORT))
        tile = core.get_tile(show_all=True, now=NOW)
        self.assertEqual(tile["source"], "task export")
        self.assertEqual(tile["status"], "red")
        self.assertEqual(tile["summary"], "3 fällig · 5 offen")
        self.assertEqual([(i["id"], i["kind"], i["label"], i["soon"]) for i in tile["items"]], [
            (2, "over", "überfällig seit 4 Tagen", True),
            (3, "due", "heute fällig", True),
            (6, "ok", "morgen fällig", True),
            (5, "ok", "fällig Mi, 2. Dez", False),
            (1, "ok", "", False),
        ])
        over = tile["items"][0]
        self.assertEqual((over["meta"], over["priority_label"]), ("projekt-a · +buero · +kunde", "hoch"))
        self.assertTrue(tile["items"][1]["active"])

    def test_export_arguments(self):
        self.fake_task()
        core.get_tile(now=NOW)
        args = (self.dir / "args.txt").read_text(encoding="utf-8").split()
        self.assertEqual(args[-2:], ["status:pending", "export"])
        self.assertIn("rc.gc=off", args)  # sonst würde schon das Anzeigen die IDs neu nummerieren

    def test_nothing_open(self):
        self.fake_task()
        tile = core.get_tile(now=NOW)
        self.assertEqual((tile["status"], tile["summary"], tile["items"]), ("green", "nichts offen", []))

    def test_failure_shows_error(self):
        self.fake_task("", exit_code=2, stderr="Kaputt.")
        tile = core.get_tile(now=NOW)
        self.assertEqual(tile["error"], "task export fehlgeschlagen: Kaputt.")
        self.assertEqual(tile["status"], "unknown")

    def test_garbage_output(self):
        self.fake_task("das ist kein json")
        self.assertEqual(core.get_tile(now=NOW)["error"], "task export liefert kein JSON")

    def test_default_shows_only_due_until_tomorrow(self):
        self.fake_task(json.dumps(CLI_EXPORT))
        tile = core.get_tile(now=NOW)
        self.assertEqual([i["id"] for i in tile["items"]], [2, 3, 6])
        self.assertEqual((tile["count_soon"], tile["count_all"]), (3, 5))

    @override_settings(TASKS_DAYS_AHEAD=0)
    def test_days_ahead_setting(self):
        self.fake_task(json.dumps(CLI_EXPORT))
        self.assertEqual([i["id"] for i in core.get_tile(now=NOW)["items"]], [2, 3])

    def test_nothing_due_but_open_tasks(self):
        self.fake_task(json.dumps([CLI_EXPORT[0]]))
        tile = core.get_tile(now=NOW)
        self.assertEqual((tile["items"], tile["summary"], tile["status"]), ([], "1 offen", "green"))
        response = self.client.get(reverse("tasks:tile"))
        self.assertContains(response, "Nichts fällig bis morgen.")
        self.assertContains(response, "Alle (1)")

    def test_cli_wins_over_db(self):
        self.fake_task(json.dumps(CLI_EXPORT))
        self.fake_db({"x": {"description": "aus der DB", "status": "pending"}})
        self.assertEqual(core.get_tile(now=NOW)["source"], "task export")


class DbTests(Sandbox):
    def sample(self):
        self.fake_db({
            "a": {"description": "Ohne alles", "status": "pending", "entry": epoch("2026-11-24T09:00:00")},
            "b": {"description": "Überfällig", "status": "pending", "due": epoch("2026-11-20T23:00:00"),
                  "entry": epoch("2026-11-01T09:00:00"), "project": "projekt-a", "priority": "H",
                  "tag_kunde": "x", "tags": "kunde", "annotation_1790000000": "Notiz"},
            "c": {"description": "Erledigt", "status": "completed", "end": epoch("2026-11-24T09:00:00")},
            "d": {"description": "Wartet", "status": "pending", "wait": epoch("2026-12-01T00:00:00")},
            "e": {"description": "Gelöscht", "status": "deleted"},
            "f": {"description": "Blockiert", "status": "pending", "dep_a": "x", "entry": epoch("2026-11-24T09:00:00")},
            "g": {"description": "kaputt"},
        }, working_set=[(1, "a"), (2, "b"), (3, "d"), (4, "f")])

    def test_reads_pending_and_skips_waiting(self):
        self.sample()
        tile = core.get_tile(show_all=True, now=NOW)
        self.assertEqual(tile["source"], core.DB_NAME)
        self.assertEqual([(i["id"], i["name"]) for i in tile["items"]], [(2, "Überfällig"), (1, "Ohne alles"), (4, "Blockiert")])
        self.assertEqual(tile["items"][0]["meta"], "projekt-a · +kunde")
        self.assertEqual(tile["status"], "red")

    def test_urgency_matches_taskwarrior_defaults(self):
        self.sample()
        tasks = {t["uuid"]: t for t in core.export_db(self.dir / "data" / core.DB_NAME, NOW)}
        # fällig vor ~4,5 Tagen: 12 × ((4.5 + 14) × 0.8 / 21 + 0.2) ≈ 10.86; H 6; Projekt 1; Tag 0.8; Anmerkung 0.8; Alter 24 Tage
        self.assertAlmostEqual(tasks["b"]["urgency"], 10.857 + 6 + 1 + 0.8 + 0.8 + 2 * 24 / 365, places=2)
        self.assertAlmostEqual(tasks["a"]["urgency"], 8.0 + 2 * 1 / 365, places=3)  # blockiert f
        self.assertAlmostEqual(tasks["f"]["urgency"], -5.0 + 2 * 1 / 365, places=3)

    def test_readonly_folder(self):
        self.sample()
        folder = self.dir / "data"
        folder.chmod(0o555)  # wie ein schreibgeschützt eingebundenes ~/.task: keine -wal/-shm möglich
        self.addCleanup(folder.chmod, 0o755)
        self.assertEqual(core.get_tile(now=NOW)["count_all"], 3)

    @override_settings(TASKS_SOURCE="db")
    def test_db_only_ignores_cli(self):
        self.fake_task(json.dumps(CLI_EXPORT))
        self.sample()
        self.assertEqual(core.get_tile(now=NOW)["source"], core.DB_NAME)

    def test_broken_db(self):
        (self.dir / "data").mkdir()
        (self.dir / "data" / core.DB_NAME).write_text("kein sqlite", encoding="utf-8")
        self.assertIn("nicht lesbar", core.get_tile(now=NOW)["error"])


class MissingTests(Sandbox):
    def test_hint_when_nothing_found(self):
        tile = core.get_tile(now=NOW)
        self.assertTrue(tile["missing"])
        self.assertEqual((tile["status"], tile["error"]), ("unknown", None))

    def test_tile_shows_hint(self):
        response = self.client.get(reverse("tasks:tile"))
        self.assertContains(response, "Kein Taskwarrior gefunden")
        self.assertContains(response, "brew install task")

    @override_settings(TASKS_SOURCE="cli")
    def test_cli_only_does_not_fall_back(self):
        self.fake_db({"a": {"description": "x", "status": "pending"}})
        self.assertTrue(core.get_tile(now=NOW)["missing"])

    @override_settings(TASKS_SOURCE="irgendwas")
    def test_bad_source_setting(self):
        self.assertIn("TASKS_SOURCE", core.get_tile(now=NOW)["error"])


class ViewTests(Sandbox):
    def test_tile_renders_items_and_polls(self):
        self.fake_task(json.dumps(CLI_EXPORT))
        response = self.client.get(reverse("tasks:tile"))
        self.assertContains(response, "Überfällig")
        self.assertContains(response, 'data-collapse="tasks"')
        self.assertContains(response, 'hx-trigger="every 300s"')
        self.assertNotContains(response, "Wartet noch")
        self.assertNotContains(response, "Nächste Woche")  # erst unter „Alle“
        self.assertContains(response, "Alle (5)")
        self.assertContains(self.client.get(reverse("tasks:tile"), {"all": "1"}), "Nächste Woche")

    def test_post_not_allowed(self):
        self.assertEqual(self.client.post(reverse("tasks:tile")).status_code, 405)

    def test_template_tag(self):
        self.fake_task(json.dumps(CLI_EXPORT))
        html = Template("{% load tasks %}{% tasks_tile %}").render(Context({}))
        self.assertIn("tk-tile", html)
        self.assertIn("Heute", html)
