"""HTTP surface.

Two audiences, two privilege levels, one deliberate asymmetry:

* **The owner** (browser, or the owner token) can do everything: submit,
  review, promote a held record, discard, undo.
* **An agent** (the agent token) can do exactly one thing: submit a document
  and read back what happened to it. It cannot promote, cannot discard, cannot
  delete, and cannot enumerate the calendar.

That asymmetry is the point. The agent token is the credential most likely to
leak — it sits in an environment variable on some other machine — so it is
scoped so that leaking it costs the owner nothing worse than junk events
sitting in a quarantine calendar.
"""

from __future__ import annotations

import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import batch, staging, store
from ..calendar_api import (
    CalendarClient,
    CalendarError,
    NotAuthorised,
    authorisation_url,
    complete_web_flow,
    connection_status,
    save_client_secret,
    scopes,
)
from ..config import get_config
from ..extractors import barcode as barcode_extractor
from ..extractors import llm as llm_extractor
from ..ocr import available as ocr_available
from ..pipeline import process_file, process_text
from ..schema import IssueLevel, Itinerary

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

#: Refuse anything larger than this. Booking documents are small; a big upload
#: is either a mistake or an attempt to exhaust the box.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

app = FastAPI(title="wayfare", docs_url=None, redoc_url=None)

Scope = Literal["owner", "agent"]

#: Headings for the browser-facing error page. The status code is a fact about
#: HTTP; these say what happened in the terms the person was working in.
HTTP_HEADINGS = {
    400: "That submission had nothing in it",
    404: "Not found",
    413: "That file is too big",
    503: "Not set up yet",
}

HTTP_SUGGESTIONS = {
    400: "If you came back to this page after submitting, the files were already "
    "read and cleared. Add them again and they will upload as you do.",
    413: "Booking documents are small. Twenty megabytes is the limit per file.",
    503: "Finish connecting a calendar on the setup page, then try again.",
}


@app.exception_handler(StarletteHTTPException)
def _http_exception(request: Request, exc: StarletteHTTPException):
    """Show a browser the login form; give an API client its JSON.

    Landing on /setup with no cookie is the normal way to arrive at this site,
    and answering that with {"detail": "Bad or missing token."} is useless to
    a person holding a phone. The distinction is what the client asked for,
    not which endpoint was hit.
    """
    wants_html = "text/html" in request.headers.get("accept", "")

    if exc.status_code == 401 and wants_html and request.method == "GET":
        response = TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {"error": None, "next": request.url.path},
            status_code=401,
        )
        return response

    if exc.status_code == 403 and wants_html:
        return TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {
                "error": "That token cannot do this. Sign in with the owner token.",
                "next": request.url.path,
            },
            status_code=403,
        )

    # Anything else a browser asked for gets a page, not the API's error body.
    # {"detail": "Send at least one file, or some text."} is a correct answer
    # to the wrong question: the person is holding a phone, not a client.
    if wants_html:
        return TEMPLATES.TemplateResponse(
            request,
            "error.html",
            {
                "heading": HTTP_HEADINGS.get(exc.status_code, "Something went wrong"),
                "detail": exc.detail,
                "suggestion": HTTP_SUGGESTIONS.get(exc.status_code),
            },
            status_code=exc.status_code,
        )

    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(NotAuthorised)
def _not_authorised(request: Request, exc: NotAuthorised):
    """Missing Google credentials is an operator problem, not a server fault.

    Reported as 503 so an agent's error table tells it to stop and ask a human
    rather than retrying a request that cannot succeed.
    """
    return JSONResponse(
        status_code=503,
        content={"detail": f"Calendar access is not set up: {exc}"},
    )


def _authenticate(request: Request) -> Scope:
    """Resolve the caller's privilege level, or refuse."""
    cfg = get_config()
    owner_token, agent_token = cfg.owner_token, cfg.agent_token

    if not owner_token and not agent_token:
        raise HTTPException(
            status_code=503,
            detail="No tokens configured. Run deploy/setup.sh to generate them.",
        )

    presented = None
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        presented = header[7:].strip()
    if presented is None:
        presented = request.cookies.get("wayfare_token")

    if presented:
        if owner_token and secrets.compare_digest(presented, owner_token):
            return "owner"
        if agent_token and secrets.compare_digest(presented, agent_token):
            return "agent"

    raise HTTPException(status_code=401, detail="Bad or missing token.")


