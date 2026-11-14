import threading
import time
from datetime import timedelta
from io import StringIO
from unittest import mock

import requests
from django.core.management import call_command
from django.core.management.base import CommandError
from django.tasks import TaskResultStatus, task
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from .api import ChallengeAPIError, get_json
from .forms import DraftForm
from .models import DraftBatch, Person
from .task_backends import ThreadPoolBackend
from .tasks import DraftingInProgress, mark_interrupted_batches, start_drafting

# Run tasks synchronously in tests so results are deterministic.
IMMEDIATE_TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}


def fake_response(payload, status_code=200):
    response = mock.Mock(status_code=status_code, text=str(payload))
    response.json.return_value = payload
    return response


def person(pid, **extra):
    return {"id": pid, "name": f"Person {pid}", "age": 20 + pid, **extra}


def draft_payload(number, last):
    people = [person(number * 10 + i, rank="Private", health=100) for i in range(10)]
    return {"status": 200, "data": people, "next_batch": None if number == last else number + 1}


def fake_server(batch_count):
    """Simulate /people and /draft/<n> for batch_count batches of 10 people."""

    def get(url, timeout):
        path = url.rsplit("/", 2)
        if path[-1] == "people":
            people = [person(i) for i in range(batch_count * 10)]
            people[-1]["key"] = "draft-info"
            people[-1]["name"] = "Hellstorm Fireburn"
            return fake_response({"status": 200, "data": people})
        if path[-2] == "draft":
            number = int(path[-1])
            if number >= batch_count:
                return fake_response({"status": 400, "data": {"message": "Invalid batch id"}})
            return fake_response(draft_payload(number, batch_count - 1))
        return fake_response({}, status_code=404)

    return get


class GetJsonTests(TestCase):
    @mock.patch("army.api.requests.get", return_value=fake_response({"status": 400, "data": "bad"}))
    def test_error_status_in_body_is_not_retryable(self, _):
        with self.assertRaises(ChallengeAPIError) as ctx:
            get_json("draft/10")
        self.assertFalse(ctx.exception.retryable)

    @mock.patch("army.api.requests.get", return_value=fake_response("", status_code=502))
    def test_5xx_is_retryable(self, _):
        with self.assertRaises(ChallengeAPIError) as ctx:
            get_json("draft/0")
        self.assertTrue(ctx.exception.retryable)

    @mock.patch("army.api.requests.get", return_value=fake_response("", status_code=499))
    def test_gateway_499_is_retryable(self, _):
        with self.assertRaises(ChallengeAPIError) as ctx:
            get_json("draft/0")
        self.assertTrue(ctx.exception.retryable)

    @mock.patch("army.api.requests.get", side_effect=requests.Timeout)
    def test_timeout_is_retryable(self, _):
        with self.assertRaises(ChallengeAPIError) as ctx:
            get_json("draft/0")
        self.assertTrue(ctx.exception.retryable)


@override_settings(TASKS=IMMEDIATE_TASKS)
class ImportAndDraftTests(TestCase):
    @mock.patch("army.api.requests.get", side_effect=fake_server(3))
    def test_import_is_idempotent_and_prints_key(self, _):
        out = StringIO()
        call_command("import_people", stdout=out)
        call_command("import_people", stdout=out)
        self.assertEqual(Person.objects.count(), 30)
        self.assertIn("key: draft-info", out.getvalue())
        self.assertIn("/draft-info", out.getvalue())

    @mock.patch("army.api.requests.get", side_effect=fake_server(3))
    def test_draft_all_ranks_everyone_one_task_per_batch(self, _):
        call_command("import_people", stdout=StringIO())
        start_drafting()
        task_ids = set(DraftBatch.objects.values_list("task_id", flat=True))
        self.assertEqual(len(task_ids - {""}), 3)  # one separate task per batch
        self.assertEqual(Person.objects.filter(rank="Private", health=100).count(), 30)
        self.assertEqual(Person.objects.get(external_id=29).raw.get("rank"), None)  # raw stays as imported
        self.assertEqual(list(DraftBatch.objects.values_list("status", flat=True)), ["done"] * 3)

    @mock.patch("army.api.requests.get", side_effect=fake_server(3))
    def test_follows_next_batch_when_estimate_is_too_low(self, _):
        start_drafting()  # no people imported: estimate is 1 batch
        self.assertEqual(DraftBatch.objects.filter(status="done").count(), 3)
        self.assertEqual(Person.objects.count(), 30)

    @mock.patch("army.tasks.time.sleep")
    @mock.patch("army.api.requests.get", side_effect=[requests.Timeout, fake_response(draft_payload(0, 0))])
    def test_retries_timeouts(self, *_):
        start_drafting([0])
        batch = DraftBatch.objects.get(number=0)
        self.assertEqual((batch.status, batch.attempts), ("done", 2))

    @mock.patch("army.api.requests.get", side_effect=fake_server(1))
    def test_invalid_batch_fails_command(self, _):
        with self.assertRaises(CommandError):
            call_command("draft_people", "--batch", "5", stdout=StringIO())
        batch = DraftBatch.objects.get(number=5)
        self.assertEqual((batch.status, batch.attempts), ("failed", 1))
        self.assertIn("Invalid batch id", batch.error)


    def test_refuses_to_start_while_batches_are_active(self):
        DraftBatch.objects.create(number=0, status=DraftBatch.Status.RUNNING)
        with self.assertRaises(DraftingInProgress):
            start_drafting()
        with self.assertRaises(DraftingInProgress):
            start_drafting([0])

    def test_stale_batches_are_marked_interrupted(self):
        fresh = DraftBatch.objects.create(number=0, status=DraftBatch.Status.RUNNING)
        stale = DraftBatch.objects.create(number=1, status=DraftBatch.Status.RUNNING)
        DraftBatch.objects.filter(pk=stale.pk).update(updated_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(mark_interrupted_batches(), 1)
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
        self.assertContains(response, "still being drafted")


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


class DraftFormTests(TestCase):
    def test_parses_batches(self):
        form = DraftForm({"batches": "5, 2,2"})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["batches"], [2, 5])

    def test_empty_means_all(self):
        form = DraftForm({"batches": ""})
        self.assertTrue(form.is_valid())
        self.assertIsNone(form.cleaned_data["batches"])

    def test_rejects_garbage(self):
        self.assertFalse(DraftForm({"batches": "x, 2"}).is_valid())
