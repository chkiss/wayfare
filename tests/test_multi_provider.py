"""Reading with more than one endpoint.

A whole day's free budget was spent on one provider — every model answering
"rate limited", the document reported unreadable — while a second free
endpoint on the same machine answered in under a second. wayfare had only ever
been told about one.
"""

import pytest

import wayfare.config as config
from wayfare.extractors import llm


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("WAYFARE_SECRETS_DIR", str(tmp_path / "secrets"))
    config._config = None
    yield
    config._config = None


class Reply:
    status_code = 200

    def json(self):
        return {"choices": [{"message": {"content": '{"records": []}'}}]}


def sent(monkeypatch):
    """Capture the request each model would produce."""
    seen = []

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.append({"url": url, "headers": headers or {}, "model": (json or {}).get("model")})
        return Reply()

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    return seen


# --- naming -------------------------------------------------------------


def test_a_bare_model_goes_to_the_configured_endpoint(monkeypatch):
    monkeypatch.setenv("WAYFARE_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    config._config = None
    seen = sent(monkeypatch)

    llm._attempt("google/gemma:free", "text", config.get_config())

    assert seen[0]["url"].startswith("https://openrouter.ai/api/v1")
    assert seen[0]["model"] == "google/gemma:free"


def test_a_prefixed_model_goes_to_its_own_endpoint(monkeypatch):
    seen = sent(monkeypatch)
    llm._attempt("zen:big-pickle", "text", config.get_config())

    assert seen[0]["url"].startswith("https://opencode.ai/zen/v1")
    # The prefix is addressing, not part of the model's name.
    assert seen[0]["model"] == "big-pickle"


def test_a_provider_that_needs_no_key_is_not_sent_one(monkeypatch):
    """Zen's free tier is keyless; an empty Bearer header is worse than none."""
    seen = sent(monkeypatch)
    llm._attempt("zen:big-pickle", "text", config.get_config())
    assert "Authorization" not in seen[0]["headers"]


def test_each_provider_uses_its_own_key(monkeypatch):
    cfg = config.get_config()
    cfg.secrets_dir.mkdir(parents=True, exist_ok=True)
    (cfg.secrets_dir / "llm_api_key.nous").write_text("nous-secret", encoding="utf-8")
    monkeypatch.setenv("WAYFARE_LLM_API_KEY", "openrouter-secret")
    config._config = None
    seen = sent(monkeypatch)

    llm._attempt("nous:tencent/hy3:free", "text", config.get_config())
    llm._attempt("google/gemma:free", "text", config.get_config())

    assert seen[0]["headers"]["Authorization"] == "Bearer nous-secret"
    assert seen[1]["headers"]["Authorization"] == "Bearer openrouter-secret"


def test_a_provider_that_demands_tags_gets_them(monkeypatch):
    """Nous rejects an untagged request outright."""
    seen = sent(monkeypatch)
    llm._attempt("nous:tencent/hy3:free", "text", config.get_config())
    assert "X-Tags" in seen[0]["headers"]


# --- building the pool --------------------------------------------------


def test_free_models_are_gathered_from_every_enabled_endpoint(monkeypatch):
    monkeypatch.setenv("WAYFARE_LLM_PROVIDERS", "zen")
    config._config = None
    monkeypatch.setattr(
        llm.modelchain,
        "free_models",
        lambda base: ["a:free", "b:free"] if "openrouter" in base else ["big-pickle"],
    )

    models = llm.free_models()

    assert "zen:big-pickle" in models
    assert "a:free" in models


def test_the_endpoints_are_interleaved_not_exhausted_in_turn(monkeypatch):
    """One provider's whole catalogue first would spend the chain inside one
    outage — which is exactly the day this was built for."""
    monkeypatch.setenv("WAYFARE_LLM_PROVIDERS", "zen")
    config._config = None
    monkeypatch.setattr(
        llm.modelchain,
        "free_models",
        lambda base: ["a:free", "b:free"] if "openrouter" in base else ["big-pickle", "glm-5"],
    )

    assert llm.free_models() == ["a:free", "zen:big-pickle", "b:free", "zen:glm-5"]


def test_an_endpoint_that_is_down_costs_nothing(monkeypatch):
    """Discovery is a convenience and must never be why a call does not happen."""
    monkeypatch.setenv("WAYFARE_LLM_PROVIDERS", "zen")
    config._config = None

    def half_broken(base):
        if "openrouter" in base:
            raise OSError("connection refused")
        return ["big-pickle"]

    monkeypatch.setattr(llm.modelchain, "free_models", half_broken)
    assert llm.free_models() == ["zen:big-pickle"]


def test_an_endpoint_is_used_only_when_it_is_named(monkeypatch):
    """modelchain knows Nous's address; that is not permission to use it."""
    monkeypatch.setenv("WAYFARE_LLM_PROVIDERS", "zen")
    config._config = None
    asked = []

    def note(base):
        asked.append(base)
        return []

    monkeypatch.setattr(llm.modelchain, "free_models", note)
    llm.free_models()

    assert not any("nousresearch" in base for base in asked)


def test_the_default_endpoint_is_asked_first(monkeypatch):
    monkeypatch.setenv("WAYFARE_LLM_PROVIDERS", "zen")
    config._config = None
    assert config.get_config().enabled_providers[0] == "openrouter"
