"""Google Calendar access, with the guardrails built in rather than bolted on.

Two rules shape this module:

* **Nothing lands on the real calendar unvalidated.** Every event is created on
  a separate "Travel (pending)" calendar first. Clean events are then *moved*
  to the real one, which keeps a single event identity throughout — no
  create-then-delete, no duplicates if a step fails halfway.
* **Every write is reversible.** Each insert and each move is appended to an
  undo log with its event id, so `wayfare undo` can put things back without
  needing to guess what the tool did.

Credentials live outside the repository (see `config.py`) and never appear in
logs or API responses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config, get_config

#: The scope for each mode. See Config.scope_mode for the trade-off.
SCOPES_BY_MODE = {
    "full": ["https://www.googleapis.com/auth/calendar"],
    "app": ["https://www.googleapis.com/auth/calendar.app.created"],
}


def scopes(cfg: Config | None = None) -> list[str]:
    cfg = cfg or get_config()
    return SCOPES_BY_MODE.get(cfg.scope_mode, SCOPES_BY_MODE["full"])


#: Kept for callers that predate the mode switch.
SCOPES = SCOPES_BY_MODE["full"]


class CalendarError(RuntimeError):
    pass


class NotAuthorised(CalendarError):
    """Raised when no usable stored credentials exist."""


@dataclass
class WriteResult:
    event_id: str
    calendar_id: str
    html_link: str | None
    summary: str
    promoted: bool


def _load_credentials(cfg: Config):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not cfg.oauth_token.exists():
        raise NotAuthorised(
            f"No stored Google credentials at {cfg.oauth_token}. Run `wayfare auth`."
        )
    credentials = Credentials.from_authorized_user_file(str(cfg.oauth_token), scopes(cfg))
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        _save_credentials(cfg, credentials)
    if not credentials.valid:
        raise NotAuthorised("Stored Google credentials are not valid. Run `wayfare auth`.")
    return credentials


def _save_credentials(cfg: Config, credentials) -> None:
    cfg.secrets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    cfg.oauth_token.write_text(credentials.to_json(), encoding="utf-8")
    cfg.oauth_token.chmod(0o600)


def authorise(cfg: Config | None = None) -> Path:
    """Run the one-time OAuth consent flow and store the refresh token.

    Uses the console flow so it works over SSH on a headless server.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    cfg = cfg or get_config()
    if not cfg.oauth_client_secret.exists():
        raise CalendarError(
            f"OAuth client secret not found at {cfg.oauth_client_secret}.\n"
            "Create a Desktop-app OAuth client in Google Cloud Console, enable the "
            "Google Calendar API, and save the downloaded JSON there."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(cfg.oauth_client_secret), scopes(cfg))
    credentials = flow.run_local_server(port=0, open_browser=False)
    _save_credentials(cfg, credentials)
    return cfg.oauth_token


# --- browser setup flow --------------------------------------------------
#
# The CLI flow above needs a terminal. The functions below drive the same
# consent from the microsite, so connecting a calendar is a matter of clicking
# through pages rather than downloading a JSON file and knowing where to put
# it.


def client_secret_kind(path: Path) -> str | None:
    """Which OAuth client type a downloaded JSON file describes."""
    section = _client_section(path)
    return section[0] if section else None


def _client_section(path: Path):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for kind in ("web", "installed"):
        if kind in payload and payload[kind].get("client_id"):
            return kind, payload[kind]
    return None


def client_project_id(path: Path) -> str | None:
    """The Cloud project the OAuth client belongs to.

    Worth surfacing: the console remembers whichever project you last opened,
    so consent-screen settings are easy to edit against a different project
    from the one holding the client. The resulting failure says nothing about
    projects at all.
    """
    section = _client_section(path)
    if not section:
        return None
    return section[1].get("project_id") or None


def save_client_secret(payload: bytes, cfg: Config | None = None) -> str:
    """Store an uploaded OAuth client JSON, refusing anything that is not one.

    Returns the client type. Raises CalendarError with a message written for
    someone who has just downloaded the wrong file from the Cloud Console.
    """
    cfg = cfg or get_config()
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CalendarError("That file is not JSON. Upload the file Google gave you.") from exc

    kind = next((k for k in ("web", "installed") if k in parsed), None)
    if kind is None or not parsed[kind].get("client_id"):
        if "type" in parsed and parsed.get("type") == "service_account":
            raise CalendarError(
                "That is a service-account key. A service account has no access to your "
                "personal calendar; create an OAuth client ID instead."
            )
        raise CalendarError(
            "That JSON does not contain an OAuth client. Download the file from the "
            "credentials page using the download icon next to your OAuth 2.0 Client ID."
        )

    cfg.secrets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    cfg.oauth_client_secret.write_bytes(payload)
    cfg.oauth_client_secret.chmod(0o600)
    return kind


