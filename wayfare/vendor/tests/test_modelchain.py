"""The three kinds of failure behave differently, which is the whole point."""

import json

import pytest

from modelchain import (
    Bench,
    CAP_DEFAULT_SECONDS,
    ChainExhausted,
    JsonFileBench,
    MemoryBench,
    TEMP_COOLDOWN_SECONDS,
    bench_reason,
    bench_seconds_for,
    classify_failure,
    free_models,
    run,
)
from modelchain import discover


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


# --- classification ------------------------------------------------------


@pytest.mark.parametrize(
    "error,expected",
    [
        ("429 Too Many Requests", "temporary"),
        ("upstream timed out", "temporary"),
        ("503 Service Unavailable", "temporary"),
        ("connection reset by peer", "temporary"),
        ("FreeUsageLimitError", "capped"),
        ("requires available credits", "capped"),
        ("404 no such model", "gone"),
        ("model has been deprecated", "gone"),
        ("401 unauthorized", "gone"),
        ("something nobody has seen before", "temporary"),
    ],
)
def test_failures_are_classified(error, expected):
    assert classify_failure(error) == expected


def test_an_unrecognised_failure_is_treated_as_temporary():
    """Disabling a channel on evidence we do not understand is worse."""
    assert classify_failure("☃") == "temporary"


def test_a_temporary_failure_gets_a_short_cooldown():
    assert bench_seconds_for("429", "temporary") == TEMP_COOLDOWN_SECONDS


def test_a_cap_wall_honours_the_provider_s_own_retry_hint():
    seconds = bench_seconds_for("limit reached, retrying in 2h 30m", "capped")
    assert seconds == 2 * 3600 + 30 * 60 + 600


def test_a_cap_wall_without_a_hint_waits_a_day():
    assert bench_seconds_for("quota exceeded", "capped") == CAP_DEFAULT_SECONDS


def test_a_gone_model_waits_for_a_human():
    assert bench_seconds_for("404 not found", "gone") is None


def test_a_raw_error_is_reduced_to_something_readable():
    assert bench_reason('{"error":{"code":503,"msg":"..."}}') == "provider outage (503)"


# --- the walk ------------------------------------------------------------


def test_the_first_working_model_wins():
    result = run(["a", "b"], lambda model: ("answer", None))
    assert result.ok and result.model == "a" and result.value == "answer"


def test_a_failure_falls_through_to_the_next():
    def attempt(model):
        return (None, "429") if model == "a" else ("answer", None)

    result = run(["a", "b"], attempt)
    assert result.model == "b"
    assert [a.model for a in result.attempts] == ["a", "b"]


def test_a_raised_exception_is_a_failure_not_a_crash():
    def attempt(model):
        if model == "a":
            raise TimeoutError("upstream gone")
        return "answer", None

    assert run(["a", "b"], attempt).model == "b"


def test_a_benched_model_is_skipped_not_retried():
    clock = Clock()
    bench = MemoryBench(clock)
    bench.bench("a", "429", TEMP_COOLDOWN_SECONDS)

    tried = []
    run(["a", "b"], lambda m: tried.append(m) or ("answer", None), bench=bench)
    assert tried == ["b"]


def test_a_cooldown_expires_on_its_own():
    clock = Clock()
    bench = MemoryBench(clock)
    bench.bench("a", "429", TEMP_COOLDOWN_SECONDS)
    assert not bench.usable("a")

    clock.advance(TEMP_COOLDOWN_SECONDS + 1)
    assert bench.usable("a")


def test_a_gone_model_does_not_come_back_by_itself():
    """'Disabled until a human looks' must not quietly un-disable itself."""
    clock = Clock()
    bench = MemoryBench(clock)
    bench.bench("a", "404 not found", None)
    clock.advance(10 * 365 * 24 * 3600)
    assert not bench.usable("a")
    assert bench.restore("a") is True
    assert bench.usable("a")


def test_exhausting_the_chain_reports_every_failure():
    result = run(["a", "b"], lambda model: (None, f"{model} broke"))
    assert not result.ok
    assert "a broke" in result.summary() and "b broke" in result.summary()


def test_exhaustion_can_raise_for_callers_that_prefer_it():
    with pytest.raises(ChainExhausted):
        run(["a"], lambda model: (None, "429"), raise_on_exhaustion=True)


