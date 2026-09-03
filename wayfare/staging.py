"""Files uploaded while the user is still assembling a batch.

Reading a trip takes real time — OCR on several screenshots, then a model
call — and none of it can start until the last file arrives. Uploading each
file the moment it is chosen moves the transfer off the submit button, so
pressing it costs only the reading.

A staging area is a directory per browser session, holding the bytes and the
original filename. It is deliberately dumb: nothing is extracted, parsed or
validated here. That happens once, on submit, over the whole trip.

Staged files are temporary by construction. They are removed when the batch is
submitted, when the user takes a file back out, and — for anything abandoned —
by a sweep on the next upload.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .config import get_config

#: Abandoned staging directories are swept after this long. Long enough to
#: assemble a trip over a coffee, short enough not to accumulate.
EXPIRE_AFTER_SECONDS = 6 * 3600

#: Filenames are shown back to the user and used as the submission's label, so
#: they are stored as data rather than trusted as paths.
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ ()+-]")


@dataclass(frozen=True)
class StagedFile:
    file_id: str
    name: str
    size: int
    path: Path


def _root() -> Path:
    return get_config().state_dir / "staging"


def _session_dir(session: str) -> Path:
    # The session id comes from a cookie, so it never reaches the filesystem
    # unchecked: a traversal here would let a caller write anywhere.
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", session or ""):
        raise ValueError("Bad staging session.")
    return _root() / session


def new_session() -> str:
    return secrets.token_urlsafe(18)


def safe_name(name: str) -> str:
    """A display name with no path in it, and never empty."""
    cleaned = _SAFE_NAME.sub("_", Path(name or "").name).strip()
    return cleaned[:120] or "document"


def add(session: str, name: str, payload: bytes) -> StagedFile:
    """Stage one uploaded file and return its handle."""
    sweep()
    directory = _session_dir(session)
    directory.mkdir(parents=True, exist_ok=True)

    file_id = secrets.token_urlsafe(12)
    display = safe_name(name)
    path = directory / file_id
    path.write_bytes(payload)
    (directory / f"{file_id}.json").write_text(
        json.dumps({"name": display, "size": len(payload)}), encoding="utf-8"
    )
    return StagedFile(file_id=file_id, name=display, size=len(payload), path=path)


def get(session: str, file_id: str) -> StagedFile | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", file_id or ""):
        return None
    directory = _session_dir(session)
    path = directory / file_id
    meta = directory / f"{file_id}.json"
    if not path.exists() or not meta.exists():
        return None
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return StagedFile(
        file_id=file_id,
        name=str(data.get("name", "document")),
        size=int(data.get("size", 0)),
        path=path,
    )


def collect(session: str, file_ids: list[str]) -> list[StagedFile]:
    """The staged files named, in the order the user arranged them.

    Ids that no longer exist are skipped rather than raising: a submission
    should not fail because one file was already swept or removed in another
    tab.
    """
    found = []
    for file_id in file_ids:
        item = get(session, file_id)
        if item is not None:
            found.append(item)
    return found


def list_ids(session: str) -> list[str]:
    """Everything currently staged for this session.

    The page uses this to tell whether the batch it is holding is still on the
    server. Coming back to a submitted page leaves the browser showing files
    that were read and cleared minutes ago.
    """
    try:
        directory = _session_dir(session)
    except ValueError:
        return []
    try:
        return sorted(p.name for p in directory.iterdir() if p.suffix != ".json")
    except OSError:
        return []


def remove(session: str, file_id: str) -> bool:
    item = get(session, file_id)
    if item is None:
        return False
    item.path.unlink(missing_ok=True)
    item.path.with_suffix(".json").unlink(missing_ok=True)
    return True


def clear(session: str) -> None:
    """Drop a whole session, once its batch has been submitted."""
    try:
        shutil.rmtree(_session_dir(session), ignore_errors=True)
    except ValueError:
        pass


def sweep(now: float | None = None) -> int:
    """Delete staging directories nobody came back for."""
    now = now if now is not None else time.time()
    removed = 0
    try:
        directories = list(_root().iterdir())
    except OSError:
        return 0

    for directory in directories:
        try:
            if now - directory.stat().st_mtime > EXPIRE_AFTER_SECONDS:
                shutil.rmtree(directory, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed
