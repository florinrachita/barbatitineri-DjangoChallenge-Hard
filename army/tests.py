import threading
import time
from datetime import timedelta
from io import StringIO
from unittest import mock

import requests
from django.core.management import call_command
from django.core.management.base import CommandError
from django.tasks import TaskResultStatus, task
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from .api import ChallengeAPIError, get_json
from .batches import CannotStart, mark_interrupted
from .fights import InvalidFightData, health_mismatches, save_fights
from .forms import BatchForm
from .models import DraftBatch, Fight, FightBatch, FightEvent, Person
from .task_backends import ThreadPoolBackend
from .tasks import start_download, start_simulation

# Run tasks synchronously in most tests so results are deterministic.
IMMEDIATE_TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}
THREAD_TASKS = {"default": {"BACKEND": "army.task_backends.ThreadPoolBackend", "OPTIONS": {"MAX_WORKERS": 16}}}


def fake_response(payload, status_code=200):
    response = mock.Mock(status_code=status_code, text=str(payload))
    response.json.return_value = payload
    return response


def person(pid, **extra):
    return {"id": pid, "name": f"Person {pid}", "age": 20 + pid, **extra}


def draft_payload(number, last):
    people = [person(number * 10 + i, rank="Private", health=100) for i in range(10)]
    return {"status": 200, "data": people, "next_batch": None if number == last else number + 1}


def fight(fight_id, blue, red, damages):
    """A fight where blue and red take turns attacking, blue first."""
    steps = []
    for i, damage in enumerate(damages):
        attacker, defender = (blue, red) if i % 2 == 0 else (red, blue)
        steps.append({"attacker": attacker, "defender": defender, "damage": damage})
    return {"fight_id": fight_id, "blue_soldier_id": blue, "red_soldier_id": red, "steps": steps}


def fight_payload(number, last):
    # Soldier 0 is in every fight, so parallel fights keep updating the same row.
    fights = [fight(number * 3 + i, 0, number * 3 + i + 1, [5, 7, 6]) for i in range(3)]
    return {"status": 200, "data": fights, "next_batch": None if number == last else number + 1}


def fake_server(batch_count):
    """Simulate /people, /draft/<n> and /fight/<n>, each with batch_count batches."""

    def get(url, timeout):
        path = url.rsplit("/", 2)
        if path[-1] == "people":
            people = [person(i) for i in range(batch_count * 10)]
            people[-1].update(key="draft-info", name="Hellstorm Fireburn")
            return fake_response({"status": 200, "data": people})
        if path[-2] in ("draft", "fight"):
            number = int(path[-1])
            if number >= batch_count:
                return fake_response({"status": 400, "data": {"message": "Invalid batch id"}})
            build = draft_payload if path[-2] == "draft" else fight_payload
            return fake_response(build(number, batch_count - 1))
        return fake_response({}, status_code=404)

    return get


def create_soldiers(count, health=100):
    return [
        Person.objects.create(
            external_id=i, name=f"Person {i}", age=30, raw={}, rank="Private", health=health,
            draft_raw={"id": i, "health": health},
        )
        for i in range(count)
    ]


class GetJsonTests(SimpleTestCase):
    def assert_retryable(self, response_or_error, expected):
        kwargs = {"side_effect": response_or_error} if isinstance(response_or_error, type) else {
            "return_value": response_or_error
        }
        with mock.patch("army.api.requests.get", **kwargs):
            with self.assertRaises(ChallengeAPIError) as ctx:
                get_json("draft/0")
        self.assertIs(ctx.exception.retryable, expected)

    def test_error_status_in_body_is_not_retryable(self):
        self.assert_retryable(fake_response({"status": 400, "data": "bad"}), False)

    def test_5xx_and_gateway_499_are_retryable(self):
        self.assert_retryable(fake_response("", status_code=502), True)
        self.assert_retryable(fake_response("", status_code=499), True)

    def test_timeout_is_retryable(self):
        self.assert_retryable(requests.Timeout, True)


