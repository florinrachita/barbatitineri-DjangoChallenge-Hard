# Django War Simulator (Hard)

A Django 6.1 project for the coderun 2026 Django Challenge (Hard). General Deadlock is
defending his fortress: first register every civilian at peacetime, then draft them as
soldiers through background tasks.

## The challenge

1. **Peacetime.** Download the list of people from `<host>/people` and register all of them.
2. **The key.** One person, *Hellstorm Fireburn*, has a `key` property that points to the
   next instructions at `<host>/<key>`. It was `draft-info` during drafting and later changed to
   `fight-info`. `import_people` always prints the current key.
3. **Drafting.** Those instructions say:
   > Drafting will happen as a series of background processes. You have to implement a
   > background task that downloads all the data. Drafting will add a rank to each person.
   > You will have to migrate your database to support that. Each drafting batch must happen
   > as a separate background task call.

   Hint: *parallelize as much as possible, the server is configured to take quite a long time otherwise.*
4. **Fights** (`<host>/fight-info`). Fight data is a list of fights; each has two soldier ids and
   a list of events (who attacked whom, and the damage). Simulate each fight as a background
   process, save every event in the database, and watch out for database design and concurrency.
5. **Simulation** (`<host>/simulate-fight-info`). Expose a page at `/simulate`, replay each fight in its
   own background process (in parallel), lower the defender's health for each event, and show the
   results in HTML.

Host: `https://coderun2026-csbwf4haa4d0gubu.westus3-01.azurewebsites.net`

## What the API returns

| Endpoint | Response |
|----------|----------|
| `GET /people` | `{"status": 200, "data": [{"id", "name", "age", "key"?}, ...]}`: 100 people, one page. Only Hellstorm Fireburn (id 99) has `key`. Names are not unique; `id` is. |
| `GET /draft-info` | `{"status": 200, "data": {"message": "..."}}`: the instructions above. |
| `GET /draft/<n>` | `{"status": 200, "data": [10 people + "rank" + "health"], "next_batch": n+1 or null}`. Batches run from 0 to 9. |
| `GET /draft/10` | `{"status": 400, "data": {"message": "Invalid batch id"}}` with HTTP 200, so the body's `status` must be checked. |
| `GET /fight/<n>` | `{"status": 200, "data": [30 fights], "next_batch": ...}`. Batches 0 to 9 (fights 0 to 299); `/fight/10` is invalid. A fight is `{"fight_id", "blue_soldier_id", "red_soldier_id", "steps": [{"attacker", "defender", "damage"}]}`. Blue attacks first, they alternate, damage is 5 to 10, and a soldier can be in several fights. |

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
python manage.py import_fights   # Phase 3: download fights + events, one background task per batch
python manage.py simulate_fights # Phase 4: replay every fight as its own background task
python manage.py runserver       # http://127.0.0.1:8000/ (drafting) and /simulate (fights)
python manage.py test army       # offline tests (HTTP is mocked)
```

`draft_people --batch 4 --batch 7` (and `import_fights --batch N`) re-runs only those batches. When batches fail, the
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
   └─ start_download("draft")     reserve DraftBatch rows 0..9, refuse if a run is active
        └─ download_all task      enqueue one download_batch task per batch
             ├─ download_batch(0) ──> GET /draft/0 ──> rank + health saved on 10 Persons
             └─ ...               all batches run at the same time; next_batch is followed

import_fights / /simulate "Download fights"
   └─ start_download("fight")  ──> same flow ──> Fight + FightEvent rows (300 fights)

simulate_fights / /simulate "Replay all fights"
   └─ start_simulation()          reset health to drafted values, refuse if a run is active
        └─ simulate_all task      enqueue one simulate_fight task per fight (300, in parallel)
             └─ simulate_fight(id)  for each event: UPDATE health = MAX(health - damage, 0)
```

### Concurrency

A soldier can be in several fights, and fights run in parallel, so many tasks lower the same
soldier's health at once. Reading health in Python, subtracting and saving would lose updates.
`fights.apply_event` does the subtraction inside the database in one `UPDATE` statement
(`Greatest(F("health") - damage, 0)`), in a short transaction that also records
`defender_health_after` on the event. SQLite takes the write lock at the start of each
transaction (`transaction_mode: IMMEDIATE`).

Health only goes down, so each soldier's final health must equal drafted health minus all damage
received (floored at 0), whatever order the fights ran in. `fights.health_mismatches()` checks this
after every simulation; the result is shown on `/simulate` and by `simulate_fights`. A test runs 40
fights in real threads against one shared soldier. Replacing the `UPDATE` with read-modify-write
makes it fail (898 instead of 640).

Each fight's winner is the soldier who took less damage *in that fight* (equal damage is a draw),
so the result doesn't depend on the order of other fights.

| File | Role |
|------|------|
| `army/models.py` | `Person` (with `raw`, `rank`, `health`, `draft_raw`), `DraftBatch` / `FightBatch` (status and response of each batch download, sharing an abstract `BatchLog`), `Fight` (two soldiers, raw JSON, simulation result) and `FightEvent` (one row per step, ordered, with `defender_health_after`). Constraints: unique event order per fight, a soldier can't fight or attack themselves. |
| `army/migrations/` | `0001` Person, `0002` drafting fields + DraftBatch, `0003` `updated_at`, `0004` fights, events and FightBatch. |
| `army/api.py` | HTTP client. Errors are reported from both the HTTP status and the body's `status`. Timeouts, connection errors, 5xx and gateway 499 are retried; other 4xx are not. Every response is logged. |
| `army/batches.py` | Shared batch download logic: reserving rows, retries with backoff and jitter, interrupted-run detection. |
| `army/drafting.py`, `army/fights.py` | Saving soldiers; saving and validating fights; replaying fights and the health check. |
| `army/tasks.py` | The background tasks (`download_all`, `download_batch`, `simulate_all`, `simulate_fight`) and the functions that start them. |
| `army/task_backends.py` | The in-process thread-pool task backend. |
| `army/management/commands/` | `import_people`, `draft_people`, `import_fights`, `simulate_fights`, `run_workers`. |
| `army/views.py`, `forms.py`, `templates/army/` | `/` drafting overview; `/simulate` with forms to download fights and replay them, fight results (damage, winner, every event), soldiers' health and the health check. Pages refresh while tasks run. |
| `army/admin.py` | Admin for all models; fights show their events inline. |
| `army/tests.py` | 23 offline tests, including the real-thread concurrency test. |

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
- **Draft:** all 100 people drafted: 29 General, 28 Sergeant, 27 Lieutenant, 16 Private. A full
  parallel draft took 262 s when the server was quiet; when it was busy, gateway timeouts were retried.
- **Fights:** the fight download, simulation and `/simulate` page pass the offline tests, including the
  concurrency test. A full live run against `/fight/0..9` hadn't finished at submission time: the
  server was answering slowly (up to 240 s per request). Run `import_fights` and then `simulate_fights`.
