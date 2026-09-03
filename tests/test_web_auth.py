"""Landing on the site in a browser must show a login form, not JSON."""

from fastapi.testclient import TestClient

from wayfare.web.app import app

HTML = {"accept": "text/html,application/xhtml+xml"}


def client(monkeypatch):
    monkeypatch.setenv("WAYFARE_OWNER_TOKEN", "owner-secret")
    monkeypatch.setenv("WAYFARE_AGENT_TOKEN", "agent-secret")
    import wayfare.config as config

    config._config = None
    return TestClient(app, raise_server_exceptions=False)


def test_browser_hitting_setup_gets_the_login_form(monkeypatch):
    response = client(monkeypatch).get("/setup", headers=HTML)
    assert response.status_code == 401
    assert "Owner token" in response.text
    assert "detail" not in response.text


def test_the_login_form_remembers_where_you_were_going(monkeypatch):
    response = client(monkeypatch).get("/setup", headers=HTML)
    assert 'name="next" value="/setup"' in response.text


def test_an_api_client_still_gets_json(monkeypatch):
    response = client(monkeypatch).get("/setup", headers={"accept": "application/json"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Bad or missing token."


def test_agent_token_in_a_browser_is_told_to_use_the_owner_token(monkeypatch):
    response = client(monkeypatch).get(
        "/setup", headers={**HTML, "Authorization": "Bearer agent-secret"}
    )
    assert response.status_code == 403
    assert "owner token" in response.text


def test_login_returns_to_the_requested_page(monkeypatch):
    response = client(monkeypatch).post(
        "/login", data={"token": "owner-secret", "next": "/setup"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"


def test_login_refuses_to_redirect_off_site(monkeypatch):
    """A 'next' pointing elsewhere would turn the login into an open redirect."""
    response = client(monkeypatch).post(
        "/login",
        data={"token": "owner-secret", "next": "//evil.example.com/"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/"


def test_a_wrong_token_says_so_and_keeps_the_destination(monkeypatch):
    response = client(monkeypatch).post("/login", data={"token": "nope", "next": "/setup"})
    assert response.status_code == 401
    assert "not accepted" in response.text
    assert 'value="/setup"' in response.text
