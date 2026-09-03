"""A busy free model must not be the difference between reading a booking and not."""

import pytest

import wayfare.config as config
from wayfare.extractors import llm


class Reply:
    def __init__(self, status, content=None):
        self.status_code = status
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


@pytest.fixture(autouse=True)
def configured(monkeypatch, tmp_path):
    monkeypatch.setenv("WAYFARE_SECRETS_DIR", str(tmp_path))
    monkeypatch.setenv("WAYFARE_LLM_API_KEY", "k")
    monkeypatch.setenv("WAYFARE_LLM_MODEL", "primary:free")
    config._config = None
    monkeypatch.setattr(llm, "free_models", lambda cfg=None: ["spare-one:free", "spare-two:free"])
    llm._free_cache = None
    yield
    config._config = None


def test_the_configured_model_is_tried_first(monkeypatch):
    tried = []

    def fake_post(model, text, cfg):
        tried.append(model)
        return Reply(200, '{"records": []}')

    monkeypatch.setattr(llm, "_post", fake_post)
    llm._call_model("some text", config.get_config())
    assert tried == ["primary:free"]


def test_a_rate_limited_model_falls_through_to_the_next(monkeypatch):
    tried = []

    def fake_post(model, text, cfg):
        tried.append(model)
        return Reply(429) if model == "primary:free" else Reply(200, '{"records": []}')

    monkeypatch.setattr(llm, "_post", fake_post)
    result = llm._call_model("some text", config.get_config())
    assert tried == ["primary:free", "spare-one:free"]
    assert result == {"records": []}


def test_a_network_failure_also_falls_through(monkeypatch):
    def fake_post(model, text, cfg):
        if model == "primary:free":
            raise TimeoutError("upstream gone")
        return Reply(200, '{"records": []}')

    monkeypatch.setattr(llm, "_post", fake_post)
    assert llm._call_model("some text", config.get_config()) == {"records": []}


def test_a_bad_key_is_not_retried_against_every_model(monkeypatch):
    """401 is about the key, not the model; trying more is pointless."""
    tried = []

    def fake_post(model, text, cfg):
        tried.append(model)
        return Reply(401)

    monkeypatch.setattr(llm, "_post", fake_post)
    with pytest.raises(llm.LLMUnavailable):
        llm._call_model("some text", config.get_config())
    assert tried == ["primary:free"]


def test_exhausting_every_model_says_so_plainly(monkeypatch):
    monkeypatch.setattr(llm, "_post", lambda model, text, cfg: Reply(429))
    with pytest.raises(llm.LLMUnavailable, match="No model could be reached"):
        llm._call_model("some text", config.get_config())


def test_the_fallback_count_is_configurable(monkeypatch):
    monkeypatch.setenv("WAYFARE_LLM_FALLBACKS", "1")
    config._config = None
    tried = []
    monkeypatch.setattr(llm, "_post", lambda model, text, cfg: tried.append(model) or Reply(429))
    with pytest.raises(llm.LLMUnavailable):
        llm._call_model("some text", config.get_config())
    assert tried == ["primary:free", "spare-one:free"]


def test_discovery_is_delegated_to_the_shared_library(monkeypatch):
    """Filtering free models is modelchain's job, and is tested there."""
    from wayfare.vendor import modelchain

    monkeypatch.undo()  # The fixture stubs llm.free_models; this test wants the real one.
    monkeypatch.setattr(modelchain, "free_models", lambda base_url: ["from/library:free"])
    assert llm.free_models() == ["from/library:free"]


def test_a_rate_limit_is_classified_on_its_status_not_its_body(monkeypatch):
    """Provider bodies carry request ids; one containing 404 must not stick."""
    from wayfare.vendor import modelchain

    class Response:
        status_code = 429
        text = "rate limited upstream (request req_404abc)"

    monkeypatch.setattr(llm, "_post", lambda model, text, cfg: Response())
    value, error = llm._attempt("primary:free", "text", config.get_config())
    assert value is None
    assert modelchain.classify_failure(error, getattr(error, "status", None)) == "temporary"


def test_a_spent_free_window_is_capped_not_merely_slowed(monkeypatch):
    from wayfare.vendor import modelchain

    class Response:
        status_code = 429
        text = "free usage exceeded, retry in 15 minutes"

    monkeypatch.setattr(llm, "_post", lambda model, text, cfg: Response())
    _, error = llm._attempt("primary:free", "text", config.get_config())
    kind = modelchain.classify_failure(error, getattr(error, "status", None))
    assert kind == "capped"
    assert modelchain.bench_seconds_for(error, kind) == 900 + 600
