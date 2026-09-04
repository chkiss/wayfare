"""Try a chain of models, and remember which ones are not worth trying.

Free and cheap model endpoints fail constantly and in different ways, and the
difference matters. A rate limit clears in a minute. A cap wall clears when the
provider's window rolls over. A withdrawn model never comes back on its own and
needs a person. Treating all three the same either hammers a struggling
endpoint or disables a working one for a day.

This library is the policy, not the plumbing. It never makes an HTTP request,
never writes a log, and never sends an alert — the calling application passes
in a function that attempts one model, and gets told what happened. That is
what lets a long-running assistant and a stateless web service share it.

Vendored into consumers with `git subtree`, so every consumer works standalone
with no install step. See README.md.
"""

from .bench import Bench, JsonFileBench, MemoryBench
from .chain import Attempt, ChainExhausted, Result, run
from .classify import (
    CAP_DEFAULT_SECONDS,
    TEMP_COOLDOWN_SECONDS,
    bench_reason,
    bench_seconds_for,
    classify_failure,
    retry_hint_seconds,
)
from .discover import free_models
from .providers import DEFAULTS, Providers

__all__ = [
    "Attempt",
    "Bench",
    "DEFAULTS",
    "Providers",
    "CAP_DEFAULT_SECONDS",
    "ChainExhausted",
    "JsonFileBench",
    "MemoryBench",
    "Result",
    "TEMP_COOLDOWN_SECONDS",
    "bench_reason",
    "bench_seconds_for",
    "retry_hint_seconds",
    "classify_failure",
    "free_models",
    "run",
]

__version__ = "0.1.0"
