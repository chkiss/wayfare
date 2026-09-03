"""Interpreting extracted text with a free model, under supervision.

The model is never shown an image and never asked to read anything. It is
given text that a deterministic tool already produced, and asked to say which
parts of it are a flight, a train, a hotel or something else.

Two constraints make a weak free model safe enough to use here:

1. **Strict JSON, no prose.** The reply must parse as the declared schema or
   it is discarded outright. There is no "best effort" path.
2. **Evidence quoting.** For every field it fills in, the model must quote the
   exact substring of the source text it took the value from. Each quote is
   then checked against the source. A model that invents a departure time
   cannot produce a quote for it, and the invented field is dropped rather
   than believed.

The second rule is what makes this work with models that are individually
unreliable — including ones whose vision is known to be inconsistent. It turns
hallucination from a silent corruption into a detected, reported failure.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import httpx

from ..config import get_config
from ..vendor import modelchain
from ..schema import (
    FlightRecord,
    IssueLevel,
    LocalTime,
    LodgingRecord,
    OtherRecord,
    Place,
    Provenance,
    Record,
    TrainRecord,
)

SOURCE = "llm"

SYSTEM_PROMPT = """\
You convert travel booking text into structured JSON. You are reading text that \
has already been extracted from a document; you are not interpreting an image.

Reply with ONE JSON object and nothing else. No prose, no markdown fence.

Schema:
{
  "records": [
    {
      "kind": "flight" | "train" | "bus" | "ferry" | "lodging" | "other",
      "carrier": string|null,          // flight: airline IATA code, e.g. "BA"
      "operator": string|null,         // train/bus/ferry: operator, e.g. "Amtrak"
      "number": string|null,           // service number, digits only
      "origin_iata": string|null,      // flight: 3-letter airport code
      "origin_name": string|null,      // station or airport as printed
      "origin_city": string|null,      // the town it is in, e.g. "Boston"
      "destination_iata": string|null,
      "destination_name": string|null,
      "destination_city": string|null,
      "departure_local": string|null,  // "YYYY-MM-DDTHH:MM", local time at origin
      "arrival_local": string|null,    // "YYYY-MM-DDTHH:MM", local time at destination
      "property_name": string|null,    // lodging
      "address": string|null,
      "check_in_local": string|null,   // "YYYY-MM-DDTHH:MM"
      "check_out_local": string|null,
      "title": string|null,            // "other" only
      "start_local": string|null,      // "other" only
      "end_local": string|null,        // "other" only
      "seat": string|null,
      "cabin": string|null,
      "room": string|null,
      "confirmation": string|null,
      "traveller": string|null,
      "evidence": { "<field name>": "<exact substring copied from the source>" }
    }
  ]
}

Rules, in order of importance:
1. NEVER invent a value. If the source does not state something, use null.
   A null is always better than a guess. Omitting a field costs nothing;
   inventing one puts a wrong time in someone's calendar.
2. For every non-null field, put an entry in "evidence" whose value is copied
   CHARACTER FOR CHARACTER from the source text. Do not paraphrase, reformat
   or normalise the quote. Quotes are checked automatically.
3. Times are local wall-clock times as printed. Do not convert timezones.
   Do not adjust for anything.
4. If the year is not stated anywhere, use null rather than assuming one.
5. Return an empty "records" list if the text contains no travel booking.
6. A station name is not a place name. "Back Bay Station" is in Boston and
   "Gare de Lyon" is in Paris, so fill in the *_city fields as well as the
   *_name fields. The city decides the timezone, which decides whether the
   hour on the calendar is right.
7. A city may be quoted from any part of the source, including a station code
   table, a header, or an address. If the city is genuinely not stated and you
   only know it from your own knowledge of the station, still fill it in and
   quote the station name it came from as the evidence.
