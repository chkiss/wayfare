"""Finding models the provider currently offers at no cost.

A hard-coded list of free models rots. They are withdrawn and added
constantly, and when the last one on a stale list disappears the application
stops working for a reason its user cannot see. Asking the provider is both
simpler and self-maintaining.

Uses urllib so this module stays dependency-free; a caller that already has an
HTTP client can pass its own ``fetch``.
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Callable

#: How long a fetched catalogue stays fresh.
CACHE_SECONDS = 900

_cache: dict[str, tuple[float, list[str]]] = {}


def _default_fetch(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def free_models(
    base_url: str,
    fetch: Callable[[str, float], dict] | None = None,
    timeout: float = 20.0,
    clock: Callable[[], float] = time.time,
    text_only: bool = True,
) -> list[str]:
    """Free model ids from an OpenAI-compatible ``/models`` endpoint.

    Ordered by context length, largest first. Returns the last known list on a
    fetch failure, and an empty list if there has never been one — discovery is
    a convenience and must never be the reason a call does not happen.

    ``text_only`` filters out image, audio and video models, which are also
    listed as free and would be tried and fail.
    """
    base = base_url.rstrip("/")
    cached = _cache.get(base)
    now = clock()
    if cached and now - cached[0] < CACHE_SECONDS:
        return cached[1]

    try:
        payload = (fetch or _default_fetch)(f"{base}/models", timeout)
        entries = payload.get("data", []) if isinstance(payload, dict) else []
    except Exception:  # noqa: BLE001 - see the docstring
        return cached[1] if cached else []

    ranked: list[tuple[int, str]] = []
    for entry in entries:
        identifier = entry.get("id")
        if not identifier or not _is_free(entry):
            continue
        if text_only and not _is_text_model(entry):
            continue
        try:
            context = int(entry.get("context_length") or 0)
        except (TypeError, ValueError):
            context = 0
        ranked.append((context, identifier))

    models = [identifier for _, identifier in sorted(ranked, reverse=True)]
    _cache[base] = (now, models)
    return models


def _is_free(entry: dict) -> bool:
    pricing = entry.get("pricing") or {}
    try:
        return all(float(pricing.get(key, 1) or 0) == 0 for key in ("prompt", "completion"))
    except (TypeError, ValueError):
        return False


def _is_text_model(entry: dict) -> bool:
    architecture = entry.get("architecture") or {}
    inputs = architecture.get("input_modalities") or []
    outputs = architecture.get("output_modalities") or []
    if inputs and "text" not in inputs:
        return False
    if outputs and outputs != ["text"]:
        return False
    modality = architecture.get("modality") or ""
    return not modality or modality.endswith("->text")


def clear_cache() -> None:
    """Forget the catalogue. For tests, and for a manual refresh."""
    _cache.clear()
