"""Saying what is happening during the wait, instead of "Sending request".

The upload finishes while the user is still choosing files. Everything after
the button is OCR and free models, and a browser left to itself describes that
wait with the name of the one part that is already over.
"""

import time

import pytest
from fastapi.testclient import TestClient

import wayfare.config as config
import wayfare.progress as progress
import wayfare.store as store
import wayfare.web.app as web
from wayfare.web.app import app


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("WAYFARE_OWNER_TOKEN", "owner-secret")
    monkeypatch.setenv("WAYFARE_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv("WAYFARE_STATE_DIR", str(tmp_path / "state"))
    config._config = None
    cfg = config.get_config()
    cfg.secrets_dir.mkdir(parents=True, exist_ok=True)
    cfg.oauth_token.write_text("{}")

    c = TestClient(app, raise_server_exceptions=False)
    c.cookies.set("wayfare_token", "owner-secret")
    return c


def wait_for(job_id, client, predicate, limit=5.0):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        payload = client.get(f"/progress/{job_id}").json()
        if predicate(payload):
            return payload
        time.sleep(0.02)
    raise AssertionError(f"job never satisfied the condition: {payload}")


# --- the reporter itself ------------------------------------------------


def test_reporting_without_a_job_is_silent():
    """The CLI and the tests run the same code with nothing attached."""
    progress.report("this goes nowhere")  # must not raise


def test_a_bound_job_collects_the_phases():
    job = progress.start(total=2)
    token = progress.bind(job)
    try:
        progress.report("Reading the text of return.pdf")
        progress.report("Asking 3 models to read it")
    finally:
        progress.unbind(token)

    assert job.phase == "Asking 3 models to read it"
    assert job.history == ["Reading the text of return.pdf", "Asking 3 models to read it"]


def test_a_repeated_phase_is_not_recorded_twice():
    job = progress.start()
    token = progress.bind(job)
    try:
        progress.report("Checking")
        progress.report("Checking")
    finally:
        progress.unbind(token)
    assert job.history == ["Checking"]


def test_finished_jobs_are_eventually_dropped(monkeypatch):
    job = progress.start()
    progress.finish(job, "abc123")
    job.finished_at = time.monotonic() - progress.KEEP_SECONDS - 1
    progress.start()  # any new job sweeps
    assert progress.get(job.id) is None


# --- through the web ----------------------------------------------------


def test_a_background_submission_reports_its_phases_then_its_result(client, monkeypatch):
    def slow_read(path, name):
        from wayfare import progress as p
        from wayfare.schema import Itinerary

        p.report(f"Reading the text of {name}")
        time.sleep(0.05)
        p.report("Asking 3 models to read it")
        return Itinerary()

    monkeypatch.setattr(web, "_read_one", slow_read)
    monkeypatch.setattr(web, "_recheck_with_calendar", lambda it: it)
    monkeypatch.setattr(
        store, "commit", lambda *a, **k: type("S", (), {"submission_id": "sub-1"})()
    )

    response = client.post(
        "/submit",
        data={"background": "1"},
        files={"upload": ("return.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    assert response.status_code == 200
    job_id = response.json()["job"]

    done = wait_for(job_id, client, lambda j: j["done"])
    assert done["submission_id"] == "sub-1"
    assert "Asking 3 models to read it" in done["history"]


def test_a_failure_reaches_the_page_instead_of_spinning(client, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("tesseract fell over")

    monkeypatch.setattr(web, "_run_submission", explode)

    job_id = client.post("/submit", data={"background": "1", "text": "x"}).json()["job"]
    done = wait_for(job_id, client, lambda j: j["done"])
    assert "tesseract fell over" in done["error"]
    assert done["submission_id"] is None


def test_an_empty_background_submission_is_refused_immediately(client):
    response = client.post("/submit", data={"background": "1"})
    assert response.status_code == 400


def test_progress_is_owner_only(client):
    client.cookies.clear()
    assert client.get("/progress/anything").status_code in (401, 403)


def test_an_unknown_job_is_a_404(client):
    assert client.get("/progress/deadbeef").status_code == 404


def test_the_working_panel_is_hidden_until_something_is_running(client):
    """A display rule silently beats the `hidden` attribute, and did.

    The panel sat on the home page saying "Working" with nothing running,
    because `.working { display: flex }` outranks `[hidden]`.
    """
    body = client.get("/", headers={"accept": "text/html"}).text
    assert 'id="working"' in body and "hidden" in body
    # The rule that makes the attribute mean what it says.
    assert "[hidden] { display: none !important; }" in body


# --- a batch the server no longer holds ---------------------------------


def test_a_staged_file_the_server_lost_is_never_passed_over(client, monkeypatch):
    """The measured failure: one upload silently dropped, only the text read.

    A submission completed and cleared the batch while the browser had lost
    contact. The user pressed the button again, the ids no longer existed, and
    the document was left out of the submission with nothing on screen to say
    so.
    """
    monkeypatch.setattr(web, "_read_one", lambda path, name: None)

    response = client.post(
        "/submit",
        data={"background": "1", "staged": ["deadbeefdeadbeef"], "text": "a booking"},
    )
    assert response.status_code == 409
    assert response.json()["missing"] == ["deadbeefdeadbeef"]


def test_a_batch_the_server_still_holds_is_submitted(client, monkeypatch):
    """The recovery must not fire when there is nothing wrong."""
    from wayfare import staging
    from wayfare.schema import Itinerary

    session = "sessionsessions1"
    file_id = staging.add(session, "ticket.pdf", b"%PDF-1.4 fake").file_id
    client.cookies.set("wayfare_batch", session)

    monkeypatch.setattr(web, "_read_one", lambda path, name: Itinerary())
    monkeypatch.setattr(web, "_recheck_with_calendar", lambda it: it)
    monkeypatch.setattr(
        store, "commit", lambda *a, **k: type("S", (), {"submission_id": "sub-2"})()
    )

    response = client.post("/submit", data={"background": "1", "staged": [file_id]})
    assert response.status_code == 200
    done = wait_for(response.json()["job"], client, lambda j: j["done"])
    assert done["submission_id"] == "sub-2"
