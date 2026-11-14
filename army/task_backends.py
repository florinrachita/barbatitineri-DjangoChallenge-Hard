"""In-process django.tasks backend that runs each enqueued task on a thread pool.

Django 6.x only ships ImmediateBackend (runs tasks synchronously) and DummyBackend
(never runs them). This backend reuses ImmediateBackend's execution logic but submits
each task to a ThreadPoolExecutor, so independent tasks run in parallel.

Limits: results live in memory, so pending tasks are lost if the process exits.
For a durable queue, point settings.TASKS at a worker-based backend
(e.g. the `django-tasks` package); the task code does not change.
"""

from concurrent.futures import ThreadPoolExecutor

from django.db import connections
from django.tasks.backends.immediate import ImmediateBackend
from django.tasks.base import TaskResult, TaskResultStatus
from django.tasks.exceptions import TaskResultDoesNotExist
from django.utils.crypto import get_random_string


class ThreadPoolBackend(ImmediateBackend):
    supports_get_result = True

    def __init__(self, alias, params):
        super().__init__(alias, params)
        self._executor = ThreadPoolExecutor(
            max_workers=self.options.get("MAX_WORKERS", 16),
            thread_name_prefix=f"task-{alias}",
        )
        self._results = {}

    def enqueue(self, task, args, kwargs):
        self.validate_task(task)
        task_result = TaskResult(
            task=task,
            id=get_random_string(32),
            status=TaskResultStatus.READY,
            enqueued_at=None,
            started_at=None,
            last_attempted_at=None,
            finished_at=None,
            args=args,
            kwargs=kwargs,
            backend=self.alias,
            errors=[],
            worker_ids=[],
        )
        self._results[task_result.id] = task_result
        self._executor.submit(self._run, task_result)
        return task_result

    def _run(self, task_result):
        try:
            # Relies on ImmediateBackend's internal runner (status, signals, errors).
            self._execute_task(task_result)
        finally:
            # Worker threads are not request threads; close their DB connections ourselves.
            connections.close_all()

    def get_result(self, result_id):
        try:
            return self._results[result_id]
        except KeyError:
            raise TaskResultDoesNotExist(result_id) from None
