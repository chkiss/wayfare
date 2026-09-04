"""Saying what the tool is doing while it does it.

Reading a batch takes as long as the slowest free model answers, which is
seconds on a good day and most of a minute on a bad one. For all of that time
the browser showed "Sending request to wayfare…", which is both wrong and
useless: the sending finished immediately, and the wait that followed had no
explanation. A person watching that has no way to tell a slow model from a
hung server, and the only available action is to press the button again.

So the work reports what stage it is at, and the page says so. The stages are
named after what is actually happening — reading text, asking models, checking
the result against the calendar — because "please wait" tells nobody anything
they did not already know.

Reporting is a no-op unless a job is bound to the current context, so the CLI
and the tests run through exactly the same code paths with nothing attached.
"""

from __future__ import annotations

import threading
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field

#: How long a finished job stays readable. The page fetches its result once,
#: immediately; this is slack for a reload, not storage.
KEEP_SECONDS = 600

_current: ContextVar["Job | None"] = ContextVar("wayfare_progress_job", default=None)


@dataclass
class Job:
    """One submission being worked on, and how far it has got."""

    id: str
    #: Free text, shown as-is: "Reading return.pdf", "Asking 3 models".
    phase: str = "Starting"
    #: The step within the batch, so a four-file upload shows movement.
    step: int = 0
    total: int = 0
    done: bool = False
    submission_id: str | None = None
    #: Set when the work failed, so the page can say what went wrong rather
    #: than spinning until the user gives up.
    error: str | None = None
    status: int = 500
    finished_at: float | None = None
    history: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "phase": self.phase,
            "step": self.step,
            "total": self.total,
            "done": self.done,
            "submission_id": self.submission_id,
            "error": self.error,
            "status": self.status,
            "history": list(self.history),
        }


_jobs: dict[str, Job] = {}
_lock = threading.Lock()


def start(total: int = 0) -> Job:
    """Register a new job and return it. The caller binds it around the work."""
    job = Job(id=uuid.uuid4().hex[:12], total=total)
    with _lock:
        _sweep()
        _jobs[job.id] = job
    return job


def get(job_id: str) -> Job | None:
    with _lock:
        return _jobs.get(job_id)


def bind(job: Job | None):
    """Make `job` the one that `report` writes to, for this context."""
    return _current.set(job)


def unbind(token) -> None:
    _current.reset(token)


def report(phase: str, step: int | None = None) -> None:
    """Say what is happening now. Silent when nobody is listening."""
    job = _current.get()
    if job is None:
        return
    job.phase = phase
    if step is not None:
        job.step = step
    if not job.history or job.history[-1] != phase:
        job.history.append(phase)


def finish(job: Job, submission_id: str) -> None:
    job.submission_id = submission_id
    job.phase = "Done"
    job.done = True
    job.finished_at = time.monotonic()


def fail(job: Job, message: str, status: int = 500) -> None:
    job.error = message
    job.status = status
    job.done = True
    job.finished_at = time.monotonic()


def _sweep() -> None:
    """Drop jobs nobody came back for. Called with the lock held."""
    now = time.monotonic()
    for job_id, job in list(_jobs.items()):
        if job.finished_at is not None and now - job.finished_at > KEEP_SECONDS:
            del _jobs[job_id]