"""


class LLMUnavailable(RuntimeError):
    """Raised when no model backend is configured."""


def available() -> bool:
    return bool(get_config().llm_api_key)


# --- setup helpers -------------------------------------------------------


def save_api_key(key: str, cfg=None) -> None:
    """Store the model API key with owner-only permissions."""
    cfg = cfg or get_config()
    key = key.strip()
    if not key:
        raise LLMUnavailable("No key given.")
    if len(key) > 512 or "\n" in key:
        raise LLMUnavailable("That does not look like an API key.")

    cfg.secrets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = cfg.secrets_dir / "llm_api_key"
    path.write_text(key + "\n", encoding="utf-8")
    path.chmod(0o600)


def save_model(model: str, cfg=None) -> None:
    """Store the chosen model name alongside the key."""
    cfg = cfg or get_config()
    model = model.strip()
    if not model:
        return
    cfg.secrets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (cfg.secrets_dir / "llm_model").write_text(model + "\n", encoding="utf-8")


def verify() -> tuple[bool, str]:
    """Make one small real request, so setup fails here rather than mid-trip.

    A key that is merely *present* proves nothing: it can be revoked, out of
    credit, or for the wrong provider. Better to find out on the setup page
    than when a boarding pass is being read at an airport.
    """
    cfg = get_config()
    if not cfg.llm_api_key:
        return False, "No key saved."

    try:
        response = httpx.post(
            f"{cfg.llm_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {cfg.llm_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": cfg.llm_model,
                "temperature": 0,
                "max_tokens": 5,
                "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
            },
            timeout=45.0,
        )
    except Exception as exc:  # noqa: BLE001 - reported to the operator as text
        return False, f"Could not reach {cfg.llm_base_url} ({type(exc).__name__})."

    if response.status_code == 401:
        return False, "The provider rejected that key (401)."
    if response.status_code == 402:
        return False, "That key has no credit for this model (402)."
    if response.status_code == 404:
        return False, f"The provider does not offer '{cfg.llm_model}' (404). Try another model."
    if response.status_code == 429:
        # The provider answered, which means the key was accepted. A busy free
        # tier is not a broken key and must not be reported as one.
        spares = len([m for m in free_models(cfg) if m != cfg.llm_model])
        return True, (
            f"Key accepted. {cfg.llm_model} is rate-limited right now, which is normal "
            f"for a free model; {spares} other free models are available as fallbacks."
        )
    if response.status_code >= 400:
        return False, f"The provider returned {response.status_code}."

    try:
        reply = response.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError):
        return False, "The provider replied in a shape this tool does not understand."

    return True, f"{cfg.llm_model} answered: {reply.strip()[:40]!r}"


def record_check(ok: bool, detail: str, cfg=None) -> None:
    """Remember the last test, so the page can show it instead of a flash."""
    cfg = cfg or get_config()
    cfg.secrets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (cfg.secrets_dir / "llm_status.json").write_text(
        json.dumps(
            {
                "ok": ok,
                "detail": detail,
                "model": cfg.llm_model,
                "checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        ),
        encoding="utf-8",
    )


def last_check(cfg=None) -> dict | None:
    cfg = cfg or get_config()
    try:
        return json.loads((cfg.secrets_dir / "llm_status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def status() -> dict:
    """What the setup page needs. Never returns the key itself."""
    cfg = get_config()
    key = cfg.llm_api_key
    check = last_check(cfg)
    # A result recorded against a different model says nothing about this one.
    if check and check.get("model") != cfg.llm_model:
        check = None
    return {
        "configured": bool(key),
        # Enough to tell two keys apart, not enough to use one.
        "hint": f"…{key[-4:]}" if key else None,
        "model": cfg.llm_model,
        "base_url": cfg.llm_base_url,
        "check": check,
    }


def extract(text: str, source_file: str, ocr_confidence: float | None = None) -> list[Record]:
    """Ask the configured model to structure already-extracted text."""
    cfg = get_config()
    if not cfg.llm_api_key:
        raise LLMUnavailable(
            "No model API key. Set WAYFARE_LLM_API_KEY or write secrets/llm_api_key."
        )

    payload = _call_model(text, cfg)
    entries = payload.get("records")
    if not isinstance(entries, list):
        return []

    records: list[Record] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        record = _build_record(entry, text, source_file, ocr_confidence)
        if record is not None:
            records.append(record)
    return records


#: Statuses that mean "this model, right now" rather than "your key". 403 is
#: here because on OpenRouter it is a per-model data policy; 401 and 402 are
#: about the key and would fail identically everywhere.
RETRYABLE = {403, 408, 429, 500, 502, 503, 504}


def _post(model: str, text: str, cfg) -> httpx.Response:
    return httpx.post(
        f"{cfg.llm_base_url.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {cfg.llm_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Source text:\n\n{text}"},
            ],
        },
        timeout=cfg.llm_timeout,
    )


def free_models(cfg=None) -> list[str]:
    """Models the provider currently offers at no cost."""
    cfg = cfg or get_config()
    return modelchain.free_models(cfg.llm_base_url)


def _bench(cfg=None):
    """Bench state on disk, so a withdrawn model is not retried every upload."""
    cfg = cfg or get_config()
    return modelchain.JsonFileBench(cfg.secrets_dir / "model_bench.json")


def _candidates(cfg) -> list[str]:
    """The configured model first, then free alternatives as fallbacks."""
    chain = [cfg.llm_model]
    for identifier in free_models(cfg):
        if identifier not in chain:
            chain.append(identifier)
        if len(chain) >= cfg.llm_fallbacks + 1:
            break
    return chain


class _Failure(str):
    """An error string that also remembers its HTTP status.

    modelchain classifies on the status when it has one, and a status is a
    fact where a message is a guess.
    """

    def __new__(cls, status: int, text: str):
        failure = super().__new__(cls, text)
        failure.status = status
        return failure


def _attempt(model: str, text: str, cfg):
    """One model, reported the way modelchain expects: (value, error)."""
    try:
        response = _post(model, text, cfg)
    except Exception as exc:  # noqa: BLE001 - the next model may work
        return None, f"{type(exc).__name__}: {exc}"

    if response.status_code in RETRYABLE:
        # Carry the body: it is what distinguishes "slow down" from "your free
        # window is spent", and the two deserve different bench durations.
        detail = response.text[:200].replace("\n", " ")
        return None, _Failure(response.status_code, f"{response.status_code} {detail}")
    if response.status_code >= 400:
        # A key problem, not a model problem. Say so rather than walking the
        # whole chain to fail identically each time.
        raise LLMUnavailable(f"{model}: provider returned {response.status_code}")

    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError):
        return None, "unexpected response shape"
    return _parse_json(content), None


def _call_model(text: str, cfg) -> dict:
    result = modelchain.run(
        _candidates(cfg),
        lambda model: _attempt(model, text, cfg),
        bench=_bench(cfg),
        # A key problem fails the same way on every model.
        fatal=(LLMUnavailable,),
    )
    if result.ok:
        return result.value

    hint = (
        "Free models are shared and rate-limited in bursts; this usually clears in a "
        "minute."
    )
    if "403" in result.summary():
        hint = (
            "A 403 on free models usually means the provider's data policy has not been "
            "accepted — on OpenRouter, enable free model publication at "
            "openrouter.ai/settings/privacy."
        )
    raise LLMUnavailable(f"No model could be reached. {result.summary()}. {hint}")


def _parse_json(content: str) -> dict:
    """Pull the JSON object out of a reply, tolerating a stray code fence."""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
        except ValueError:
            return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalise(value: str) -> str:
    """Collapse whitespace and case so quote checking survives OCR spacing."""
    return re.sub(r"\s+", " ", value).strip().casefold()


def _verify_evidence(entry: dict, source_text: str) -> tuple[set[str], set[str]]:
    """Split the entry's fields into those the source supports and those it does not."""
    evidence = entry.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {}

    haystack = _normalise(source_text)
    supported: set[str] = set()
    unsupported: set[str] = set()

    for field, value in entry.items():
        if field in {"kind", "evidence"} or value in (None, "", []):
            continue
        quote = evidence.get(field)
        if isinstance(quote, str) and quote.strip() and _normalise(quote) in haystack:
            supported.add(field)
        else:
            unsupported.add(field)
    return supported, unsupported


