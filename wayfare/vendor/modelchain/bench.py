"""Remembering which models are not worth trying, and until when.

Storage is deliberately an interface. A long-running service wants this on
disk so a bench survives a restart; a request-scoped process may not want to
persist anything at all. Neither choice belongs in the policy.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable


class Bench:
    """Base class: which models are benched, and why."""

    #: An expired record is kept this long so a report can still explain a
    #: recent bench, then dropped.
    PRUNE_AFTER_SECONDS = 7 * 24 * 3600

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock

    # --- storage, for subclasses to provide ------------------------------
    def _load(self) -> dict:
        raise NotImplementedError

    def _save(self, state: dict) -> None:
        raise NotImplementedError

    # --- policy ----------------------------------------------------------
    def usable(self, model: str) -> bool:
        record = self._load().get(model)
        if record is None:
            return True
        until = record.get("until")
        if until is None:
            return False  # Benched until a human clears it.
        return self.clock() >= until

    def bench(self, model: str, why, seconds: int | None) -> None:
        """Bench a model. ``seconds=None`` means until a human clears it.

        An existing longer bench is never shortened: a model benched for a day
        because its free window is spent should not come back in two minutes
        because it also timed out once.
        """
        state = self._load()
        now = self.clock()

        held = state.get(model)
        if held is not None:
            until = held.get("until")
            if until is None:
                return  # Waiting for a human already; nothing outranks that.
            if seconds is not None and until > now + seconds:
                return

        state = self._prune(state, now)
        state[model] = {
            "until": (now + seconds) if seconds else None,
            "why": str(why)[:120],
            "since": now,
        }
        self._save(state)

    def _prune(self, state: dict, now: float) -> dict:
        """Drop records whose cooldown lapsed long ago.

        Without this the state file grows for the life of the service, one
        entry per model that ever hiccupped.
        """
        keep = {}
        for model, record in state.items():
            until = record.get("until")
            if until is None or until > now - self.PRUNE_AFTER_SECONDS:
                keep[model] = record
        return keep

    def restore(self, model: str) -> bool:
        """Put a benched model back into service. True if it was benched."""
        state = self._load()
        if model not in state:
            return False
        del state[model]
        self._save(state)
        return True

    def benched(self) -> dict:
        """Every model currently out of service, with its record."""
        return {m: r for m, r in self._load().items() if not self.usable(m)}

    def report(self) -> list[str]:
        """One readable line per benched model."""
        from .classify import bench_reason

        now = self.clock()
        lines = []
        for model, record in sorted(self.benched().items()):
            until = record.get("until")
            if until is None:
                when = "until a human clears it"
            else:
                minutes = int((until - now) / 60)
                when = (
                    f"retry in ~{minutes}m"
                    if 0 <= minutes < 120
                    else "retry after " + time.strftime("%b %d %H:%M", time.localtime(until))
                )
            lines.append(f"{model} — {bench_reason(record.get('why'))} ({when})")
        return lines


class MemoryBench(Bench):
    """For a process that does not outlive the request."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        super().__init__(clock)
        self._state: dict = {}

    def _load(self) -> dict:
        return self._state

    def _save(self, state: dict) -> None:
        self._state = state


class JsonFileBench(Bench):
    """For a service that should not forget a bench across a restart."""

    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time) -> None:
        super().__init__(clock)
        self.path = Path(path)

    def _load(self) -> dict:
        try:
            with self.path.open(encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, ValueError):
            return {}
        return state if isinstance(state, dict) else {}

    def _save(self, state: dict) -> None:
        # Written via a temporary file: a half-written bench file reads as no
        # bench at all, which would silently re-enable every channel.
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(state), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            pass  # Losing the bench is survivable; crashing the caller is not.
