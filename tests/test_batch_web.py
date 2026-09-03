"""The upload form and the ingest API both take a whole trip at once."""

import pytest
from fastapi.testclient import TestClient

import wayfare.config as config
import wayfare.store as store
import wayfare.web.app as web
from wayfare.web.app import app

HTML = {"accept": "text/html"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("WAYFARE_OWNER_TOKEN", "owner-secret")
    monkeypatch.setenv("WAYFARE_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv("WAYFARE_STATE_DIR", str(tmp_path / "state"))
    config._config = None
    cfg = config.get_config()
    cfg.secrets_dir.mkdir(parents=True, exist_ok=True)
    cfg.oauth_token.write_text("{}")

    # Never touch a real calendar from a test.
    monkeypatch.setattr(store, "commit", _record_commit)
    _record_commit.calls.clear()

    c = TestClient(app, raise_server_exceptions=False)
    c.cookies.set("wayfare_token", "owner-secret")
    return c


class _Submission:
    submission_id = "test"

    def to_dict(self):
        return {
            "submission_id": "test",
            "source_file": _record_commit.calls[-1][1],
            "created": "now",
            "records": [],
            "itinerary_issues": [],
            "summary": {"promoted": 0, "pending": 0, "rejected": 0},
        }


def _record_commit(itinerary, source_file, **kwargs):
    _record_commit.calls.append((itinerary, source_file))
    return _Submission()


_record_commit.calls = []


def upload(name, body=b"nothing readable here"):
    return ("upload", (name, body, "text/plain"))


def test_several_files_become_one_submission(client):
    response = client.post(
        "/submit",
        files=[upload("outbound.txt"), upload("return.txt"), upload("hotel.txt")],
        headers=HTML,
    )
    assert response.status_code == 200
    assert len(_record_commit.calls) == 1
    assert "3 documents" in _record_commit.calls[-1][1]


def test_files_and_pasted_text_arrive_together(client):
    """They are not alternatives — a trip often comes as both."""
    client.post(
        "/submit",
        files=[upload("boarding-pass.txt")],
        data={"text": "Hotel Example, 4-8 March"},
        headers=HTML,
    )
    assert "2 documents" in _record_commit.calls[-1][1]


def test_a_single_file_is_still_named_after_itself(client):
    client.post("/submit", files=[upload("outbound.txt")], headers=HTML)
    assert _record_commit.calls[-1][1] == "outbound.txt"


def test_an_empty_submission_is_refused(client):
    response = client.post("/submit", data={"text": "   "}, headers=HTML)
    assert response.status_code == 400


def test_the_api_takes_a_batch_too(client):
    response = client.post(
        "/api/v1/ingest",
        files=[upload("outbound.txt"), upload("return.txt")],
        headers={"authorization": "Bearer owner-secret"},
    )
    assert response.status_code == 200
    assert "2 documents" in _record_commit.calls[-1][1]


def test_one_oversized_file_does_not_take_the_batch_down_silently(client, monkeypatch):
    monkeypatch.setattr(web, "MAX_UPLOAD_BYTES", 10)
    response = client.post(
        "/submit", files=[upload("huge.txt", b"x" * 50)], headers=HTML
    )
    assert response.status_code == 413
    assert "huge.txt" in response.text


# --- navigation ----------------------------------------------------------


def test_the_home_page_has_a_way_back_to_setup(client):
    body = client.get("/", headers=HTML).text
    assert 'href="/setup"' in body
    assert "⚙" in body


def test_the_upload_field_accepts_more_than_one_file(client):
    assert "multiple" in client.get("/", headers=HTML).text


def test_finishing_setup_offers_the_way_back(client, monkeypatch):
    # A dict, exactly as calendar_api.connection_status returns. Faking it as
    # an object is what let a live AttributeError through a green suite.
    monkeypatch.setattr(
        web,
        "connection_status",
        lambda: {"connected": True, "account": None, "client_uploaded": True},
    )
    body = client.get("/setup?done=1", headers=HTML).text
    assert "showModal" in body
    assert "Back to wayfare" in body


def test_done_is_harmless_against_the_real_connection_status(client):
    """No monkeypatch on purpose.

    Faking connection_status as an object let a live 500 through a green
    suite: the real function returns a dict, and `status.connected` works in
    a Jinja template but not in Python.
    """
    assert client.get("/setup?done=1", headers=HTML).status_code == 200


def test_returning_to_setup_later_does_not_pop_a_dialog(client, monkeypatch):
    # A dict, exactly as calendar_api.connection_status returns. Faking it as
    # an object is what let a live AttributeError through a green suite.
    monkeypatch.setattr(
        web,
        "connection_status",
        lambda: {"connected": True, "account": None, "client_uploaded": True},
    )
    assert "showModal" not in client.get("/setup", headers=HTML).text