def _parse_local(value) -> LocalTime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return LocalTime(local=datetime.strptime(cleaned, fmt))
        except ValueError:
            continue
    return None


def _iata(value) -> str | None:
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    return code if len(code) == 3 and code.isalpha() else None


#: Scheduled surface transport the model may name, and the mode it becomes.
#: "coach" and "rail" appear because models use them interchangeably with
#: "bus" and "train", and rejecting the synonym would discard a real booking.
GROUND_MODES = {
    "train": "train",
    "rail": "train",
    "bus": "bus",
    "coach": "bus",
    "ferry": "ferry",
}


def _city_was_stated(record, source_text: str) -> None:
    """Note a city the model supplied but the document never printed.

    Cities are the one field the model is allowed to fill from its own
    knowledge, because a station name alone cannot yield a timezone and
    "Back Bay Station" says Boston to any reader. That is still an inference,
    so it is recorded as one.
    """
    haystack = _normalise(source_text)
    inferred = []
    for place in (getattr(record, "origin", None), getattr(record, "destination", None)):
        if place is not None and place.city and _normalise(place.city) not in haystack:
            inferred.append(place.city)
    if inferred:
        record.add_issue(
            IssueLevel.INFO,
            "place.city_inferred",
            "The document does not print "
            + " or ".join(sorted(set(inferred)))
            + "; the city was inferred from the station name to establish the timezone.",
            SOURCE,
        )


