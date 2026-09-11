"""The off switch has to stop the request, not merely discourage it.

`WAYFARE_DISABLE_LLM` guards a free tier capped at fifty requests a day. It
was read in exactly one place, `available()`, and the default quorum of one
took a branch that never called it — so the switch was off on the only
configuration anyone runs. A benchmark announcing "deterministic extractors
only" spent the day's budget instead.

These tests reach for the network the way the pipeline does and assert that
nothing gets there, rather than asserting that a flag is set.
"""

import pytest

import wayfare.config as config
from wayfare.extractors import llm


@pytest.fixture(autouse=True)
def configured(monkeypatch, tmp_path):
    monkeypatch.setenv("WAYFARE_SECRETS_DIR", str(tmp_path))
    monkeypatch.setenv("WAYFARE_LLM_API_KEY", "k")
    config._config = None
    yield
    config._config = None


@pytest.fixture
def calls(monkeypatch):
    """Record requests rather than raising on them.

    Raising here proves nothing: `_attempt` catches whatever a request throws
    and reports it as a failed model, so a test that raises inside `httpx.post`
    still sees `LLMUnavailable` and passes against the unfixed code. What has
    to be asserted is that the call never happened.
    """
    made: list[str] = []

    def record(url, *args, **kwargs):
        made.append(url)

        class Reply:
            status_code = 503
            text = ""

            @staticmethod
            def json():
                return {}

        return Reply()

    monkeypatch.setattr(llm.httpx, "post", record)
    return made


def test_extract_refuses_when_disabled(monkeypatch, calls):
    monkeypatch.setenv("WAYFARE_DISABLE_LLM", "1")
    config._config = None

    with pytest.raises(llm.LLMUnavailable):
        llm.extract("a boarding pass", "pass.txt")
    assert calls == []


def test_read_with_refuses_when_disabled(monkeypatch, calls):
    """The named-model path is a separate door to the same network."""
    monkeypatch.setenv("WAYFARE_DISABLE_LLM", "1")
    config._config = None

    with pytest.raises(llm.LLMUnavailable):
        llm.read_with("some-model:free", "a boarding pass", "pass.txt")
    assert calls == []


def test_pipeline_reads_nothing_from_a_model_when_disabled(monkeypatch, calls, tmp_path):
    """The bug in full: quorum 1 skipped `available()` and called the model anyway."""
    monkeypatch.setenv("WAYFARE_DISABLE_LLM", "1")
    monkeypatch.setenv("WAYFARE_LLM_QUORUM", "1")
    config._config = None
    assert config.get_config().llm_quorum == 1

    from wayfare import pipeline

    document = tmp_path / "ticket.txt"
    document.write_text("Zug ICE 1196 von Mainz nach Kassel", encoding="utf-8")

    # No exception: a document that no deterministic extractor understands is
    # an empty reading, not a crash. The point is that it got there without
    # a request.
    pipeline.process_file(document, document.name)
    assert calls == []


def test_missing_key_still_refuses(monkeypatch, calls):
    monkeypatch.delenv("WAYFARE_DISABLE_LLM", raising=False)
    monkeypatch.setenv("WAYFARE_LLM_API_KEY", "")
    config._config = None

    with pytest.raises(llm.LLMUnavailable):
        llm.extract("a boarding pass", "pass.txt")
