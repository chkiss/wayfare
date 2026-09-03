"""Two permission modes, and the trade-off between them.

"full" uses the sensitive calendar scope: events reach your own calendar and
the duplicate check can see what is already on it. "app" uses
calendar.app.created, which Google does not class as sensitive — no unverified
warning — but the app may only touch calendars it made itself.
"""

import pytest

import wayfare.config as config
from wayfare.calendar_api import CalendarClient, scopes


@pytest.fixture(autouse=True)
def reset():
    config._config = None
    yield
    config._config = None


def test_full_mode_asks_for_the_calendar_scope(monkeypatch):
    monkeypatch.setenv("WAYFARE_SCOPE_MODE", "full")
    assert scopes() == ["https://www.googleapis.com/auth/calendar"]


def test_app_mode_asks_only_for_app_created_calendars(monkeypatch):
    monkeypatch.setenv("WAYFARE_SCOPE_MODE", "app")
    assert scopes() == ["https://www.googleapis.com/auth/calendar.app.created"]


def test_an_unknown_mode_falls_back_to_full_rather_than_breaking(monkeypatch):
    monkeypatch.setenv("WAYFARE_SCOPE_MODE", "nonsense")
    assert scopes() == ["https://www.googleapis.com/auth/calendar"]


def test_full_mode_promotes_to_the_configured_calendar(monkeypatch):
    monkeypatch.setenv("WAYFARE_SCOPE_MODE", "full")
    monkeypatch.setenv("WAYFARE_CALENDAR_ID", "primary")
    assert CalendarClient().target_calendar_id() == "primary"


def test_app_mode_promotes_to_a_calendar_it_created(monkeypatch):
    """It cannot write to "primary" at all, so it must own the destination."""
    monkeypatch.setenv("WAYFARE_SCOPE_MODE", "app")
    monkeypatch.setenv("WAYFARE_TARGET_CALENDAR", "Travel")

    client = CalendarClient()
    made = {}

    def fake_named(wanted, description):
        made["wanted"] = wanted
        return "app-made-calendar-id"

    client._calendar_named = fake_named
    assert client.target_calendar_id() == "app-made-calendar-id"
    assert made["wanted"] == "Travel"


def test_the_target_calendar_is_resolved_once(monkeypatch):
    monkeypatch.setenv("WAYFARE_SCOPE_MODE", "app")
    client = CalendarClient()
    calls = []
    client._calendar_named = lambda wanted, description: calls.append(wanted) or "id"
    client.target_calendar_id()
    client.target_calendar_id()
    assert len(calls) == 1
