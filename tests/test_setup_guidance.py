"""The setup page has to be followable by someone who has not done this before."""

import pytest
from fastapi.testclient import TestClient

import wayfare.config as config
from wayfare.web.app import app

HTML = {"accept": "text/html"}
REDIRECT = "https://wayfare.example.com/oauth/callback"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("WAYFARE_OWNER_TOKEN", "owner-secret")
    monkeypatch.setenv("WAYFARE_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv("WAYFARE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("WAYFARE_BASE_URL", "https://wayfare.example.com")
    config._config = None
    c = TestClient(app, raise_server_exceptions=False)
    c.cookies.set("wayfare_token", "owner-secret")
    return c


def test_the_redirect_uri_is_shown_before_it_is_needed(client):
    page = client.get("/setup", headers=HTML).text
    assert REDIRECT in page


def test_every_pasteable_value_has_its_own_copy_button(client):
    """One button per value beats one button that copies a blob of three."""
    import re

    page = client.get("/setup", headers=HTML).text
    blocks = re.findall(
        r'<span class="copyable">\s*<code>([^<]+)</code>\s*'
        r'<button class="copy" type="button" onclick="wfCopy\(this\)">copy</button>',
        page,
    )
    values = [b.strip() for b in blocks]
    assert REDIRECT in values
    assert "https://wayfare.example.com/about" in values
    assert "https://wayfare.example.com/privacy" in values
    assert "https://wayfare.example.com/terms" in values
    assert "https://www.googleapis.com/auth/calendar" in values


def test_the_redirect_uri_is_still_available_once_connected(client, monkeypatch):
    """It is needed again for any second client, and is impossible to guess."""
    monkeypatch.setattr(
        "wayfare.web.app.connection_status",
        lambda: {"connected": True, "account": "me@example.com", "client_uploaded": True,
                 "client_kind": "web", "error": None},
    )
    assert REDIRECT in client.get("/setup", headers=HTML).text


def test_the_choices_that_break_the_flow_are_named(client):
    """User data, not Application data. Web application, not Desktop."""
    page = client.get("/setup", headers=HTML).text
    assert "User data" in page
    assert "Web application" in page


def test_the_scope_is_given_because_consent_fails_without_it(client):
    page = client.get("/setup", headers=HTML).text
    assert "https://www.googleapis.com/auth/calendar" in page
    assert "Data access" in page


def test_the_app_domain_links_are_offered_ready_to_paste(client):
    page = client.get("/setup", headers=HTML).text
    assert "https://wayfare.example.com/about" in page
    assert "https://wayfare.example.com/privacy" in page
    assert "https://wayfare.example.com/terms" in page


def test_publishing_is_one_instruction(client):
    page = client.get("/setup", headers=HTML).text
    assert "Publish app" in page


def test_upload_lives_in_the_step_it_belongs_to(client):
    """The 'uploaded' badge and the file input are the same card."""
    page = client.get("/setup", headers=HTML).text
    step_one = page.index("Step 1 — Create the OAuth client")
    step_two = page.index("Step 2 — Branding")
    assert step_one < page.index('name="client_json"') < step_two


def test_only_verifiable_steps_carry_a_status_pill(client):
    """A 'to do' pill on 'enable the API' is a claim the app cannot check."""
    page = client.get("/setup", headers=HTML).text
    for orphan in ("Step 1 — Turn on", "Step 2 — Create the OAuth"):
        assert orphan not in page


def test_the_steps_are_in_the_order_they_must_be_done(client):
    page = client.get("/setup", headers=HTML).text
    order = [
        "Step 1 — Create the OAuth client",
        "Step 2 — Branding",
        "Step 3 — Data access",
        "Step 4 — Publish",
        "Step 5 — Grant access",
    ]
    positions = [page.index(step) for step in order]
    assert positions == sorted(positions)


def test_a_desktop_client_upload_is_called_out_on_the_page(client, monkeypatch):
    monkeypatch.setattr(
        "wayfare.web.app.connection_status",
        lambda: {"connected": False, "account": None, "client_uploaded": True,
                 "client_kind": "installed", "error": None},
    )
    assert "Desktop client" in client.get("/setup", headers=HTML).text


def test_the_public_pages_need_no_token(client):
    """Google has to be able to fetch them, so they cannot sit behind login."""
    anonymous = TestClient(app, raise_server_exceptions=False)
    for path in ("/about", "/privacy", "/terms"):
        assert anonymous.get(path, headers=HTML).status_code == 200, path


def test_the_privacy_page_states_what_google_data_is_used_for(client):
    page = client.get("/privacy", headers=HTML).text
    assert "Google Calendar API" in page
    assert "myaccount.google.com/permissions" in page


def test_console_links_are_pinned_to_the_client_project(client, monkeypatch):
    """The console opens whichever project you last used, not necessarily this one."""
    monkeypatch.setattr(
        "wayfare.web.app.connection_status",
        lambda: {"connected": False, "account": None, "client_uploaded": True,
                 "client_kind": "web", "project_id": "example-project-123", "error": None},
    )
    page = client.get("/setup", headers=HTML).text
    assert "auth/branding?project=example-project-123" in page
    assert "auth/scopes?project=example-project-123" in page
    assert "auth/audience?project=example-project-123" in page
    assert "example-project-123" in page


def test_no_project_query_before_a_client_is_uploaded(client):
    page = client.get("/setup", headers=HTML).text
    assert "?project=" not in page
