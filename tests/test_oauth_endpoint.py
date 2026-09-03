"""The consent flow must run against Google's current endpoint."""

import json

import pytest

import wayfare.config as config
from wayfare.calendar_api import AUTH_URI, authorisation_url, build_web_flow

CLIENT = {
    "web": {
        "client_id": "test.apps.googleusercontent.com",
        "project_id": "example-project",
        "client_secret": "not-a-real-secret",
        # Exactly what Google puts in a downloaded client file.
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["https://wayfare.example.com/oauth/callback"],
    }
}


@pytest.fixture
def client_file(monkeypatch, tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "client_secret.json").write_text(json.dumps(CLIENT))
    monkeypatch.setenv("WAYFARE_SECRETS_DIR", str(secrets))
    monkeypatch.setenv("WAYFARE_SCOPE_MODE", "full")
    config._config = None
    yield
    config._config = None


def test_the_legacy_endpoint_in_the_client_file_is_overridden(client_file):
    flow = build_web_flow("https://wayfare.example.com/oauth/callback")
    assert flow.client_config["auth_uri"] == AUTH_URI


def build(state="state123"):
    return authorisation_url("https://wayfare.example.com/oauth/callback", state)


def test_the_authorisation_url_uses_the_v2_endpoint(client_file):
    url, _ = build()
    assert url.startswith(AUTH_URI)


def test_offline_access_is_requested_so_the_token_survives(client_file):
    url, _ = build()
    assert "access_type=offline" in url
    assert "prompt=consent" in url


def test_incremental_authorisation_is_not_requested(client_file):
    """One scope, nothing to add to, one less thing to go wrong."""
    url, _ = build()
    assert "include_granted_scopes" not in url


def test_the_state_is_carried_through(client_file):
    url, _ = build()
    assert "state=state123" in url


def test_the_pkce_verifier_is_handed_back_to_the_caller(client_file):
    """Consent succeeds and the exchange then fails without it."""
    url, verifier = build()
    assert verifier, "no code verifier returned"
    assert "code_challenge=" in url


def test_two_attempts_get_different_verifiers(client_file):
    _, first = build()
    _, second = build()
    assert first != second