def require_any(request: Request) -> Scope:
    return _authenticate(request)


def require_owner(request: Request) -> Scope:
    scope = _authenticate(request)
    if scope != "owner":
        raise HTTPException(
            status_code=403,
            detail="This token may only submit documents to the pending calendar.",
        )
    return scope


# --- browser -------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    cfg = get_config()
    token = request.cookies.get("wayfare_token")
    authorised = bool(token and cfg.owner_token and secrets.compare_digest(token, cfg.owner_token))
    if not authorised:
        return TEMPLATES.TemplateResponse(request, "login.html", {"error": None, "next": "/"})

    # Without a calendar connection the tool cannot do the one thing it exists
    # to do, so there is nothing useful to show here yet. Send them to setup
    # rather than to an upload form whose every submission would fail.
    #
    # The check is the cheap one — does a stored token exist — because this
    # runs on every page load. /setup does the real verification.
    if not cfg.oauth_token.exists():
        return RedirectResponse("/setup", status_code=303)

    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "submissions": store.recent(15),
            "calendar_id": cfg.calendar_id,
            "pending_name": cfg.pending_calendar_name,
            "auto_promote": cfg.auto_promote,
            "threshold": cfg.promote_threshold,
            # Not fatal: barcodes and PDFs with a text layer still work
            # without a model. Worth saying once, not worth blocking on.
            "needs_model": not llm_extractor.available(),
            "ocr_ready": ocr_available(),
        },
    )


# --- public pages --------------------------------------------------------
#
# Unauthenticated on purpose. Google requires an app's OAuth branding to carry
# a home page, a privacy policy and a terms link, all on the same domain as the
# redirect URI, and refuses to let the app leave testing mode without them. A
# login wall on those URLs defeats the point, so these three pages describe the
# instance without exposing anything about its owner or their travel.

PUBLIC_PAGES = {
    "about": ("About wayfare", "Booking documents in, checked calendar events out."),
    "privacy": ("Privacy", "What this instance stores, and what it never does."),
    "terms": ("Terms of use", "Provided as is, under the MIT licence."),
}


def _public_page(request: Request, page: str) -> HTMLResponse:
    heading, standfirst = PUBLIC_PAGES[page]
    return TEMPLATES.TemplateResponse(
        request,
        "public.html",
        {"page": page, "heading": heading, "standfirst": standfirst},
    )


# Declared one by one rather than as /{page}: a path parameter here would be
# matched before /setup and every other route defined below it.
@app.get("/about", response_class=HTMLResponse)
def about_page(request: Request):
    return _public_page(request, "about")


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page(request: Request):
    return _public_page(request, "privacy")


@app.get("/terms", response_class=HTMLResponse)
def terms_page(request: Request):
    return _public_page(request, "terms")


@app.post("/login")
def login(request: Request, token: str = Form(...), next: str = Form("/")):
    cfg = get_config()
    if not cfg.owner_token or not secrets.compare_digest(token, cfg.owner_token):
        return TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {"error": "That token was not accepted.", "next": next},
            status_code=401,
        )
    # Only ever return to a path on this site.
    destination = next if next.startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(destination, status_code=303)
    response.set_cookie(
        "wayfare_token",
        token,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=60 * 60 * 24 * 90,
    )
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("wayfare_token")
    return response


# --- staged uploads ------------------------------------------------------
#
# The transfer happens as each file is chosen, so pressing Read it costs only
# the reading. The plain multipart form still works untouched when the script
# does not run, which is why /submit accepts both.


def _staging_session(request: Request) -> str:
    return request.cookies.get("wayfare_batch") or ""


@app.post("/uploads")
async def stage_upload(
    request: Request,
    scope: Scope = Depends(require_owner),
    upload: UploadFile = File(...),
):
    """Hold one file until the batch is submitted."""
    payload = await upload.read()
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"'{upload.filename}' is too large.")

    session = _staging_session(request) or staging.new_session()
    try:
        item = staging.add(session, upload.filename or "document", payload)
    except ValueError:
        # A malformed cookie, not a malformed request: start a fresh session
        # rather than making the user clear their cookies.
        session = staging.new_session()
        item = staging.add(session, upload.filename or "document", payload)

    response = JSONResponse({"id": item.file_id, "name": item.name, "size": item.size})
    response.set_cookie(
        "wayfare_batch", session, httponly=True, samesite="lax", secure=_is_https(request)
    )
    return response


