# Django War Simulator (Hard)

General Deadlock's fortress: register people at peacetime, then draft them as soldiers
using background tasks.

## Setup

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
```

Optional environment variables:

| Variable            | Default                                  |
|---------------------|------------------------------------------|
| `DJANGO_SECRET_KEY` | random per process (fine for local dev)  |
| `DJANGO_DEBUG`      | `1`                                      |
| `CHALLENGE_HOST`    | the coderun2026 Azure host               |

## Run

```sh
python manage.py import_people   # Phase 1: register everyone from <host>/people
python manage.py draft_people    # Phase 2: draft everyone via <host>/draft/<n>
python manage.py runserver       # http://127.0.0.1:8000/ (overview + draft form)
```

`draft_people --batch 6 --batch 7` re-drafts only those batches. `--timeout` sets how many
seconds to wait (default 900). Use `python manage.py createsuperuser` to log in at `/admin/`.

## How it works

**Phase 1, peacetime.** `<host>/people` returns `{"status": 200, "data": [{id, name, age, key?}, ...]}`
with 100 people. `import_people` saves them with `update_or_create` on `external_id`,
so running it again updates instead of duplicating. It checks the stored count against the
JSON and prints Hellstorm Fireburn's `key` (`draft-info`) and the next URL.

**Phase 2, drafting.** `<host>/draft-info` says drafting happens as background processes,
adds a rank to each person, needs a database migration, and every batch must be a separate
background task call. `<host>/draft/<n>` returns 10 people with `rank` and `health`, plus
`next_batch` (`null` on the last batch, 9). An unknown batch returns `"status": 400` in the body.

- `army/tasks.py` uses Django's built-in tasks framework (`django.tasks`).
  `draft_all` estimates the batch count from registered people and enqueues one `draft_batch`
  task per batch, so they all run at once. Each `draft_batch` downloads its batch, stores
  `rank`, `health` and the raw JSON on `Person`, and records status and response in `DraftBatch`.
  It also follows `next_batch` in case the estimate was too low.
- `army/task_backends.py` is a small `ThreadPoolBackend`. Django only ships backends that run
  tasks synchronously or not at all, so this one runs each enqueued task on a thread pool
  (16 workers). Tasks live in memory, so pending tasks are lost if the process stops. Swapping in a
  durable worker backend (for example the `django-tasks` package) only requires changing `TASKS`.
- `army/api.py` holds the HTTP client: timeouts, HTTP errors, and errors reported in the body.
  Timeouts, connection errors and 5xx are retried (3 attempts, `DRAFT_MAX_ATTEMPTS`); 4xx are not.
  Every response is logged with its status and duration.
- `army/views.py`, `forms.py`, `templates/army/index.html`: the overview page lists batch status,
  rank totals and soldiers. `DraftForm` starts drafting for everyone or for chosen batches.
  The page refreshes itself while batches are running.
- `army/admin.py` registers `Person` and `DraftBatch`.

The draft endpoint is slow (15 to 180 s per request) and gets slower under parallel load,
so `CHALLENGE_DRAFT_TIMEOUT` is 180 s. SQLite waits up to 20 s for write locks because
several threads write at once.
