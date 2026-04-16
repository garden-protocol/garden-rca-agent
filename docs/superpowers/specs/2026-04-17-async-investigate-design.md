# Async `/investigate` Endpoint (Batch D)

**Date:** 2026-04-17
**Status:** Approved (auto mode), pending implementation
**Motivation:** After Batch B, the specialist iterates with Loki + on-chain tools, so end-to-end investigation time regularly exceeds Cloudflare's 100s proxy timeout. Discord users see `error code: 524`. Verified via direct `curl` against the prod endpoint.

---

## Goal

Replace the synchronous `POST /investigate/{server_secret}` with an async pattern:
- POST enqueues the job, returns immediately with a job id.
- GET polls for status and result.

The actual investigation work runs on the RCA server unbothered by any HTTP timeout.

## Non-goals

- Persistent job store (Redis, Postgres). In-memory is fine for single-replica today; swappable later.
- Webhooks / push notifications. Polling is simpler and avoids needing an inbound URL from the Discord bot.
- Changing `/explore`, `/study`, `/health` — only `/investigate` is long-running.

---

## Design

### API

**`POST /investigate/{server_secret}`** — returns 202 Accepted immediately:

```json
{
  "job_id": "7c9e6...",
  "status": "queued",
  "poll_url": "/jobs/{server_secret}/7c9e6..."
}
```

No `InvestigateResponse` body on POST anymore — callers must poll.

**`GET /jobs/{server_secret}/{job_id}`** — returns 200 with one of three shapes depending on status:

```json
// queued or running
{"status": "queued" | "running", "created_at": "...", "started_at": "..." | null}

// done
{"status": "done", "created_at": "...", "started_at": "...", "finished_at": "...", "result": <InvestigateResponse>}

// failed
{"status": "failed", "created_at": "...", "started_at": "...", "finished_at": "...", "error": "..."}
```

404 if the job id is unknown or expired.

### Job store

New module `jobs.py` (in project root next to `main.py`):

```python
class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"

@dataclass
class Job:
    id: str
    status: JobStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: InvestigateResponse | None = None
    error: str | None = None

_JOBS: dict[str, Job] = {}
_TTL = timedelta(hours=1)
_LOCK = asyncio.Lock()

async def create() -> Job: ...
async def set_running(job_id: str) -> None: ...
async def set_done(job_id: str, result: InvestigateResponse) -> None: ...
async def set_failed(job_id: str, error: str) -> None: ...
async def get(job_id: str) -> Job | None: ...  # returns None if missing or expired
async def purge_expired() -> int: ...          # called opportunistically
```

- `create()` generates `uuid.uuid4().hex`, stores a new `Job(status=QUEUED)`, returns it.
- `get()` checks `_JOBS[job_id]`; if `finished_at` is set and older than `_TTL`, deletes and returns None.
- On every `create()` / `get()` we opportunistically call `purge_expired()` — no separate background task.
- `asyncio.Lock` guards the dict (FastAPI runs multiple coroutines concurrently).

### Runner

In `main.py`:

```python
async def _run_investigation(job_id: str, order_id: str, investigate: bool):
    await jobs.set_running(job_id)
    try:
        result = await asyncio.to_thread(orchestrator.investigate, order_id, investigate)
        await jobs.set_done(job_id, result)
    except Exception as exc:
        logger.exception("Job %s failed", job_id)
        await jobs.set_failed(job_id, f"{type(exc).__name__}: {exc}")
```

POST handler:

```python
@app.post("/investigate/{server_secret}", status_code=202)
async def investigate_order(server_secret: str, req: InvestigateRequest):
    if server_secret != settings.server_secret:
        raise HTTPException(status_code=403, detail="Forbidden")
    job = await jobs.create()
    asyncio.create_task(_run_investigation(job.id, req.order_id, req.investigate))
    return {
        "job_id": job.id,
        "status": job.status.value,
        "poll_url": f"/jobs/{server_secret}/{job.id}",
    }
```

GET handler:

```python
@app.get("/jobs/{server_secret}/{job_id}")
async def get_job(server_secret: str, job_id: str):
    if server_secret != settings.server_secret:
        raise HTTPException(status_code=403, detail="Forbidden")
    job = await jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    payload = {
        "status": job.status.value,
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }
    if job.status == JobStatus.DONE:
        payload["result"] = job.result.model_dump(mode="json")
    elif job.status == JobStatus.FAILED:
        payload["error"] = job.error
    return payload
```

