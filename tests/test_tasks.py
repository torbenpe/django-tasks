import json
import threading
import urllib.error
import urllib.request
from unittest import mock
from zoneinfo import ZoneInfo
import sqlite3
import stat
import tempfile
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from django.template import Context, Template
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from tasks import bridge, core, taskwarrior

# Mittwoch, 25. Nov. 2026, 12:00 in Berlin
NOW = datetime(2026, 11, 25, 11, 0, tzinfo=dt_timezone.utc)


def U(c):
    """Gültige Task-UUID aus einem Hex-Zeichen, z. B. U("b")."""
    return f"{c * 8}-{c * 4}-{c * 4}-{c * 4}-{c * 12}"


def epoch(text):
    return str(int(datetime.fromisoformat(text).replace(tzinfo=dt_timezone.utc).timestamp()))


CLI_EXPORT = [
    {"id": 1, "uuid": U("a"), "description": "Alt und ohne alles", "entry": "20260101T090000Z", "status": "pending", "urgency": 1.5},
    {"id": 2, "uuid": U("b"), "description": "Überfällig", "due": "20261120T230000Z", "entry": "20261101T090000Z",
     "status": "pending", "project": "projekt-a", "tags": ["kunde", "buero"], "priority": "H", "urgency": 19.2},
    {"id": 3, "uuid": U("c"), "description": "Heute", "due": "20261125T160000Z", "entry": "20261120T090000Z",
     "status": "pending", "start": "20261125T080000Z", "urgency": 14.0},
    {"id": 4, "uuid": U("d"), "description": "Wartet noch", "wait": "20261201T000000Z", "entry": "20261120T090000Z",
     "status": "pending", "urgency": 9.0},
    {"id": 6, "uuid": U("f"), "description": "Morgen", "due": "20261126T100000Z", "entry": "20261120T090000Z",
     "status": "pending", "urgency": 8.5},
    {"id": 5, "uuid": U("e"), "description": "Nächste Woche", "due": "20261201T230000Z", "entry": "20261120T090000Z",
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
            # task add: Argumente je Zeile festhalten (Beschreibung mit Leerzeichen bleibt ein Argument)
            'case "$*" in *" add "*) '
            f'printf "%s\\n" "$@" > "{self.dir}/add.txt"; echo "Created task 7."; exit 0;; esac\n'
            'case "$*" in *" modify "*|*" done") '
            f'echo "$@" >> "{self.dir}/modify.txt"; exit 0;; esac\n'
            f'echo "$@" > "{self.dir}/args.txt"\n'
            f'cat "{self.dir}/out.json"\n'
            f'echo "{stderr}" >&2\n'
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

    def added(self):
        path = self.dir / "add.txt"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def modified(self):
        path = self.dir / "modify.txt"
        return path.read_text(encoding="utf-8").split() if path.exists() else []

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


BERLIN = ZoneInfo("Europe/Berlin")
LOCAL_NOW = NOW.astimezone(BERLIN)  # Mi, 25. Nov., 12:00


class SnoozeDateTests(SimpleTestCase):
    def due(self, text):
        return datetime.fromisoformat(text).replace(tzinfo=BERLIN)

    def test_overdue_counts_from_today(self):
        new = taskwarrior.snoozed_due(self.due("2026-10-26T00:00"), 7, LOCAL_NOW)
        self.assertEqual(new.astimezone(BERLIN), self.due("2026-12-02T00:00"))

    def test_future_counts_from_due_and_keeps_time(self):
        new = taskwarrior.snoozed_due(self.due("2026-11-26T11:00"), 7, LOCAL_NOW)
        self.assertEqual(new.astimezone(BERLIN), self.due("2026-12-03T11:00"))

    def test_today_counts_from_today(self):
        new = taskwarrior.snoozed_due(self.due("2026-11-25T17:00"), 7, LOCAL_NOW)
        self.assertEqual(new.astimezone(BERLIN), self.due("2026-12-02T17:00"))

    def test_across_daylight_saving_keeps_local_midnight(self):
        now = datetime(2027, 3, 20, 12, 0, tzinfo=BERLIN)
        new = taskwarrior.snoozed_due(self.due("2027-03-25T00:00"), 7, now)
        self.assertEqual(new, datetime(2027, 3, 31, 22, 0, tzinfo=dt_timezone.utc))  # 1. April 00:00 Sommerzeit

    def test_rejects_bad_input(self):
        for uuid, days in (("nicht-uuid", 7), (U("b"), 0), (U("b"), 999), (U("b"), True)):
            with self.subTest(uuid=uuid, days=days), self.assertRaises(taskwarrior.TaskError):
                taskwarrior.snooze("/gibts/nicht", uuid, days, LOCAL_NOW)


class SnoozeCliTests(Sandbox):
    @mock.patch("django.utils.timezone.now", return_value=NOW)  # „heute“ steckt im Ergebnis
    def test_snooze_overdue_task(self, _now):
        self.fake_task(json.dumps(CLI_EXPORT))
        response = self.client.post(reverse("tasks:snooze", args=[U("b")]), HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        # überfällig: ab heute (25. Nov.) + 7 = Mi, 2. Dez., Uhrzeit 00:00 Berlin bleibt
        self.assertEqual(self.modified()[-3:], [U("b"), "modify", "due:20261201T230000Z"])
        self.assertContains(response, "Verschoben auf Mi, 2. Dez.")

    def test_button_only_for_tasks_with_due_date(self):
        self.fake_task(json.dumps(CLI_EXPORT))
        response = self.client.get(reverse("tasks:tile"), {"all": "1"})
        self.assertContains(response, 'class="tk-snooze"', count=4)  # b, c, f, e – nicht a
        self.assertContains(response, 'name="csrfmiddlewaretoken"')

    def test_task_without_due_date(self):
        self.fake_task(json.dumps(CLI_EXPORT))
        response = self.client.post(reverse("tasks:snooze", args=[U("a")]), HTTP_HX_REQUEST="true")
        self.assertContains(response, "Nicht verschoben – Task hat kein Fälligkeitsdatum")
        self.assertEqual(self.modified(), [])

    def test_without_htmx_redirects(self):
        self.fake_task(json.dumps(CLI_EXPORT))
        response = self.client.post(reverse("tasks:snooze", args=[U("b")]))
        self.assertRedirects(response, "/", fetch_redirect_response=False)

    @override_settings(TASKS_SNOOZE_DAYS=3)
    @mock.patch("django.utils.timezone.now", return_value=NOW)
    def test_snooze_days_setting(self, _now):
        self.fake_task(json.dumps(CLI_EXPORT))
        self.client.post(reverse("tasks:snooze", args=[U("f")]), HTTP_HX_REQUEST="true")
        self.assertEqual(self.modified()[-1], "due:20261129T100000Z")  # morgen 11:00 + 3

    def test_get_not_allowed(self):
        self.assertEqual(self.client.get(reverse("tasks:snooze", args=[U("b")])).status_code, 405)


class DoneCliTests(Sandbox):
    def test_done(self):
        self.fake_task(json.dumps(CLI_EXPORT))
        response = self.client.post(reverse("tasks:done", args=[U("b")]), HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.modified()[-2:], [U("b"), "done"])
        self.assertContains(response, "Erledigt: Überfällig")

    def test_button_for_every_task(self):
        self.fake_task(json.dumps(CLI_EXPORT))
        response = self.client.get(reverse("tasks:tile"), {"all": "1"})
        self.assertContains(response, 'class="tk-check"', count=5)  # auch a ohne Fälligkeit
        self.assertContains(response, "Alt und ohne alles erledigt")

    def test_unknown_task(self):
        self.fake_task(json.dumps(CLI_EXPORT))
        response = self.client.post(reverse("tasks:done", args=[U("9")]), HTTP_HX_REQUEST="true")
        self.assertContains(response, "Nicht abgehakt – Task nicht gefunden oder nicht mehr offen")
        self.assertEqual(self.modified(), [])

    def test_without_htmx_redirects(self):
        self.fake_task(json.dumps(CLI_EXPORT))
        response = self.client.post(reverse("tasks:done", args=[U("b")]))
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertEqual(self.modified()[-1], "done")

    def test_get_not_allowed(self):
        self.assertEqual(self.client.get(reverse("tasks:done", args=[U("b")])).status_code, 405)


class AddCliTests(Sandbox):
    def post(self, **data):
        return self.client.post(reverse("tasks:add"), data, HTTP_HX_REQUEST="true")

    def test_form_in_tile_without_polling(self):
        self.fake_task(json.dumps(CLI_EXPORT))
        self.assertContains(self.client.get(reverse("tasks:tile")), 'aria-label="Neuer Task"')
        response = self.client.get(reverse("tasks:tile"), {"edit": "neu"})
        self.assertContains(response, 'name="description"')
        self.assertContains(response, '<option value="projekt-a">')  # vorhandene Projekte als Vorschlag
        self.assertNotContains(response, "hx-trigger")
        self.assertNotContains(response, 'class="tk-list"')

    def test_add_with_all_fields(self):
        self.fake_task(json.dumps(CLI_EXPORT))
        response = self.post(description="  Angebot   schreiben ", project="projekt-a", tags="+kunde, buero kunde",
                             priority="h", due="2026-12-03")
        self.assertEqual(response.status_code, 200)
        args = self.added()
        self.assertEqual(args[args.index("add"):], ["add", "project:projekt-a", "+kunde", "+buero", "priority:H",
                                                   "due:2026-12-03", "--", "Angebot schreiben"])
        self.assertIn("rc.verbose=new-id", args)
        self.assertContains(response, "Angelegt: 7 Angebot schreiben")
        self.assertContains(response, "hx-trigger")  # zurück in der Liste

    def test_description_stays_text(self):
        self.fake_task(json.dumps(CLI_EXPORT))
        self.post(description="project:x +tag due:today rc.data.location=/tmp")
        self.assertEqual(self.added()[-2:], ["--", "project:x +tag due:today rc.data.location=/tmp"])

    def test_only_description(self):
        self.fake_task(json.dumps(CLI_EXPORT))
        self.post(description="Nur Text")
        args = self.added()
        self.assertEqual(args[args.index("add"):], ["add", "--", "Nur Text"])

    def test_errors_keep_form_and_input(self):
        self.fake_task(json.dumps(CLI_EXPORT))
        response = self.post(description=" ", project="zwei worte", tags="a:b", priority="X", due="2026-02-30")
        for text in ("Beschreibung <em class=\"tk-field-error\">– fehlt", "Projekt <em", "Tags <em",
                     "Priorität <em", "Fällig <em", 'value="zwei worte"'):
            self.assertContains(response, text)
        self.assertEqual(self.added(), [])

    def test_task_error_keeps_form(self):
        self.fake_task(json.dumps(CLI_EXPORT))
        with mock.patch("tasks.taskwarrior.add", side_effect=taskwarrior.TaskError("task add fehlgeschlagen: Nein.")):
            response = self.post(description="Geht nicht")
        self.assertContains(response, "Nicht angelegt – task add fehlgeschlagen: Nein.")
        self.assertContains(response, 'value="Geht nicht"')

    def test_without_htmx_redirects(self):
        self.fake_task(json.dumps(CLI_EXPORT))
        response = self.client.post(reverse("tasks:add"), {"description": "Ohne HTMX"})
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertEqual(self.added()[-1], "Ohne HTMX")

    def test_get_not_allowed(self):
        self.assertEqual(self.client.get(reverse("tasks:add")).status_code, 405)

    def test_validation_in_taskwarrior_module(self):
        ok = {"description": "x", "project": "", "tags": [], "priority": "", "due": ""}
        self.assertEqual(taskwarrior.new_task_errors(ok), {})
        for bad in ({"description": "a\nb"}, {"description": "x" * 301}, {"project": "a b"}, {"tags": ["a b"]},
                    {"tags": "kunde"}, {"tags": [f"t{i}" for i in range(11)]}, {"priority": "X"}, {"due": "morgen"},
                    {"due": "2026-13-01"}):
            with self.subTest(bad=bad):
                self.assertTrue(taskwarrior.new_task_errors({**ok, **bad}))


class ReadOnlyTests(Sandbox):
    def test_db_only_has_no_button_and_refuses(self):
        self.fake_db({U("b"): {"description": "Überfällig", "status": "pending", "due": epoch("2026-11-20T23:00:00")}})
        self.assertNotContains(self.client.get(reverse("tasks:tile")), "verschieben")
        response = self.client.post(reverse("tasks:snooze", args=[U("b")]), HTTP_HX_REQUEST="true")
        self.assertContains(response, "Verschieben geht hier nicht")
        self.assertNotContains(response, 'class="tk-check"')
        response = self.client.post(reverse("tasks:done", args=[U("b")]), HTTP_HX_REQUEST="true")
        self.assertContains(response, "Abhaken geht hier nicht")
        self.assertNotContains(response, "Neuer Task")
        self.assertNotContains(self.client.get(reverse("tasks:tile"), {"edit": "neu"}), 'name="description"')
        response = self.client.post(reverse("tasks:add"), {"description": "x"}, HTTP_HX_REQUEST="true")
        self.assertContains(response, "Anlegen geht hier nicht")


class BridgeTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.fake_task(json.dumps(CLI_EXPORT))
        self.key_file = self.dir / "bruecke.schluessel"
        key = bridge.create_key(self.key_file)
        server = bridge.make_server(key, str(self.dir / "task"), port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_address[1]}"
        # Django sieht kein task (wie im Container), nur die Brücke
        self.enterContext(override_settings(TASKS_COMMAND=str(self.dir / "kein-task"),
                                            TASKS_BRIDGE_URL=self.url, TASKS_BRIDGE_KEY_FILE=str(self.key_file)))

    def test_reads_through_bridge(self):
        tile = core.get_tile(now=NOW)
        self.assertEqual((tile["source"], tile["can_write"]), ("Tasks-Brücke", True))
        self.assertEqual([i["id"] for i in tile["items"]], [2, 3, 6])

    def test_snooze_through_bridge(self):
        response = self.client.post(reverse("tasks:snooze", args=[U("f")]), HTTP_HX_REQUEST="true")
        self.assertEqual(self.modified()[-3:-1], [U("f"), "modify"])
        self.assertContains(response, "Verschoben auf")

    def test_done_through_bridge(self):
        response = self.client.post(reverse("tasks:done", args=[U("f")]), HTTP_HX_REQUEST="true")
        self.assertEqual(self.modified()[-2:], [U("f"), "done"])
        self.assertContains(response, "Erledigt: Morgen")

    def test_add_through_bridge(self):
        response = self.client.post(reverse("tasks:add"), {"description": "Über die Brücke – äöü", "tags": "mac"},
                                    HTTP_HX_REQUEST="true")
        args = self.added()
        self.assertEqual(args[args.index("add"):], ["add", "+mac", "--", "Über die Brücke – äöü"])
        self.assertContains(response, "Angelegt: 7 Über die Brücke – äöü")

    def test_bridge_rejects_bad_new_task(self):
        key = self.key_file.read_text().strip()
        body = json.dumps({"description": "x", "project": "a b"}).encode()
        request = urllib.request.Request(self.url + "/tasks", method="POST", data=body,
                                         headers={"Authorization": f"Bearer {key}"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 502)
        self.assertEqual(self.added(), [])

    def test_key_file_is_private_and_reused(self):
        self.assertEqual(self.key_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(bridge.create_key(self.key_file), self.key_file.read_text().strip())

    def test_wrong_key_is_rejected(self):
        self.key_file.write_text("falsch\n")
        tile = core.get_tile(now=NOW)
        self.assertEqual(tile["error"], "Tasks-Brücke: Schlüssel fehlt oder falsch")
        self.assertEqual(self.modified(), [])

    def test_only_known_paths(self):
        key = self.key_file.read_text().strip()
        for method, path in (("POST", "/tasks/neu"), ("GET", "/tasks/add"), ("GET", f"/tasks/{U('b')}/snooze"), ("GET", f"/tasks/{U('b')}/done"),
                             ("POST", "/tasks/x/delete")):
            request = urllib.request.Request(self.url + path, method=method, data=b"{}" if method == "POST" else None,
                                             headers={"Authorization": f"Bearer {key}"})
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 404)

    def test_bridge_down_falls_back_to_db_read_only(self):
        self.fake_db({U("b"): {"description": "Aus der DB", "status": "pending", "due": epoch("2026-11-20T23:00:00")}})
        with override_settings(TASKS_BRIDGE_URL="http://127.0.0.1:9"):
            tile = core.get_tile(now=NOW)
        self.assertEqual((tile["source"], tile["can_write"]), (core.DB_NAME, False))
        self.assertEqual(tile["note"], "Tasks-Brücke nicht erreichbar – nur lesend")

    def test_bridge_down_without_db_is_an_error(self):
        with override_settings(TASKS_BRIDGE_URL="http://127.0.0.1:9"):
            self.assertEqual(core.get_tile(now=NOW)["error"], "Tasks-Brücke nicht erreichbar")
