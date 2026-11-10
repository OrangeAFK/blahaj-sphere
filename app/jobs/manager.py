"""In-process asyncio worker for jobs (SPEC §6.3) plus the change stream behind SSE."""

import asyncio
import inspect
import time
from typing import AsyncIterator, Awaitable, Callable

from app.jobs.store import Job, JobHandle, JobStore

JobFn = Callable[[JobHandle], None] | Callable[[JobHandle], Awaitable[None]]

POLL_S = 0.25
KEEPALIVE_S = 15.0
SNAPSHOT_RECENT = 10
LOOKBACK_NS = 2_000_000_000


class JobManager:
    def __init__(self, store: JobStore, concurrency: int = 2):
        self.store = store
        self.concurrency = concurrency
        self._queue: asyncio.Queue[tuple[str, JobFn]] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._workers: list[asyncio.Task] = []

    @property
    def started(self) -> bool:
        return self._queue is not None

    async def start(self) -> None:
        if self.started:
            return
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self.store.fail_interrupted()
        self._workers = [asyncio.create_task(self._worker()) for _ in range(self.concurrency)]

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers, self._queue, self._loop = [], None, None

    def submit(self, type: str, fn: JobFn, node_id: str | None = None) -> Job:
        """Queue `fn(handle)` (sync or async). Safe to call from any thread."""
        if self._queue is None or self._loop is None:
            raise RuntimeError("job manager not started")
        job = self.store.create(type, node_id)
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (job.id, fn))
        return job

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            job_id, fn = await self._queue.get()
            handle = JobHandle(self.store, job_id)
            try:
                self.store.set_status(job_id, "running")
                if inspect.iscoroutinefunction(fn):
                    await fn(handle)
                else:
                    await asyncio.to_thread(fn, handle)
                handle.done()
            except asyncio.CancelledError:
                handle.fail("cancelled")
                raise
            except Exception as e:  # a failing job must never kill the worker
                handle.fail_with_exception(e)

    async def events(self, poll_s: float = POLL_S, keepalive_s: float = KEEPALIVE_S) -> AsyncIterator[Job | None]:
        """Yield a snapshot (active + recent jobs), then every job change. `None` = keepalive tick.

        Polls the shared DB so changes from other processes (MCP tool calls) stream too."""
        cursor = time.time_ns()  # taken before the snapshot so nothing changing meanwhile is missed
        snapshot = {j.id: j for j in await asyncio.to_thread(self.store.list_jobs, True, 200)}
        for j in await asyncio.to_thread(self.store.list_jobs, False, SNAPSHOT_RECENT):
            snapshot.setdefault(j.id, j)
        seen: dict[str, int] = {}
        for j in sorted(snapshot.values(), key=lambda j: j.updated_ns):
            seen[j.id] = j.updated_ns
            yield j
        last_sent = time.monotonic()
        while True:
            await asyncio.sleep(poll_s)
            # Look back a little: another process may commit a row stamped just before our cursor.
            changed = await asyncio.to_thread(self.store.changed_since, cursor - LOOKBACK_NS)
            for j in changed:
                if seen.get(j.id) != j.updated_ns:
                    seen[j.id] = j.updated_ns
                    yield j
                    last_sent = time.monotonic()
                cursor = max(cursor, j.updated_ns)
            # Prune bookkeeping for rows older than the cursor; they cannot reappear.
            if len(seen) > 500:
                seen = {k: v for k, v in seen.items() if v >= cursor - LOOKBACK_NS}
            if time.monotonic() - last_sent >= keepalive_s:
                last_sent = time.monotonic()
                yield None
