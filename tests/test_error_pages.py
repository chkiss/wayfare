"""A browser gets a page; a client gets JSON."""

import pytest
from fastapi.testclient import TestClient

import wayfare.config as config
import wayfare.store as store
from wayfare.web.app import app

HTML = {"accept": "text/html"}
JSON = {"accept": "application/json"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("WAYFARE_OWNER_TOKEN", "owner-secret")
    monkeypatch.setenv("WAYFARE_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv("WAYFARE_STATE_DIR", str(tmp_path / "state"))
    config._config = None
    cfg = config.get_config()
    cfg.secrets_dir.mkdir(parents=True, exist_ok=True)
    cfg.oauth_token.write_text("{}")

    monkeypatch.setattr(store, "commit", lambda *a, **k: _Submission())
    c = TestClient(app, raise_server_exceptions=False)
    c.cookies.set("wayfare_token", "owner-secret")
    return c


class _Submission:
    def to_dict(self):
        return {
            "submission_id": "t",
            "source_file": "t",
            "created": "now",
            "records": [],
            "itinerary_issues": [],
            "summary": {"promoted": 0, "pending": 0, "rejected": 0},
        }


def test_an_empty_submission_shows_a_page_not_a_json_blob(client):
    """This is what came back after going back and resubmitting."""
    response = client.post("/submit", data={"text": "  "}, headers=HTML)
    assert response.status_code == 400
    assert '{"detail"' not in response.text
    assert "text/html" in response.headers["content-type"]
    assert "Back to wayfare" in response.text


def test_the_page_explains_the_case_that_actually_causes_it(client):
    response = client.post("/submit", data={"text": ""}, headers=HTML)
    assert "already read and cleared" in response.text


def test_an_api_client_still_gets_json(client):
    response = client.post(
        "/api/v1/ingest",
        data={"text": ""},
        headers={"authorization": "Bearer owner-secret", **JSON},
    )
    assert response.status_code == 400
    assert response.json()["detail"]


def test_a_missing_page_is_still_a_page(client):
    response = client.get("/no-such-thing", headers=HTML)
    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]


# --- coming back to a submitted page ------------------------------------


def test_the_page_can_ask_what_is_still_staged(client):
    staged = client.post(
        "/uploads", files={"upload": ("outbound.pdf", b"x", "text/plain")}
    ).json()["id"]

    assert client.get("/uploads", headers=JSON).json()["ids"] == [staged]


def test_nothing_is_staged_after_a_submission(client):
    staged = client.post(
        "/uploads", files={"upload": ("outbound.pdf", b"x", "text/plain")}
    ).json()["id"]
    client.post("/submit", data={"staged": [staged]}, headers=HTML)

    # The page asks this on restore, sees its file is gone, and re-uploads it.
    assert client.get("/uploads", headers=JSON).json()["ids"] == []


def test_the_button_says_what_it_does(client):
    body = client.get("/", headers=HTML).text
    assert "Generate iCals" in body
    assert "Read it" not in body
