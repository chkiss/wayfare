"""Walking a chain of models until one answers.

The caller supplies a function that attempts exactly one model and reports
whether it worked. Everything about *how* to call a model — the HTTP client,
the request shape, the provider — stays with the caller. What lives here is the
order, the skipping, and what to do with the failures afterwards.

Failures are triaged only once a reply is in hand, or once every model has
failed. Answering is more urgent than bookkeeping: nobody waiting on a result
should wait on the housekeeping for a model that already failed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from .bench import Bench, MemoryBench
from .classify import bench_seconds_for, classify_failure


@dataclass
class Attempt:
    """One model tried, and how it went."""

    model: str
    error: str | None = None
    #: HTTP status, when the caller attached one to its error. Classification
    #: prefers it: a status is a fact where a message is a guess.
    status: int | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class Result:
    """What the walk produced."""

    value: Any = None
    model: str | None = None
    attempts: list[Attempt] = field(default_factory=list)
    #: Models skipped because they were already benched.
    skipped: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.model is not None

    def summary(self) -> str:
        failed = "; ".join(f"{a.model}: {a.error}" for a in self.attempts if not a.ok)
        return failed or "no model was tried"


class ChainExhausted(RuntimeError):
    """Every model in the chain failed or was benched."""

    def __init__(self, result: Result) -> None:
        self.result = result
        super().__init__(result.summary())


def run(
    models: Sequence[str],
    attempt: Callable[[str], tuple[Any, Any]],
    bench: Bench | None = None,
    on_bench: Callable[[str, str, Any, int | None], None] | None = None,
    raise_on_exhaustion: bool = False,
    fatal: tuple[type[BaseException], ...] = (),
) -> Result:
    """Try each model in turn until one succeeds.

    ``attempt(model)`` returns ``(value, error)``. A non-None error means that
    model failed; raising is treated the same way, so a caller need not wrap
    its own exceptions. If the error object carries a ``status`` attribute it
    is used to classify the failure, which is more reliable than reading the
    provider's prose.

    ``on_bench(model, kind, error, seconds)`` is called for each benched model
    after the walk, so an application can alert or log. A ``gone`` model — one
    that will not recover on its own — is the case worth telling someone about.

    ``fatal`` names exception types that end the walk immediately instead of
    counting as a failure. Some errors are about the caller's credentials
    rather than the model, and walking the whole chain to fail identically at
    every step wastes time and tells the user nothing.
    """
    bench = bench if bench is not None else MemoryBench()
    result = Result()

    for model in models:
        if not bench.usable(model):
            result.skipped.append(model)
            continue

        try:
            value, error = attempt(model)
        except fatal:
            _triage(result.attempts, bench, on_bench)
            raise
        except Exception as exc:  # noqa: BLE001 - the next model may work
            value, error = None, f"{type(exc).__name__}: {exc}"

        if error is None:
            result.value = value
            result.model = model
            result.attempts.append(Attempt(model))
            _triage(result.attempts, bench, on_bench)
            return result

        # An error object may carry its own status (any object with a
        # `.status` attribute); str() would throw that away.
        result.attempts.append(Attempt(model, str(error), getattr(error, "status", None)))

    _triage(result.attempts, bench, on_bench)
    if raise_on_exhaustion:
        raise ChainExhausted(result)
    return result


def _triage(
    attempts: Iterable[Attempt],
    bench: Bench,
    on_bench: Callable[[str, str, Any, int | None], None] | None,
) -> None:
    for item in attempts:
        if item.ok:
            continue
        kind = classify_failure(item.error, item.status)
        seconds = bench_seconds_for(item.error, kind)
        bench.bench(item.model, item.error, seconds)
        if on_bench is not None:
            on_bench(item.model, kind, item.error, seconds)