def test_the_application_is_told_what_was_benched():
    seen = []
    run(
        ["a", "b"],
        lambda model: (None, "404 not found") if model == "a" else ("answer", None),
        on_bench=lambda model, kind, error, seconds: seen.append((model, kind, seconds)),
    )
    assert seen == [("a", "gone", None)]


def test_a_success_is_not_benched():
    bench = MemoryBench()
    run(["a"], lambda model: ("answer", None), bench=bench)
    assert bench.benched() == {}


# --- persistence ---------------------------------------------------------


def test_a_bench_survives_a_restart(tmp_path):
    path = tmp_path / "state.json"
    clock = Clock()
    JsonFileBench(path, clock).bench("a", "429", TEMP_COOLDOWN_SECONDS)
    assert not JsonFileBench(path, clock).usable("a")


def test_a_corrupt_state_file_is_not_fatal(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    assert JsonFileBench(path).usable("anything")


def test_the_state_file_is_written_atomically(tmp_path):
    path = tmp_path / "state.json"
    bench = JsonFileBench(path)
    bench.bench("a", "429", 60)
    assert json.loads(path.read_text())["a"]["why"] == "429"
    assert not list(tmp_path.glob("*.tmp"))


def test_the_report_reads_like_a_sentence():
    clock = Clock()
    bench = MemoryBench(clock)
    bench.bench("provider/model", "503 upstream", 600)
    (line,) = bench.report()
    assert "provider/model" in line and "provider outage (503)" in line


# --- discovery -----------------------------------------------------------


CATALOGUE = {
    "data": [
        {
            "id": "free/big:free",
            "pricing": {"prompt": "0", "completion": "0"},
            "context_length": 256000,
            "architecture": {"modality": "text->text", "output_modalities": ["text"]},
        },
        {
            "id": "free/small:free",
            "pricing": {"prompt": "0", "completion": "0"},
            "context_length": 8000,
            "architecture": {"modality": "text->text", "output_modalities": ["text"]},
        },
        {
            "id": "free/music",
            "pricing": {"prompt": "0", "completion": "0"},
            "context_length": 4000,
            "architecture": {"modality": "text->audio", "output_modalities": ["audio"]},
        },
        {
            "id": "paid/model",
            "pricing": {"prompt": "0.5", "completion": "1"},
            "context_length": 999999,
            "architecture": {"modality": "text->text", "output_modalities": ["text"]},
        },
    ]
}


def test_only_free_text_models_are_returned():
    discover.clear_cache()
    models = free_models("https://example.test/v1", fetch=lambda url, timeout: CATALOGUE)
    assert models == ["free/big:free", "free/small:free"]


def test_a_fetch_failure_returns_the_last_known_list():
    discover.clear_cache()
    clock = Clock()
    free_models("https://example.test/v1", fetch=lambda u, t: CATALOGUE, clock=clock)
    clock.advance(discover.CACHE_SECONDS + 1)

    def broken(url, timeout):
        raise OSError("network down")

    assert free_models("https://example.test/v1", fetch=broken, clock=clock) == [
        "free/big:free",
        "free/small:free",
    ]


def test_a_fetch_failure_with_no_history_is_empty_not_an_error():
    discover.clear_cache()

    def broken(url, timeout):
        raise OSError("network down")

    assert free_models("https://example.test/v1", fetch=broken) == []


def test_the_catalogue_is_not_refetched_every_call():
    discover.clear_cache()
    calls = []

    def counting(url, timeout):
        calls.append(url)
        return CATALOGUE

    for _ in range(3):
        free_models("https://example.test/v1", fetch=counting)
    assert len(calls) == 1


class BadKey(Exception):
    """A failure about the caller's credentials, not about the model."""


def test_a_fatal_error_stops_the_walk_at_once():
    """Every model would fail identically, so trying them proves nothing."""
    tried = []

    def attempt(model):
        tried.append(model)
        raise BadKey("401 unauthorized")

    with pytest.raises(BadKey):
        run(["a", "b", "c"], attempt, fatal=(BadKey,))
    assert tried == ["a"]


def test_a_fatal_error_is_not_swallowed_by_the_generic_handler():
    with pytest.raises(BadKey):
        run(["a"], lambda m: (_ for _ in ()).throw(BadKey("nope")), fatal=(BadKey,))


def test_without_fatal_the_same_error_is_just_a_failure():
    result = run(["a", "b"], lambda m: (_ for _ in ()).throw(BadKey("nope")))
    assert not result.ok
    assert len(result.attempts) == 2


def test_bench_reason_matches_the_substrings_it_documents():
    """Copied from a working implementation; must behave identically."""
    assert bench_reason("Rate limit exceeded") == "free-tier rate limit (429)"
    assert bench_reason("HTTP 429") == "free-tier rate limit (429)"
    assert bench_reason("FreeUsageLimitError") == "free-tier rate limit (429)"
    assert bench_reason("empty content") == "returned empty content"
    assert bench_reason("404 model gone") == "model withdrawn (404)"
    assert bench_reason("weird") == "weird"


# --- status-first classification -----------------------------------------


def test_a_status_outranks_the_message():
    """A 404 inside a request id must not bench a working model for ever."""
    assert classify_failure("429 rate limited (request req_404abc)") == "temporary"
    assert classify_failure("rate limited, id 404abc", status=429) == "temporary"


def test_cap_wording_still_promotes_a_429():
    """Providers return 429 both for 'slow down' and 'your window is spent'."""
    assert classify_failure("free usage exceeded", status=429) == "capped"
    assert classify_failure("slow down", status=429) == "temporary"


@pytest.mark.parametrize(
    "status,expected",
    [(401, "gone"), (404, "gone"), (402, "capped"), (403, "capped"),
     (429, "temporary"), (500, "temporary"), (503, "temporary"), (418, "gone")],
)
def test_statuses_map_to_kinds(status, expected):
    assert classify_failure("", status=status) == expected


# --- retry hints ---------------------------------------------------------


@pytest.mark.parametrize(
    "text,seconds",
    [
        ("retrying in 2h 30m", 9000),
        ("retry in 1 hour", 3600),
        ("retry in 15 minutes", 900),
        ("try again in 45s", 45),
        ("try again in 90 seconds", 90),
        ("no hint at all", None),
    ],
)
def test_retry_hints_are_read_in_every_unit(text, seconds):
    from modelchain.classify import retry_hint_seconds

    assert retry_hint_seconds(text) == seconds


def test_a_short_window_is_not_benched_for_a_day():
    """A fifteen-minute cap used to cost a full day of that channel."""
    assert bench_seconds_for("quota spent, retry in 15 minutes", "capped") == 900 + 600


# --- bench precedence and pruning ----------------------------------------


def test_a_longer_bench_is_not_shortened_by_a_later_blip():
    clock = Clock()
    bench = MemoryBench(clock)
    bench.bench("a", "quota", CAP_DEFAULT_SECONDS)
    bench.bench("a", "timeout", TEMP_COOLDOWN_SECONDS)

    clock.advance(TEMP_COOLDOWN_SECONDS + 1)
    assert not bench.usable("a"), "a day-long bench was overwritten by a 2-minute one"


def test_waiting_for_a_human_outranks_everything():
    clock = Clock()
    bench = MemoryBench(clock)
    bench.bench("a", "404 not found", None)
    bench.bench("a", "timeout", TEMP_COOLDOWN_SECONDS)

    clock.advance(10 * 24 * 3600)
    assert not bench.usable("a")


def test_long_expired_records_are_dropped():
    clock = Clock()
    bench = MemoryBench(clock)
    bench.bench("old", "timeout", TEMP_COOLDOWN_SECONDS)

    # Measured from when the bench expired, not from when it was set.
    clock.advance(TEMP_COOLDOWN_SECONDS + Bench.PRUNE_AFTER_SECONDS + 1)
    bench.bench("new", "timeout", TEMP_COOLDOWN_SECONDS)
    assert "old" not in bench._load()
    assert "new" in bench._load()


class StatusError(str):
    """An error that remembers its HTTP status, as a caller may supply."""

    def __new__(cls, status, text):
        error = super().__new__(cls, text)
        error.status = status
        return error


def test_a_status_on_the_error_reaches_classification():
    """Without this, a 429 body mentioning 404 benches the model for ever."""
    seen = []
    run(
        ["a"],
        lambda model: (None, StatusError(429, "rate limited, request req_404abc")),
        on_bench=lambda model, kind, error, seconds: seen.append(kind),
    )
    assert seen == ["temporary"]


def test_without_a_status_the_message_is_still_used():
    seen = []
    run(
        ["a"],
        lambda model: (None, "404 no such model"),
        on_bench=lambda model, kind, error, seconds: seen.append(kind),
    )
    assert seen == ["gone"]


def test_the_status_is_recorded_on_the_attempt():
    result = run(["a"], lambda model: (None, StatusError(503, "down")))
    assert result.attempts[0].status == 503
