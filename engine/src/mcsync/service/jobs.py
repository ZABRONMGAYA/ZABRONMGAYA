"""Background jobs: import, sync, export.

Each job runs on its own thread, reports progress (throttled to 10 Hz) and
can be cancelled cooperatively. Completion is announced with ``job.done`` or
``job.failed`` notifications carrying the result or the error.
"""

from __future__ import annotations

import itertools
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mcsync.media.extract import ExtractionCancelled
from mcsync.sync.engine import SyncCancelled

Notify = Callable[[str, Any], None]
_MIN_INTERVAL_S = 0.1


class JobCancelled(Exception):
    pass


@dataclass
class Job:
    id: str
    kind: str
    notify: Notify
    cancel: threading.Event = field(default_factory=threading.Event)
    status: str = "running"  # running | done | failed | cancelled
    progress: float = 0.0
    message: str = ""
    result: Any = None
    error: str | None = None
    finished: threading.Event = field(default_factory=threading.Event)
    _last_report: float = 0.0

    def report(self, fraction: float, message: str = "") -> None:
        self.progress, self.message = max(0.0, min(1.0, fraction)), message
        now = time.monotonic()
        if now - self._last_report >= _MIN_INTERVAL_S or fraction >= 1.0:
            self._last_report = now
            self.notify("job.progress", {"job_id": self.id, "kind": self.kind, "progress": self.progress,
                                         "message": message})  # fmt: skip

    def check_cancelled(self) -> None:
        if self.cancel.is_set():
            raise JobCancelled()

    def summary(self) -> dict:
        return {"job_id": self.id, "kind": self.kind, "status": self.status, "progress": self.progress,
                "message": self.message, "error": self.error}  # fmt: skip


class JobManager:
    def __init__(self, notify: Notify, *, log: Callable[[str], None] | None = None) -> None:
        self.notify = notify
        self.log = log or (lambda text: None)
        self.jobs: dict[str, Job] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def running(self, kinds: set[str] | None = None) -> list[Job]:
        return [j for j in self.jobs.values() if j.status == "running" and (kinds is None or j.kind in kinds)]

    def start(self, kind: str, work: Callable[[Job], Any]) -> Job:
        with self._lock:
            job = Job(id=f"job-{next(self._ids)}", kind=kind, notify=self.notify)
            self.jobs[job.id] = job

        def run() -> None:
            try:
                job.result = work(job)
                job.status = "done"
                job.report(1.0, "Done")
                self.notify("job.done", {"job_id": job.id, "kind": kind, "result": job.result})
            except (JobCancelled, SyncCancelled, ExtractionCancelled):
                job.status = "cancelled"
                self.notify("job.failed", {"job_id": job.id, "kind": kind, "cancelled": True, "error": "cancelled"})
            except Exception as exc:  # noqa: BLE001 - reported to the client
                job.status, job.error = "failed", f"{type(exc).__name__}: {exc}"
                self.log(traceback.format_exc())
                self.notify("job.failed", {"job_id": job.id, "kind": kind, "cancelled": False, "error": job.error})
            finally:
                job.finished.set()

        threading.Thread(target=run, name=f"{kind}-{job.id}", daemon=True).start()
        return job

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.status != "running":
            return False
        job.cancel.set()
        return True

    def cancel_all(self) -> None:
        for job in self.running():
            job.cancel.set()

    def wait(self, job_id: str, timeout: float | None = None) -> Job:
        job = self.jobs[job_id]
        job.finished.wait(timeout)
        return job