def _build_record(
    entry: dict, source_text: str, source_file: str, ocr_confidence: float | None
) -> Record | None:
    kind = str(entry.get("kind") or "").strip().lower()
    supported, unsupported = _verify_evidence(entry, source_text)

    # Anything the model could not quote from the source is discarded before
    # it is ever used to build a record.
    clean = {k: v for k, v in entry.items() if k in supported}

    provenance = Provenance(
        extractor="llm",
        source_file=source_file,
        ocr_confidence=ocr_confidence,
        note=f"model={get_config().llm_model}",
    )
    # Every field quoted and checked against the source is the strongest signal
    # this extractor can offer. Scoring it below the promotion threshold made
    # the threshold unreachable, so nothing read by a model could ever pass.
    confidence = 0.85 if not unsupported else 0.45

    common = {
        "confirmation": clean.get("confirmation"),
        "traveller": clean.get("traveller"),
        "extraction_confidence": confidence,
        "provenance": provenance,
    }

    record: Record | None = None

    if kind == "flight":
        departure = _parse_local(clean.get("departure_local"))
        if departure is None:
            return None
        record = FlightRecord(
            carrier=(clean.get("carrier") or "").strip().upper()[:3] or None,
            number=str(clean.get("number") or "").strip() or None,
            origin=Place(
                iata=_iata(clean.get("origin_iata")),
                name=clean.get("origin_name"),
                city=clean.get("origin_city"),
            ),
            destination=Place(
                iata=_iata(clean.get("destination_iata")),
                name=clean.get("destination_name"),
                city=clean.get("destination_city"),
            ),
            departure=departure,
            arrival=_parse_local(clean.get("arrival_local")),
            seat=clean.get("seat"),
            cabin=clean.get("cabin"),
            **common,
        )
    elif kind in GROUND_MODES:
        departure = _parse_local(clean.get("departure_local"))
        if departure is None:
            return None
        record = TrainRecord(
            mode=GROUND_MODES[kind],
            operator=clean.get("operator"),
            number=str(clean.get("number") or "").strip() or None,
            origin=Place(
                name=clean.get("origin_name"),
                city=clean.get("origin_city"),
            ),
            destination=Place(
                name=clean.get("destination_name"),
                city=clean.get("destination_city"),
            ),
            departure=departure,
            arrival=_parse_local(clean.get("arrival_local")),
            seat=clean.get("seat"),
            **common,
        )
    elif kind == "lodging":
        check_in = _parse_local(clean.get("check_in_local"))
        check_out = _parse_local(clean.get("check_out_local"))
        if check_in is None or check_out is None:
            return None
        record = LodgingRecord(
            property_name=clean.get("property_name"),
            location=Place(name=clean.get("property_name"), address=clean.get("address")),
            check_in=check_in,
            check_out=check_out,
            room=clean.get("room"),
            **common,
        )
    elif kind == "other":
        start = _parse_local(clean.get("start_local"))
        title = clean.get("title")
        if start is None or not title:
            return None
        record = OtherRecord(
            title=title,
            location=Place(name=clean.get("address")) if clean.get("address") else None,
            start=start,
            end=_parse_local(clean.get("end_local")),
            **common,
        )

    if record is None:
        return None

    _city_was_stated(record, source_text)

    if unsupported:
        record.add_issue(
            IssueLevel.WARN,
            "llm.unsupported_fields",
            "The model could not quote the source for: "
            + ", ".join(sorted(unsupported))
            + ". Those values were discarded rather than used.",
            SOURCE,
        )
    return record