@app.get("/uploads")
def list_uploads(request: Request, scope: Scope = Depends(require_owner)):
    """Which staged files the server still holds.

    The page asks on restore. A browser going back to a submitted page shows
    the batch exactly as it was, but the server read and cleared it, so the
    two have to be reconciled before the button is pressed again.
    """
    return JSONResponse({"ids": staging.list_ids(_staging_session(request))})


@app.delete("/uploads/{file_id}")
def unstage_upload(
    request: Request, file_id: str, scope: Scope = Depends(require_owner)
):
    """Take a file back out of the batch before it is submitted."""
    try:
        staging.remove(_staging_session(request), file_id)
    except ValueError:
        pass
    return JSONResponse({"removed": True})


@app.post("/submit", response_class=HTMLResponse)
async def submit_form(
    request: Request,
    scope: Scope = Depends(require_owner),
    upload: list[UploadFile] = File(default_factory=list),
    staged: list[str] = Form(default_factory=list),
    text: str = Form(""),
):
    session = _staging_session(request)
    try:
        held = staging.collect(session, staged) if session and staged else []
    except ValueError:
        held = []

    try:
        submission = await _run_submission(upload, text, allow_promote=True, staged=held)
    except HTTPException:
        raise
    except NotAuthorised as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # The batch has been read; keeping the bytes would only mean the next one
    # starts with the last trip still attached.
    if session:
        staging.clear(session)

    response = TEMPLATES.TemplateResponse(
        request, "result.html", {"submission": submission.to_dict()}
    )
    response.delete_cookie("wayfare_batch")
    return response


@app.post("/submissions/{submission_id}/records/{index}/{action}")
def review_action(
    submission_id: str,
    index: int,
    action: str,
    scope: Scope = Depends(require_owner),
    summary: str = Form(""),
    start: str = Form(""),
    end: str = Form(""),
    then_promote: str = Form(""),
):
    try:
        if action == "promote":
            store.promote(submission_id, index)
        elif action == "discard":
            store.discard(submission_id, index)
        elif action == "amend":
            store.amend(submission_id, index, summary=summary, start=start, end=end)
            if then_promote:
                store.promote(submission_id, index)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown action '{action}'.")
    except store.AmendError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RedirectResponse("/", status_code=303)


# --- setup ---------------------------------------------------------------
#
# Connecting a Google account is the one genuinely fiddly part of running this,
# and it is fiddly in a way that has nothing to do with travel. So it happens
# entirely in the browser: the page links straight to the two Cloud Console
# pages that matter, shows the exact redirect URI to paste, takes the
# downloaded JSON as an upload, and runs the consent flow. Nobody has to know
# where the secrets directory is.


def _is_https(request: Request) -> bool:
    """Whether the browser reached us over TLS, as nginx saw it."""
    return _origin(request).startswith("https://")


def _origin(request: Request) -> str:
    """This instance's public origin, as Google will see it."""
    cfg = get_config()
    if cfg.base_url:
        return cfg.base_url
    # Behind nginx, honour the original scheme rather than the proxied one.
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{scheme}://{request.url.netloc}"


def _redirect_uri(request: Request) -> str:
    """The callback URL, matching what must be registered with Google."""
    return f"{_origin(request)}/oauth/callback"


@app.get("/setup", response_class=HTMLResponse)
def setup_page(
    request: Request,
    scope: Scope = Depends(require_owner),
    message: str = "",
    done: str = "",
):
    status = connection_status()
    return TEMPLATES.TemplateResponse(
        request,
        "setup.html",
        {
            "status": status,
            # Only after a setup step that just succeeded, never on every visit:
            # someone who came back to change a setting does not need a modal
            # telling them they are finished.
            "just_finished": bool(done) and bool(status.get("connected")),
            "model": llm_extractor.status(),
            "ocr_ready": ocr_available(),
            "barcodes_ready": barcode_extractor.available(),
            "scope": scopes()[0],
            "scope_mode": get_config().scope_mode,
            # Google requires these three on the OAuth branding screen, on the
            # same domain as the redirect URI, before an app can be published.
            "public_urls": {
                name: f"{_origin(request)}/{name}" for name in PUBLIC_PAGES
            },
            "redirect_uri": _redirect_uri(request),
            "message": message,
            "calendar_id": get_config().calendar_id,
        },
    )


