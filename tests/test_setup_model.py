"""The model key is part of setup, and setup proves it works."""

import pytest
from fastapi.testclient import TestClient

import wayfare.config as config
from wayfare.extractors import llm
from wayfare.web.app import app

HTML = {"accept": "text/html"}


@pytest.fixture
def owner(monkeypatch, tmp_path):
    monkeypatch.setenv("WAYFARE_OWNER_TOKEN", "owner-secret")
    monkeypatch.setenv("WAYFARE_AGENT_TOKEN", "agent-secret")
    monkeypatch.setenv("WAYFARE_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.delenv("WAYFARE_LLM_MODEL", raising=False)
    monkeypatch.delenv("WAYFARE_LLM_API_KEY", raising=False)
    config._config = None
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set("wayfare_token", "owner-secret")
    return client


def test_the_key_is_saved_with_owner_only_permissions(owner, monkeypatch):
    monkeypatch.setattr(llm, "verify", lambda: (True, "ok"))
    owner.post("/setup/model", data={"api_key": "test-key-0000", "model": ""})
    path = config.get_config().secrets_dir / "llm_api_key"
    assert path.read_text().strip() == "test-key-0000"
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_the_chosen_model_is_remembered(owner, monkeypatch):
    monkeypatch.setattr(llm, "verify", lambda: (True, "ok"))
    owner.post("/setup/model", data={"api_key": "k", "model": "some/other-model:free"})
    config._config = None
    assert config.get_config().llm_model == "some/other-model:free"


def test_a_broken_key_is_reported_rather_than_called_set_up(owner, monkeypatch):
    monkeypatch.setattr(llm, "verify", lambda: (False, "The provider rejected that key (401)."))
    owner.post("/setup/model", data={"api_key": "bad", "model": ""})
    page = owner.get("/setup", headers=HTML).text
    assert "rejected that key" in page
    assert "not working" in page


def test_the_test_result_survives_a_reload(owner, monkeypatch):
    """A flash message is gone the moment the page is refreshed."""
    monkeypatch.setattr(llm, "verify", lambda: (True, "gemma answered: 'ok'"))
    owner.post("/setup/model", data={"api_key": "k", "model": ""})
    for _ in range(2):
        page = owner.get("/setup", headers=HTML).text
        assert "gemma answered" in page
        assert "working" in page


def test_a_busy_free_tier_is_not_a_broken_key(owner, monkeypatch):
    """429 means the provider answered, so the key was accepted."""
    import httpx

    class Busy:
        status_code = 429

    monkeypatch.setattr(httpx, "post", lambda *a, **k: Busy())
    monkeypatch.setattr(llm, "free_models", lambda cfg=None: ["a:free", "b:free"])
    llm.save_api_key("k")
    ok, detail = llm.verify()
    assert ok is True
    assert "rate-limited" in detail
    assert "fallback" in detail


def test_a_result_for_another_model_is_not_shown_as_this_one(owner, monkeypatch):
    monkeypatch.setattr(llm, "verify", lambda: (True, "old model answered"))
    owner.post("/setup/model", data={"api_key": "k", "model": "first/model"})
    owner.post("/setup/model", data={"api_key": "", "model": "second/model"})
    config._config = None
    monkeypatch.setenv("WAYFARE_LLM_MODEL", "third/model")
    config._config = None
    assert llm.status()["check"] is None


def test_the_saved_key_is_never_echoed_back(owner, monkeypatch):
    monkeypatch.setattr(llm, "verify", lambda: (True, "ok"))
    owner.post("/setup/model", data={"api_key": "test-key-supersecret", "model": ""})
    page = owner.get("/setup", headers=HTML).text
    assert "supersecret" not in page
    assert "cret" in page  # only the last four characters, as a hint


def test_an_agent_token_cannot_set_the_key(monkeypatch, tmp_path):
    monkeypatch.setenv("WAYFARE_OWNER_TOKEN", "owner-secret")
    monkeypatch.setenv("WAYFARE_AGENT_TOKEN", "agent-secret")
    monkeypatch.setenv("WAYFARE_SECRETS_DIR", str(tmp_path / "secrets"))
    config._config = None
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/setup/model",
        data={"api_key": "k"},
        headers={"Authorization": "Bearer agent-secret"},
    )
    assert response.status_code == 403


def test_rubbish_is_refused_before_it_is_stored(owner):
    with pytest.raises(llm.LLMUnavailable):
        llm.save_api_key("a\nb")
    with pytest.raises(llm.LLMUnavailable):
        llm.save_api_key("   ")