### Discord bot

Replace the single POST-and-render with: POST → start loop → poll every 5s → stop on terminal status → render. Cap polling at 14 minutes (safe inside Discord's 15-min defer window).

```python
import time

async def _investigate_with_polling(interaction, order_id: str, investigate: bool) -> dict | None:
    url = f"{RCA_AGENT_URL}/investigate/{SERVER_SECRET}"
    async with httpx.AsyncClient(timeout=30.0) as http:
        resp = await http.post(url, json={"order_id": order_id, "investigate": investigate})
        resp.raise_for_status()
        enqueue = resp.json()

    poll_url = f"{RCA_AGENT_URL}{enqueue['poll_url']}"
    start = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as http:
        while time.monotonic() - start < 14 * 60:
            await asyncio.sleep(5)
            try:
                r = await http.get(poll_url)
                r.raise_for_status()
                job = r.json()
            except Exception as exc:
                logger.warning("poll transient error: %s", exc)
                continue
            if job["status"] == "done":
                return job["result"]
            if job["status"] == "failed":
                await interaction.followup.send(f"❌ Investigation failed: {job.get('error','unknown')}")
                return None
        await interaction.followup.send("❌ Investigation exceeded 14 min; job may still be running")
        return None
```

Then the slash command handler:

```python
@client.tree.command(...)
async def investigate(interaction, order_id: str, investigate: bool = False):
    await interaction.response.defer(thinking=True)
    data = await _investigate_with_polling(interaction, order_id, investigate)
    if data is None:
        return  # already sent an error followup
    embed = _build_early_return_embed(data) if data.get("early_return") else _build_rca_embed(data)
    await interaction.followup.send(embed=embed)
```

### Concurrency

- `jobs.py` guards the dict with `asyncio.Lock`.
- Multiple concurrent POSTs each get a unique job id; they each run on their own task.
- No dedup of same-order investigations — two calls = two jobs. Simplest; can add dedup later.

### Graceful shutdown

The lifespan `shutdown` log already warns. Active jobs are lost on process exit; clients polling will get 404 when the process restarts. Acceptable for now.

---

## File change summary

| File | Action | Responsibility |
|---|---|---|
| `jobs.py` | Create | In-memory job store with TTL and asyncio lock |
| `main.py` | Modify | POST returns 202 + job id; new GET /jobs handler |
| `discord_bot.py` | Modify | Replace inline post-and-render with post + poll loop |
| `tests/jobs/__init__.py` | Create | Package marker |
| `tests/jobs/test_job_store.py` | Create | Unit tests for `jobs.py` (offline, no FastAPI) |
| `tests/test_async_endpoints.py` | Create | FastAPI TestClient tests for POST 202 + GET shapes, using a stubbed `orchestrator.investigate` |

## Testing

- `jobs.create()` returns a unique id, status=queued.
- `jobs.set_running`, `set_done`, `set_failed` transition as expected.
- `jobs.get()` returns None for missing id.
- `jobs.get()` returns None for expired finished job; does not return None for running jobs older than TTL (they haven't finished).
- `purge_expired()` removes only finished+expired jobs.
- POST `/investigate/{bad}` → 403.
- POST `/investigate/{ok}` with stubbed orchestrator → 202 + payload shape.
- GET `/jobs/{bad}/{id}` → 403.
- GET `/jobs/{ok}/unknown-id` → 404.
- Full happy path: POST → poll GET until status=done → result matches stubbed value.

## Rollout

Single PR on `feat/async-investigate`, squash-merge to main. Breaking change for any non-Discord caller of `/investigate`. Discord bot updated in the same PR, so no window of broken behavior.

## Risks

- **Server restart drops in-flight jobs.** Mitigation: noted in shutdown log. Future: persistent store.
- **Poll thrash if a job stalls.** Cap is 14 min; after that the bot reports timeout but the job continues server-side until completion (or orchestrator's own limits).
- **`httpx.AsyncClient` connection churn.** We open one client for the post and one for the poll loop — the poll loop reuses a single client across its 5s iterations. Acceptable.