@app.post("/setup/client")
async def setup_client(
    request: Request,
    scope: Scope = Depends(require_owner),
    client_json: UploadFile = File(...),
):
    payload = await client_json.read()
    if len(payload) > 64 * 1024:
        raise HTTPException(status_code=413, detail="That is far too large to be a client JSON.")
    try:
        kind = save_client_secret(payload)
    except CalendarError as exc:
        return RedirectResponse(f"/setup?message={quote(str(exc))}", status_code=303)

    note = (
        "Client saved. Now click Connect."
        if kind == "web"
        else (
            "Client saved, but it is a Desktop-type client. For the browser flow create a "
            "Web application client instead, or finish with `wayfare auth` on the server."
        )
    )
    return RedirectResponse(f"/setup?message={quote(note)}", status_code=303)


@app.post("/setup/model")
def setup_model(
    request: Request,
    scope: Scope = Depends(require_owner),
    api_key: str = Form(""),
    model: str = Form(""),
):
    """Save the model key and prove it works before saying it is set up."""
    try:
        if model.strip():
            llm_extractor.save_model(model)
        if api_key.strip():
            llm_extractor.save_api_key(api_key)
    except llm_extractor.LLMUnavailable as exc:
        return RedirectResponse(f"/setup?message={quote(str(exc))}", status_code=303)

    # Credentials are read from disk on every access, so the newly saved key
    # is live for this check without restarting anything.
    ok, detail = llm_extractor.verify()
    llm_extractor.record_check(ok, detail)
    return RedirectResponse("/setup?done=1", status_code=303)


@app.get("/setup/connect")
def setup_connect(request: Request, scope: Scope = Depends(require_owner)):
    state = secrets.token_urlsafe(24)
    try:
        url, verifier = authorisation_url(_redirect_uri(request), state)
    except CalendarError as exc:
        return RedirectResponse(f"/setup?message={quote(str(exc))}", status_code=303)

    response = RedirectResponse(url, status_code=303)
    cookie = dict(
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=600,
    )
    response.set_cookie("wayfare_oauth_state", state, **cookie)
    if verifier:
        # Needed again to redeem the code; see authorisation_url.
        response.set_cookie("wayfare_oauth_verifier", verifier, **cookie)
    return response


@app.get("/oauth/callback")
def oauth_callback(request: Request, scope: Scope = Depends(require_owner)):
    """Where Google sends the browser back after consent."""
    expected = request.cookies.get("wayfare_oauth_state")
    presented = request.query_params.get("state")
    if not expected or not presented or not secrets.compare_digest(expected, presented):
        # A mismatched state means this callback was not started here.
        return RedirectResponse(
            "/setup?message=" + quote("That sign-in did not start on this page. Try again."),
            status_code=303,
        )

    if "error" in request.query_params:
        reason = request.query_params["error"]
        return RedirectResponse(
            f"/setup?message={quote(f'Google refused the request: {reason}')}", status_code=303
        )

    authorisation_response = str(request.url)
    if get_config().base_url:
        # Rebuild against the public origin; behind a proxy request.url is http.
        authorisation_response = get_config().base_url + request.url.path
        if request.url.query:
            authorisation_response += f"?{request.url.query}"

    try:
        complete_web_flow(
            _redirect_uri(request),
            authorisation_response,
            code_verifier=request.cookies.get("wayfare_oauth_verifier"),
        )
    except Exception as exc:  # noqa: BLE001 - shown verbatim to the operator
        return RedirectResponse(f"/setup?message={quote(str(exc))}", status_code=303)

    response = RedirectResponse(
        "/setup?done=1&message=" + quote("Calendar connected."), status_code=303
    )
    response.delete_cookie("wayfare_oauth_state")
    response.delete_cookie("wayfare_oauth_verifier")
    return response


# --- api -----------------------------------------------------------------


@app.get("/healthz")
def healthz():
    """Unauthenticated liveness only. Deliberately reveals nothing."""
    return {"status": "ok"}


@app.post("/api/v1/ingest")
async def api_ingest(
    scope: Scope = Depends(require_any),
    upload: list[UploadFile] = File(default_factory=list),
    text: str = Form(""),
):
    """Submit one or more documents, a text snippet, or both.

    Repeating the ``upload`` field sends a whole trip at once, which is the
    only way the cross-record checks can see an outbound, a return and a hotel
    together.

    An agent token always submits with promotion disabled, so everything it
    sends lands in the pending calendar regardless of how clean it looks.
    """
    submission = await _run_submission(upload, text, allow_promote=(scope == "owner"))
    return JSONResponse(submission.to_dict())