#: Google's current authorization endpoint. Downloaded client JSON still names
#: the legacy `/o/oauth2/auth`, and the OAuth library uses whatever the file
#: says, so consent runs against an endpoint a decade older than the consent
#: screen it has to render.
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"


def build_web_flow(redirect_uri: str, cfg: Config | None = None):
    """A consent flow that returns the user to the microsite when they finish."""
    from google_auth_oauthlib.flow import Flow

    cfg = cfg or get_config()
    if not cfg.oauth_client_secret.exists():
        raise CalendarError("No OAuth client has been uploaded yet.")

    try:
        client_config = json.loads(cfg.oauth_client_secret.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CalendarError("The stored OAuth client file could not be read.") from exc

    for kind in ("web", "installed"):
        if kind in client_config:
            client_config[kind]["auth_uri"] = AUTH_URI
            break

    flow = Flow.from_client_config(client_config, scopes(cfg))
    flow.redirect_uri = redirect_uri
    return flow


def authorisation_url(
    redirect_uri: str, state: str, cfg: Config | None = None
) -> tuple[str, str | None]:
    """Where to send the browser to grant access, plus the PKCE verifier.

    The verifier is generated here and has to be presented again when the code
    comes back. The callback runs in a different request with a different flow
    object, so the caller must carry it across; losing it fails the exchange
    with "Missing code verifier" *after* the user has already consented.
    """
    flow = build_web_flow(redirect_uri, cfg)
    # No include_granted_scopes: this app asks for one scope and has nothing to
    # add to, and incremental authorisation is one more thing to go wrong.
    url, _ = flow.authorization_url(
        access_type="offline",
        # Without this, a second run returns no refresh token and the
        # connection silently dies an hour later.
        prompt="consent",
        state=state,
    )
    return url, getattr(flow, "code_verifier", None)


def complete_web_flow(
    redirect_uri: str,
    authorisation_response: str,
    code_verifier: str | None = None,
    cfg: Config | None = None,
) -> Path:
    """Exchange the code Google sent back for a stored refresh token."""
    cfg = cfg or get_config()
    flow = build_web_flow(redirect_uri, cfg)
    if code_verifier:
        flow.code_verifier = code_verifier
    flow.fetch_token(authorization_response=authorisation_response)
    _save_credentials(cfg, flow.credentials)
    return cfg.oauth_token


def connection_status(cfg: Config | None = None) -> dict:
    """What the setup page needs to know, with no secret in the result."""
    cfg = cfg or get_config()
    status = {
        "client_uploaded": cfg.oauth_client_secret.exists(),
        "client_kind": None,
        "project_id": None,
        "connected": False,
        "account": None,
        "error": None,
    }
    if status["client_uploaded"]:
        status["client_kind"] = client_secret_kind(cfg.oauth_client_secret)
        status["project_id"] = client_project_id(cfg.oauth_client_secret)
    if not cfg.oauth_token.exists():
        return status

    try:
        client = CalendarClient(cfg)
        profile = client.service.calendars().get(calendarId="primary").execute()
        status["connected"] = True
        status["account"] = profile.get("id")
    except Exception as exc:  # noqa: BLE001 - shown to the operator as text
        status["error"] = str(exc)
    return status


class CalendarClient:
    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or get_config()
        self._service = None
        self._pending_id: str | None = None
        self._target_id: str | None = None

    @property
    def service(self):
        if self._service is None:
            from googleapiclient.discovery import build

            self._service = build(
                "calendar", "v3", credentials=_load_credentials(self.cfg), cache_discovery=False
            )
        return self._service

    # --- calendars -------------------------------------------------------
    def target_calendar_id(self) -> str:
        """Where fully-checked events belong.

        In "full" mode that is the configured calendar, normally your primary
        one. In "app" mode the app may only touch calendars it created, so
        promoted events go to an app-owned calendar instead — visible
        alongside your own, but not part of it.
        """
        if self.cfg.scope_mode == "app":
            if self._target_id is None:
                self._target_id = self._calendar_named(
                    self.cfg.target_calendar_name,
                    "Travel events created by wayfare that passed every check.",
                )
            return self._target_id
        return self.cfg.calendar_id

    def pending_calendar_id(self) -> str:
        """Id of the quarantine calendar, creating it on first use."""
        if self._pending_id is None:
            self._pending_id = self._calendar_named(
                self.cfg.pending_calendar_name,
                "Quarantine for travel events created by wayfare. Events here have "
                "not passed every validation check; review them before trusting them.",
            )
        return self._pending_id

    def _calendar_named(self, wanted: str, description: str) -> str:
        """Find a calendar by name, creating it if this instance has none."""
        page_token = None
        while True:
            response = self.service.calendarList().list(pageToken=page_token).execute()
            for entry in response.get("items", []):
                if entry.get("summary") == wanted:
                    return entry["id"]
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        created = (
            self.service.calendars()
            .insert(body={"summary": wanted, "description": description})
            .execute()
        )
        return created["id"]

    # --- reading ---------------------------------------------------------
    def events_around(self, start: datetime, end: datetime, calendar_id: str | None = None) -> list[dict]:
        """Existing events in a window, flattened for the duplicate check."""
        target = calendar_id or self.target_calendar_id()
        response = (
            self.service.events()
            .list(
                calendarId=target,
                timeMin=_rfc3339(start),
                timeMax=_rfc3339(end),
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
            )
            .execute()
        )
        out = []
        for event in response.get("items", []):
            start_field = event.get("start", {})
            stamp = start_field.get("dateTime") or start_field.get("date") or ""
            out.append(
                {
                    "id": event.get("id"),
                    "summary": event.get("summary", ""),
                    "start_date": stamp[:10],
                    "start": stamp,
                }
            )
        return out

    def context_window(self, records) -> list[dict]:
        """Existing events spanning the dates an itinerary touches, plus a margin."""
        from .render import end_local, start_local

        stamps: list[datetime] = []
        for record in records:
            for when in (start_local(record), end_local(record)):
                if when is not None:
                    stamps.append(when.local)
        if not stamps:
            return []
        return self.events_around(
            min(stamps) - timedelta(days=2), max(stamps) + timedelta(days=2)
        )

    # --- writing ---------------------------------------------------------
    def create(self, body: dict, calendar_id: str, colour_id: str | None = None) -> dict:
        payload = dict(body)
        if colour_id:
            payload["colorId"] = colour_id
        event = self.service.events().insert(calendarId=calendar_id, body=payload).execute()
        self._log(
            {
                "action": "create",
                "calendar_id": calendar_id,
                "event_id": event["id"],
                "summary": payload.get("summary", ""),
            }
        )
        return event

    def move(self, event_id: str, source_calendar: str, destination_calendar: str) -> dict:
        event = (
            self.service.events()
            .move(calendarId=source_calendar, eventId=event_id, destination=destination_calendar)
            .execute()
        )
        self._log(
            {
                "action": "move",
                "event_id": event_id,
                "from": source_calendar,
                "to": destination_calendar,
                "summary": event.get("summary", ""),
            }
        )
        return event

    def get_event(self, event_id: str, calendar_id: str) -> dict:
        return self.service.events().get(calendarId=calendar_id, eventId=event_id).execute()

    def patch(self, event_id: str, calendar_id: str, changes: dict) -> dict:
        """Change an event in place, keeping its id so the undo log stays valid."""
        event = (
            self.service.events()
            .patch(calendarId=calendar_id, eventId=event_id, body=changes)
            .execute()
        )
        self._log(
            {
                "action": "patch",
                "calendar_id": calendar_id,
                "event_id": event_id,
                "summary": event.get("summary", ""),
                "fields": sorted(changes),
            }
        )
        return event

    def delete(self, event_id: str, calendar_id: str) -> None:
        self.service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        self._log({"action": "delete", "calendar_id": calendar_id, "event_id": event_id})

    # --- undo log --------------------------------------------------------
    def _log(self, entry: dict) -> None:
        self.cfg.ensure_dirs()
        entry = dict(entry)
        entry["timestamp"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.cfg.undo_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    def undo_log(self) -> list[dict]:
        if not self.cfg.undo_log.exists():
            return []
        entries = []
        for line in self.cfg.undo_log.read_text(encoding="utf-8").splitlines():
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
        return entries

    def undo_last(self, count: int = 1) -> list[str]:
        """Reverse the most recent writes. Moves go back; creates are deleted."""
        undone: list[str] = []
        for entry in reversed(self.undo_log()):
            if len(undone) >= count:
                break
            if entry.get("action") == "undone":
                continue
            try:
                if entry["action"] == "create":
                    self.delete(entry["event_id"], entry["calendar_id"])
                    undone.append(f"deleted '{entry.get('summary', '')}'")
                elif entry["action"] == "move":
                    self.move(entry["event_id"], entry["to"], entry["from"])
                    undone.append(f"moved '{entry.get('summary', '')}' back to pending")
                else:
                    continue
            except Exception as exc:  # noqa: BLE001 - report, do not abort the rest
                undone.append(f"could not undo {entry.get('event_id')}: {exc}")
            self._log({"action": "undone", "event_id": entry.get("event_id")})
        return undone


def _rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat(timespec="seconds")
