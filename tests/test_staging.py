"""Files are uploaded while the batch is still being assembled."""

import time

import pytest
from fastapi.testclient import TestClient

import wayfare.config as config
import wayfare.store as store
from wayfare import staging
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

    monkeypatch.setattr(store, "commit", _record_commit)
    _record_commit.calls.clear()

    c = TestClient(app, raise_server_exceptions=False)
    c.cookies.set("wayfare_token", "owner-secret")
    return c


class _Submission:
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


def stage(client, name, body=b"nothing readable"):
    return client.post("/uploads", files={"upload": (name, body, "text/plain")})


# --- staging -------------------------------------------------------------


def test_a_file_can_be_uploaded_before_the_batch_is_submitted(client):
    response = stage(client, "outbound.pdf")
    assert response.status_code == 200
    assert response.json()["name"] == "outbound.pdf"
    assert client.cookies.get("wayfare_batch")


def test_staged_files_are_read_on_submit(client):
    first = stage(client, "outbound.pdf").json()["id"]
    second = stage(client, "hotel.eml").json()["id"]

    response = client.post("/submit", data={"staged": [first, second]}, headers=HTML)
    assert response.status_code == 200
    assert _record_commit.calls[-1][1] == "2 documents (outbound.pdf, hotel.eml)"


def test_a_removed_file_does_not_reach_the_submission(client):
    first = stage(client, "outbound.pdf").json()["id"]
    second = stage(client, "wrong-trip.pdf").json()["id"]

    assert client.delete(f"/uploads/{second}").status_code == 200
    client.post("/submit", data={"staged": [first, second]}, headers=HTML)
    assert _record_commit.calls[-1][1] == "outbound.pdf"


def test_submitting_clears_the_batch(client):
    staged = stage(client, "outbound.pdf").json()["id"]
    session = client.cookies.get("wayfare_batch")
    client.post("/submit", data={"staged": [staged]}, headers=HTML)
    assert staging.get(session, staged) is None


def test_an_agent_token_cannot_stage_a_file(client):
    client.cookies.clear()
    response = client.post(
        "/uploads",
        files={"upload": ("x.pdf", b"x", "text/plain")},
        headers={"authorization": "Bearer not-the-owner"},
    )
    assert response.status_code in (401, 403)


# --- the staging area itself ---------------------------------------------


def test_a_filename_cannot_escape_the_staging_directory(client):
    item = stage(client, "../../../etc/passwd").json()
    assert "/" not in item["name"] and ".." not in item["name"]


def test_a_forged_session_cookie_is_refused(client):
    with pytest.raises(ValueError):
        staging.add("../../etc", "x.pdf", b"x")


def test_abandoned_batches_are_swept(client, monkeypatch):
    session = staging.new_session()
    staging.add(session, "forgotten.pdf", b"x")
    assert staging.get(session, staging.new_session()) is None  # unrelated id

    later = time.time() + staging.EXPIRE_AFTER_SECONDS + 60
    assert staging.sweep(now=later) == 1


def test_a_fresh_batch_is_not_swept(client):
    session = staging.new_session()
    staged = staging.add(session, "current.pdf", b"x")
    assert staging.sweep() == 0
    assert staging.get(session, staged.file_id) is not None


def test_the_form_still_works_without_the_script(client):
    """No staged ids, just an ordinary multipart post."""
    response = client.post(
        "/submit", files=[("upload", ("outbound.pdf", b"x", "text/plain"))], headers=HTML
    )
    assert response.status_code == 200
    assert _record_commit.calls[-1][1] == "outbound.pdf"
