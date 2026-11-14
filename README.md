# Django War Simulator (Hard)

A Django 6.1 project for the coderun 2026 Django Challenge (Hard). General Deadlock is
defending his fortress: first register every civilian at peacetime, then draft them as
soldiers through background tasks.

## The challenge

1. **Peacetime.** Download the list of people from `<host>/people` and register all of them.
2. **The key.** One person, *Hellstorm Fireburn*, has a `key` property that points to the
   next instructions at `<host>/<key>`. It was `draft-info` during drafting and has since changed to
   `fight-info` (see [Next stage](#next-stage-not-implemented-yet)). `import_people` always prints the current key.
3. **Drafting.** Those instructions say:
   > Drafting will happen as a series of background processes. You have to implement a
   > background task that downloads all the data. Drafting will add a rank to each person.
   > You will have to migrate your database to support that. Each drafting batch must happen
   > as a separate background task call.

   Hint: *parallelize as much as possible, the server is configured to take quite a long time otherwise.*

Host: `https://coderun2026-csbwf4haa4d0gubu.westus3-01.azurewebsites.net`

## What the API returns

| Endpoint | Response |
|----------|----------|
| `GET /people` | `{"status": 200, "data": [{"id", "name", "age", "key"?}, ...]}`: 100 people, one page. Only Hellstorm Fireburn (id 99) has `key`. Names are not unique; `id` is. |
| `GET /draft-info` | `{"status": 200, "data": {"message": "..."}}`: the instructions above. |
| `GET /draft/<n>` | `{"status": 200, "data": [10 people + "rank" + "health"], "next_batch": n+1 or null}`. Batches run from 0 to 9. |
| `GET /draft/10` | `{"status": 400, "data": {"message": "Invalid batch id"}}` with HTTP 200, so the body's `status` must be checked. |

Ranks are `Private`, `Sergeant`, `Lieutenant` and `General`. `/draft/<n>` is slow: a single request
takes 6 to 35 s, the server seems to answer roughly one draft request at a time, and under load
Azure's front end cuts requests off after about 240 s with HTTP 504 or 499.

## Setup

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser   # optional, for /admin/
```

Environment variables (all optional):

| Variable | Default | Purpose |
|----------|---------|---------|
| `CHALLENGE_HOST` | the host above | API base URL |
| `TASKS_MODE` | `threads` | how background tasks run (see below) |
| `DJANGO_SECRET_KEY` | random per process | set it for anything beyond local dev |
| `DJANGO_DEBUG` | `1` | `0` to turn debug off |

## Usage

```sh
python manage.py import_people   # Phase 1: register everyone, print Hellstorm's key and next URL
python manage.py draft_people    # Phase 2: draft everyone, one background task per batch
python manage.py runserver       # http://127.0.0.1:8000/ overview page and draft form
python manage.py test army       # offline tests (HTTP is mocked)
```

`draft_people --batch 4 --batch 7` re-drafts only those batches. When batches fail, the
command prints the exact retry command and exits with code 1.

### Background task modes

Drafting uses Django's built-in tasks framework (`django.tasks`), so the task code is the same
in both modes. Only `TASKS_MODE` changes.

- **`threads` (default).** `army.task_backends.ThreadPoolBackend` runs each enqueued task on a
  pool of 16 threads inside the current process (the `draft_people` command or `runserver`).
  Nothing else needs to run. Pending tasks are lost if the process stops.
- **`database`.** Tasks are stored in the database by
  [`django-tasks-db`](https://github.com/RealOrangeOne/django-tasks-db) and executed by separate
  worker processes, so they survive restarts:

  ```sh
  export TASKS_MODE=database
  python manage.py run_workers --count 10   # terminal 1: 10 worker processes, one per batch
  python manage.py draft_people             # terminal 2 (or use the web form)
  ```

## How it works

```
import_people ──> GET /people ──> Person rows (update_or_create on external_id)

draft_people / web form
   └─ start_drafting()            reserve DraftBatch rows 0..9, refuse if a run is active
        └─ draft_all task         enqueue one draft_batch task per batch
             ├─ draft_batch(0) ──> GET /draft/0 ──> rank + health saved on 10 Persons
             ├─ draft_batch(1)    ... all batches run at the same time
             └─ draft_batch(9)    next_batch is followed if more batches exist
```

| File | Role |
|------|------|
| `army/models.py` | `Person` holds the `/people` fields plus `raw` (the original JSON), and after drafting `rank`, `health`, `draft_raw` and `drafted_at`. `DraftBatch` records each batch's status, attempts, response, error and timing. |
| `army/migrations/` | `0001` creates `Person`, `0002` adds the drafting fields and `DraftBatch`, `0003` adds `DraftBatch.updated_at`. |
| `army/api.py` | HTTP client with timeouts. Errors are reported both from the HTTP status and from the body's `status`. Timeouts, connection errors, 5xx and gateway 499 are retryable; other 4xx are not. Every response is logged with its status and duration. |
| `army/tasks.py` | `start_drafting`, the `draft_all` and `draft_batch` tasks, retries with backoff and jitter (up to 5 attempts), and marking interrupted batches as failed. |
| `army/task_backends.py` | The in-process thread-pool task backend. |
| `army/management/commands/` | `import_people`, `draft_people` and `run_workers`. |
| `army/views.py`, `forms.py`, `templates/army/index.html` | Overview page: batch status, rank totals and soldiers. `DraftForm` starts a full draft or re-drafts chosen batches. The page refreshes itself while drafting runs. |
| `army/admin.py` | Admin for `Person` and `DraftBatch`. |
| `army/tests.py` | 16 tests: API error handling, idempotent import, one task per batch, `next_batch` discovery, retries, invalid batches, the overlap guard, interrupted batches, the view, the form, and that the thread pool really runs tasks in parallel. |

### Design notes

- **Idempotent.** Importing and drafting both use `update_or_create` on the API `id`, so they can be re-run safely.
- **No lost data.** The original `/people` object and the `/draft/<n>` object are stored as JSON next to the mapped fields.
- **Parallel.** Every batch is enqueued at once instead of walking the `next_batch` chain one batch at a time.
  The batch count is estimated from the number of registered people (`DRAFT_BATCH_SIZE = 10`).
- **Safe re-runs.** Starting a run while batches are active is refused, so a double submit doesn't add
  to the slow server's queue. Batches with no progress for `DRAFT_STALE_AFTER` seconds (for example
  after a restart) are marked interrupted, so they can be drafted again.
- **SQLite.** Several threads or processes write at once, so SQLite waits up to 20 s for locks
  (`transaction_mode: IMMEDIATE`).

## Results

- **Import:** 100 people registered, and the count matches the JSON.
- **Draft:** all 100 people drafted: 29 General, 28 Sergeant, 27 Lieutenant, 16 Private.
- **Timing:** a full parallel draft took 262 s when the server was quiet. When it was busy, several
  requests hit the gateway timeout and were retried (499 and 504 errors are retried since then).
  Database mode was checked against the live server with 2 workers on one batch.

## Next stage (not implemented yet)

After drafting, Hellstorm Fireburn's key changed to `fight-info`. `<host>/fight-info` says:

> Now you will simulate actual fights. The fight data will be provided yo you as a list of fights
> of events. Each fight contains two soldier ids and a list of events. Each event contains who
> attacked who, and how much damage the defender takes from the attacker. You have to simulate each
> individual fight as a background process. Each event for each fight must be saved into the
> database. Watch out for the database design. Watch out for concurrency issues while processing
> fights in parallel. Your last task will be found at /simulate-fight-info. Good luck

The fight data endpoint and `/simulate-fight-info` haven't been explored yet. The task framework,
`health` field and API client are ready to build on.