@override_settings(TASKS=IMMEDIATE_TASKS)
class ImportAndDraftTests(TestCase):
    @mock.patch("army.api.requests.get", side_effect=fake_server(3))
    def test_import_is_idempotent_and_prints_key(self, _):
        out = StringIO()
        call_command("import_people", stdout=out)
        call_command("import_people", stdout=out)
        self.assertEqual(Person.objects.count(), 30)
        self.assertIn("key: draft-info", out.getvalue())

    @mock.patch("army.api.requests.get", side_effect=fake_server(3))
    def test_draft_ranks_everyone_one_task_per_batch(self, _):
        call_command("import_people", stdout=StringIO())
        start_download("draft")
        task_ids = set(DraftBatch.objects.values_list("task_id", flat=True))
        self.assertEqual(len(task_ids - {""}), 3)  # one separate task per batch
        self.assertEqual(Person.objects.filter(rank="Private", health=100).count(), 30)
        self.assertIsNone(Person.objects.get(external_id=29).raw.get("rank"))  # raw stays as imported
        self.assertEqual(list(DraftBatch.objects.values_list("status", flat=True)), ["done"] * 3)

    @mock.patch("army.api.requests.get", side_effect=fake_server(3))
    def test_follows_next_batch_when_estimate_is_too_low(self, _):
        start_download("draft")  # no people imported: estimate is 1 batch
        self.assertEqual(DraftBatch.objects.filter(status="done").count(), 3)
        self.assertEqual(Person.objects.count(), 30)

    @mock.patch("army.batches.time.sleep")
    @mock.patch("army.api.requests.get", side_effect=[requests.Timeout, fake_response(draft_payload(0, 0))])
    def test_retries_timeouts(self, *_):
        start_download("draft", [0])
        batch = DraftBatch.objects.get(number=0)
        self.assertEqual((batch.status, batch.attempts), ("done", 2))

    @mock.patch("army.api.requests.get", side_effect=fake_server(1))
    def test_invalid_batch_fails_command(self, _):
        with self.assertRaisesMessage(CommandError, "draft_people --batch 5"):
            call_command("draft_people", "--batch", "5", stdout=StringIO())
        batch = DraftBatch.objects.get(number=5)
        self.assertEqual((batch.status, batch.attempts), ("failed", 1))
        self.assertIn("Invalid batch id", batch.error)

    def test_refuses_to_start_while_batches_are_active(self):
        DraftBatch.objects.create(number=0, status=DraftBatch.Status.RUNNING)
        with self.assertRaises(CannotStart):
            start_download("draft")
        with self.assertRaises(CannotStart):
            start_download("draft", [0])

    def test_stale_batches_are_marked_interrupted(self):
        fresh = DraftBatch.objects.create(number=0, status=DraftBatch.Status.RUNNING)
        stale = DraftBatch.objects.create(number=1, status=DraftBatch.Status.RUNNING)
        DraftBatch.objects.filter(pk=stale.pk).update(updated_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(mark_interrupted(DraftBatch), 1)
        fresh.refresh_from_db()
        stale.refresh_from_db()
        self.assertEqual((fresh.status, stale.status), ("running", "failed"))
        self.assertIn("Interrupted", stale.error)

    def test_view_starts_drafting_and_rejects_double_submit(self):
        with mock.patch("army.api.requests.get", side_effect=fake_server(1)):
            response = self.client.post("/", {"batches": ""})
        self.assertRedirects(response, "/")
        self.assertEqual(DraftBatch.objects.get(number=0).status, "done")
        DraftBatch.objects.filter(number=0).update(status=DraftBatch.Status.RUNNING)
        response = self.client.post("/", {"batches": "0"})
        self.assertContains(response, "still running")


@override_settings(TASKS=IMMEDIATE_TASKS)
class FightTests(TestCase):
    def test_save_fights_stores_every_event_and_is_idempotent(self):
        create_soldiers(3)
        items = [fight(0, 0, 1, [5, 6, 7]), fight(1, 1, 2, [8])]
        save_fights(0, items)
        save_fights(0, items)
        self.assertEqual(Fight.objects.count(), 2)
        self.assertEqual(FightEvent.objects.count(), 4)
        events = list(Fight.objects.get(external_id=0).events.values_list("order", "attacker__external_id", "damage"))
        self.assertEqual(events, [(0, 0, 5), (1, 1, 6), (2, 0, 7)])

    def test_rejects_unknown_soldiers_and_outsider_steps(self):
        create_soldiers(3)
        with self.assertRaisesMessage(InvalidFightData, "Unknown soldier ids [99]"):
            save_fights(0, [fight(0, 0, 99, [5])])
        bad = fight(1, 0, 1, [5])
        bad["steps"][0]["defender"] = 2
        with self.assertRaises(InvalidFightData):
            save_fights(0, [bad])
        self.assertFalse(Fight.objects.exists())  # the whole batch is rolled back

    def test_simulation_lowers_health_and_picks_winner(self):
        create_soldiers(3)
        save_fights(0, [fight(0, 0, 1, [10, 4, 10]), fight(1, 1, 2, [5, 5])])
        start_simulation()
        health = dict(Person.objects.values_list("external_id", "health"))
        self.assertEqual(health, {0: 96, 1: 75, 2: 95})  # 1 took 20 + 5, 2 took 5
        first, second = Fight.objects.order_by("external_id")
        self.assertEqual((first.blue_damage_taken, first.red_damage_taken, first.winner.external_id), (4, 20, 0))
        self.assertIsNone(second.winner)  # 5 vs 5 is a draw
        self.assertEqual(list(first.events.values_list("defender_health_after", flat=True)), [90, 96, 80])
        self.assertEqual(health_mismatches(), [])

    def test_simulation_is_repeatable_and_floors_health_at_zero(self):
        create_soldiers(2, health=15)
        save_fights(0, [fight(0, 0, 1, [10, 1, 10])])
        start_simulation()
        start_simulation()  # starts again from drafted health
        self.assertEqual(Person.objects.get(external_id=1).health, 0)
        self.assertEqual(Person.objects.get(external_id=0).health, 14)
        self.assertEqual(health_mismatches(), [])

    def test_simulation_needs_fights_and_no_active_run(self):
        create_soldiers(2)
        with self.assertRaisesMessage(CannotStart, "No fights"):
            start_simulation()
        save_fights(0, [fight(0, 0, 1, [5])])
        Fight.objects.update(status=Fight.Status.RUNNING)
        with self.assertRaisesMessage(CannotStart, "already running"):
            start_simulation()

    @override_settings(FIGHT_BATCH_COUNT=2)
    @mock.patch("army.api.requests.get", side_effect=fake_server(2))
    def test_import_fights_and_simulate_commands(self, _):
        create_soldiers(7)
        out = StringIO()
        call_command("import_fights", stdout=out)
        self.assertEqual(FightBatch.objects.filter(status="done").count(), 2)
        self.assertIn("Stored 6 fights with 18 events", out.getvalue())
        call_command("simulate_fights", stdout=out)
        self.assertIn("Health check OK", out.getvalue())
        self.assertEqual(Person.objects.get(external_id=0).health, 100 - 6 * 7)

    def test_simulate_page_shows_results(self):
        create_soldiers(2)
        save_fights(0, [fight(0, 0, 1, [9, 3])])
        response = self.client.post("/simulate", {"action": "simulate"})
        self.assertRedirects(response, "/simulate")
        response = self.client.get("/simulate")
        self.assertContains(response, "Health check OK")
        self.assertContains(response, "3 / 9")  # blue took 3, red took 9
        self.assertContains(response, "Person 0")


@override_settings(TASKS=THREAD_TASKS)
class ParallelSimulationTests(TransactionTestCase):
    """Real threads and a real SQLite file: shared soldiers must not lose health updates."""

    def test_parallel_fights_do_not_lose_updates(self):
        soldiers = create_soldiers(41, health=1000)
        # Soldier 0 fights everyone else at the same time: 40 fights x 3 hits against them.
        save_fights(0, [fight(i, i, 0, [1, 2, 3, 4, 5, 6]) for i in range(1, 41)])
        start_simulation()
        deadline = time.monotonic() + 60
        while Fight.objects.exclude(status=Fight.Status.DONE).exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(Fight.objects.filter(status=Fight.Status.DONE).count(), 40)
        soldiers[0].refresh_from_db()
        self.assertEqual(soldiers[0].health, 1000 - 40 * (1 + 3 + 5))
        self.assertEqual(health_mismatches(), [])
        self.assertGreater(len(set(Fight.objects.values_list("task_id", flat=True))), 39)


PARALLEL = 3
barrier = threading.Barrier(PARALLEL)


@task
def wait_for_peers():
    barrier.wait(timeout=5)  # BrokenBarrierError unless all tasks run at the same time
    return threading.current_thread().name


class ThreadPoolBackendTests(SimpleTestCase):
    def test_tasks_run_in_parallel(self):
        backend = ThreadPoolBackend("test", {"OPTIONS": {"MAX_WORKERS": PARALLEL}})
        results = [backend.enqueue(wait_for_peers, (), {}) for _ in range(PARALLEL)]
        deadline = time.monotonic() + 10
        while not all(r.is_finished for r in results) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual([r.status for r in results], [TaskResultStatus.SUCCESSFUL] * PARALLEL)
        self.assertEqual(len({r.return_value for r in results}), PARALLEL)
        self.assertIs(backend.get_result(results[0].id), results[0])


class BatchFormTests(SimpleTestCase):
    def test_parses_batches(self):
        form = BatchForm({"batches": "5, 2,2"})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["batches"], [2, 5])

    def test_empty_means_all(self):
        form = BatchForm({"batches": ""})
        self.assertTrue(form.is_valid())
        self.assertIsNone(form.cleaned_data["batches"])

    def test_rejects_garbage(self):
        self.assertFalse(BatchForm({"batches": "x, 2"}).is_valid())