@app.get("/api/v1/submissions/{submission_id}")
def api_submission(submission_id: str, scope: Scope = Depends(require_any)):
    data = store.load(submission_id)
    if data is None:
        raise HTTPException(status_code=404, detail="No such submission.")
    return JSONResponse(data)


@app.post("/api/v1/submissions/{submission_id}/records/{index}/promote")
def api_promote(submission_id: str, index: int, scope: Scope = Depends(require_owner)):
    try:
        return JSONResponse(store.promote(submission_id, index))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/v1/submissions/{submission_id}/records/{index}/amend")
def api_amend(
    submission_id: str,
    index: int,
    scope: Scope = Depends(require_owner),
    summary: str = Form(""),
    start: str = Form(""),
    end: str = Form(""),
):
    try:
        return JSONResponse(store.amend(submission_id, index, summary=summary, start=start, end=end))
    except store.AmendError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/v1/undo")
def api_undo(count: int = 1, scope: Scope = Depends(require_owner)):
    return JSONResponse({"undone": CalendarClient().undo_last(count)})


# --- shared --------------------------------------------------------------


def _read_one(path: Path, name: str):
    """Extract one document, turning a failure into a finding rather than a 500.

    A batch is a whole trip. One unreadable screenshot — a truncated paste, a
    format tesseract will not open — must not throw away the flights that were
    submitted alongside it, and the person needs to be told which file failed.
    """
    try:
        return process_file(path, name)
    except Exception as exc:  # noqa: BLE001 - any reader failure, reported not raised
        failed = Itinerary()
        failed.add_issue(
            IssueLevel.ERROR,
            "ingest.unreadable",
            f"'{name}' could not be read ({type(exc).__name__}). "
            "Everything else in this submission was still processed.",
            "web",
        )
        return failed


async def _run_submission(uploads, text: str, allow_promote: bool, staged: list | None = None):
    """Ingest every input given, combine them into one trip, then commit.

    Files and pasted text are not alternatives. A round trip is normally two
    files, and a booking whose hotel came by email and whose flights came as
    boarding passes is three — the checks that pay for this tool only work if
    they arrive as one itinerary.
    """
    if isinstance(uploads, UploadFile) or uploads is None:
        uploads = [uploads] if uploads is not None else []
    files = [f for f in uploads if f is not None and f.filename]

    itineraries = []
    sources = []

    # Files already uploaded one at a time while the user was still choosing.
    for item in staged or []:
        itineraries.append(_read_one(item.path, item.name))
        sources.append(item.name)

    for upload in files:
        payload = await upload.read()
        if len(payload) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail=f"'{upload.filename}' is too large.")

        with tempfile.TemporaryDirectory(prefix="wayfare-upload-") as tmp:
            path = Path(tmp) / Path(upload.filename).name
            path.write_bytes(payload)
            itineraries.append(_read_one(path, upload.filename))
        sources.append(upload.filename)

    if text.strip():
        itineraries.append(process_text(text, "pasted text"))
        sources.append("pasted text")

    if not itineraries:
        raise HTTPException(status_code=400, detail="Send at least one file, or some text.")

    itinerary = batch.combine(itineraries, existing_events=[])
    itinerary = _recheck_with_calendar(itinerary)
    return store.commit(itinerary, batch.describe(sources), allow_promote=allow_promote)


def _calendar_context_safe(records) -> list:
    """Existing events for the duplicate check, or nothing if the calendar is unreachable."""
    try:
        client = CalendarClient()
        return client.context_window(records) if records else []
    except Exception:  # noqa: BLE001 - the duplicate check is a bonus, never a blocker
        return []


def _recheck_with_calendar(itinerary):
    """Re-run the duplicate check now that the itinerary's dates are known."""
    from ..validate import coherence

    existing = _calendar_context_safe(itinerary.records)
    if existing:
        coherence._check_duplicates(itinerary, existing)
    return itinerary


def cleanup_uploads() -> None:  # pragma: no cover - operational helper
    """Remove any stray upload temporaries left by a crash."""
    for path in Path(tempfile.gettempdir()).glob("wayfare-upload-*"):
        shutil.rmtree(path, ignore_errors=True)
