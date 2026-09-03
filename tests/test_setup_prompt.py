"""Anyone who has not finished setup is sent to the setup page."""

import pytest
from fastapi.testclient import TestClient

import wayfare.config as config
from wayfare.web.app import app

HTML = {"accept": "text/html"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("WAYFARE_OWNER_TOKEN", "owner-secret")
    monkeypatch.setenv("WAYFARE_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv("WAYFARE_STATE_DIR", str(tmp_path / "state"))
    config._config = None
    c = TestClient(app, raise_server_exceptions=False)
    c.cookies.set("wayfare_token", "owner-secret")
    return c


def connect(monkeypatch):
    """Pretend the Google consent flow has been completed."""
    cfg = config.get_config()
    cfg.secrets_dir.mkdir(parents=True, exist_ok=True)
    cfg.oauth_token.write_text("{}")


def test_an_unconfigured_instance_sends_you_to_setup(client):
    response = client.get("/", headers=HTML, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"


def test_once_connected_the_home_page_appears(client, monkeypatch):
    connect(monkeypatch)
    response = client.get("/", headers=HTML)
    assert response.status_code == 200
    assert "Read it" in response.text


def test_a_missing_model_is_a_banner_not_a_redirect(client, monkeypatch):
    """Barcodes and text PDFs still work without a model, so do not block."""
    connect(monkeypatch)
    response = client.get("/", headers=HTML)
    assert response.status_code == 200
    assert "No model backend" in response.text
    assert "Finish setup" in response.text


def test_the_banner_goes_away_once_a_key_is_saved(client, monkeypatch):
    connect(monkeypatch)
    monkeypatch.setenv("WAYFARE_LLM_API_KEY", "sk-test")
    config._config = None
    response = client.get("/", headers=HTML)
    assert "No model backend" not in response.text


def test_setup_names_the_missing_server_packages(client, monkeypatch):
    monkeypatch.setattr("wayfare.web.app.ocr_available", lambda: False)
    response = client.get("/setup", headers=HTML)
    assert "tesseract is missing" in response.text
    assert "sudo apt install" in response.text
