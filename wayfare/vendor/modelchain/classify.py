"""Deciding what a failure means, and how long to stay away.

Three kinds, because three different things happen next:

``temporary``  overload, timeouts, plain rate limits. A short cooldown so one
               conversation does not hammer a struggling endpoint. Comes back
               on its own; not worth telling anyone about.
``capped``     the free tier's usage window is spent. Self-heals when the
               window rolls over, so benching it "until a human looks" would
               leave a channel dead all day for no reason. If the provider
               says how long, believe it.
``gone``       the model is withdrawn, or the key is refused. Nothing about
               waiting will fix it, so it is benched until a person decides.

Unknown errors are ``temporary`` on purpose: disabling a channel on evidence
we do not understand is worse than retrying it.
"""

from __future__ import annotations

import re

_TEMPORARY_FAILURE_RE = re.compile(
    r"timed? ?out|overload|temporar|bad gateway|\b50[234]\b|too many requests|"
    r"rate.?limit|connection (reset|refused|error)|proxy",
    re.I,
)

# CamelCase matters: providers emit FreeUsageLimitError, not three plain words.
_CAP_WALL_RE = re.compile(
    r"freeusage|free usage exceeded|usage.?limit|requires available credits|"
    r"add credits|insufficient|quota|payment",
    re.I,
)

# A retry hint in the error is the provider telling us exactly how long to stay
# away. Honour it rather than guessing. Providers phrase this several ways, and
# an unparsed hint used to mean a fifteen-minute window became a day-long bench.
_CAP_RETRY_HOURS_RE = re.compile(
    r"(?:retry|retrying|try again)\D{0,12}?(\d+)\s*h(?:ours?|rs?)?"
    r"(?:\D{0,3}(\d+)\s*m(?!s))?",
    re.I,
)
_CAP_RETRY_MINUTES_RE = re.compile(
    r"(?:retry|retrying|try again)\D{0,12}?(\d+)\s*(?:m(?:in(?:ute)?s?)?)\b", re.I
)
_CAP_RETRY_SECONDS_RE = re.compile(
    r"(?:retry|retrying|try again)\D{0,12}?(\d+)\s*(?:s(?:ec(?:ond)?s?)?)\b", re.I
)

#: A window that has just rolled over is still being hammered by everyone else
#: who was waiting for it, so wait a little past the hint.
RETRY_HINT_MARGIN_SECONDS = 600

#: Status codes are unambiguous where a message is not. Prefer them.
STATUS_KIND = {
    401: "gone",       # the key, not the model
    404: "gone",       # withdrawn
    402: "capped",     # needs credit
    403: "capped",     # policy or access, per model; may clear
    408: "temporary",
    409: "temporary",
    425: "temporary",
    429: "temporary",  # cap wording can still promote this to "capped"
}

# Word-bounded on purpose. Unbounded "404" matched a request id, a token
# count, or a URL, and the consequence of a false "gone" is the worst one
# available: a working model benched until a person notices.
_REVIEW_FAILURE_RE = re.compile(
    r"\b404\b|not found|no such model|does not exist|deprecat|decommission|"
    r"unauthorized|forbidden|invalid.{0,20}key",
    re.I,
)

#: One conversation should not keep hitting an endpoint that just failed.
TEMP_COOLDOWN_SECONDS = 120
#: How long a spent free-tier window is assumed to last, absent a hint.
CAP_DEFAULT_SECONDS = 86400


def classify_failure(error, status: int | None = None) -> str:
    """``"temporary"``, ``"capped"`` or ``"gone"``.

    When the HTTP status is known it decides, because a message is guesswork
    and a status is not: an error body mentioning "not found" or carrying a
    request id with 404 in it should not bench a working model indefinitely.
    Cap wording can still promote a 429 to ``capped``, since providers return
    429 both for "slow down" and for "your free window is spent".
    """
    text = str(error or "")

    if status is not None:
        kind = STATUS_KIND.get(status)
        if kind is None:
            kind = "temporary" if status >= 500 else "gone" if status >= 400 else "temporary"
        if kind == "temporary" and _CAP_WALL_RE.search(text):
            return "capped"
        return kind

    if _REVIEW_FAILURE_RE.search(text):
        return "gone"
    if _CAP_WALL_RE.search(text):
        return "capped"
    return "temporary"


def bench_seconds_for(error, kind: str) -> int | None:
    """How long to bench a model. ``None`` means until a human clears it."""
    if kind == "temporary":
        return TEMP_COOLDOWN_SECONDS
    if kind != "capped":
        return None

    hinted = retry_hint_seconds(error)
    if hinted is not None:
        return min(hinted + RETRY_HINT_MARGIN_SECONDS, CAP_DEFAULT_SECONDS)
    return CAP_DEFAULT_SECONDS


def retry_hint_seconds(error) -> int | None:
    """How long the provider itself asked us to wait, if it said."""
    text = str(error or "")

    match = _CAP_RETRY_HOURS_RE.search(text)
    if match:
        return int(match.group(1)) * 3600 + int(match.group(2) or 0) * 60

    match = _CAP_RETRY_MINUTES_RE.search(text)
    if match:
        return int(match.group(1)) * 60

    match = _CAP_RETRY_SECONDS_RE.search(text)
    if match:
        return int(match.group(1))
    return None


def bench_reason(why) -> str:
    """A raw provider error reduced to something a person can read."""
    text = str(why or "")
    if "503" in text:
        return "provider outage (503)"
    if "429" in text or "FreeUsageLimit" in text or "Rate limit" in text:
        return "free-tier rate limit (429)"
    if "empty content" in text:
        return "returned empty content"
    if "404" in text or "not supported" in text:
        return "model withdrawn (404)"
    return text[:50] + ("…" if len(text) > 50 else "")
