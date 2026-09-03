"""Command line interface.

Everything the microsite does is available here too, so the tool is usable
over SSH, from a cron job, or by an agent that would rather run a command than
make an HTTP request. `parse` is the safe one to reach for first: it runs the
whole pipeline and prints the verdict without touching any calendar.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

from . import batch, store
from .airports import AIRPORTS_URL
from .config import generate_token, get_config
from .pipeline import process_file, process_text
from .schema import IssueLevel

GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


def _colour(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if sys.stdout.isatty() else text


def _print_submission(submission: dict) -> None:
    counts = submission["summary"]
    print(f"\n{submission['source_file']} — {submission['submission_id']}")
    print(
        f"  {counts['promoted']} added · {counts['pending']} held · {counts['rejected']} rejected\n"
    )

    for index, record in enumerate(submission["records"]):
        badge = {
            "promoted": _colour("ADDED   ", GREEN),
            "pending": _colour("HELD    ", YELLOW),
            "rejected": _colour("REJECTED", RED),
            "discarded": _colour("DISCARD ", RED),
        }.get(record["status"], record["status"])
        print(f"  [{index}] {badge}  {record['summary']}  ({record['confidence']:.0%})")
        print(f"       {_colour(record['reason'], DIM)}")
        for issue in record["issues"]:
            if issue["level"] == "info":
                continue
            mark = _colour("!", YELLOW) if issue["level"] == "warn" else _colour("x", RED)
            print(f"       {mark} {issue['message']}")
        print()

    for issue in submission.get("itinerary_issues", []):
        if issue["level"] != "info":
            print(f"  {_colour('!', YELLOW)} {issue['message']}")


def _read_input(args) -> tuple:
    """Read every input given and combine them into one itinerary.

    Several paths are one *trip*, not several submissions: the cross-record
    checks only see a hotel booked for the wrong month if the flights are in
    front of them at the same time.
    """
    itineraries = []
    sources = []

    for raw in args.paths or []:
        if raw == "-":
            itineraries.append(process_text(sys.stdin.read(), "stdin"))
            sources.append("stdin")
            continue
        path = Path(raw).expanduser()
        if not path.exists():
            raise SystemExit(f"No such file: {path}")
        itineraries.append(process_file(path, path.name))
        sources.append(path.name)

    if args.text:
        itineraries.append(process_text(args.text, "pasted text"))
        sources.append("pasted text")

    if not itineraries:
        itineraries.append(process_text(sys.stdin.read(), "stdin"))
        sources.append("stdin")

    return batch.combine(itineraries), batch.describe(sources)


def cmd_parse(args) -> int:
    itinerary, source = _read_input(args)
    submission = store.commit(itinerary, source, dry_run=True)
    if args.json:
        print(json.dumps(submission.to_dict(), indent=2))
    else:
        _print_submission(submission.to_dict())
        print(_colour("  Dry run — nothing was written to any calendar.", DIM))
    return 0 if not any(r.status == "rejected" for r in submission.outcomes) else 1


def cmd_add(args) -> int:
    itinerary, source = _read_input(args)
    submission = store.commit(itinerary, source, allow_promote=not args.hold)
    if args.json:
        print(json.dumps(submission.to_dict(), indent=2))
    else:
        _print_submission(submission.to_dict())
    return 0


def cmd_promote(args) -> int:
    record = store.promote(args.submission_id, args.index)
    print(f"Promoted: {record['summary']}")
    return 0


def cmd_undo(args) -> int:
    from .calendar_api import CalendarClient

    for line in CalendarClient().undo_last(args.count) or ["nothing to undo"]:
        print(line)
    return 0


def cmd_auth(args) -> int:
    from .calendar_api import authorise

    path = authorise()
    print(f"Stored Google credentials at {path}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    uvicorn.run("wayfare.web.app:app", host=args.host, port=args.port, log_level="info")
    return 0


def cmd_fetch_airports(args) -> int:
    cfg = get_config()
    cfg.ensure_dirs()
    print(f"Downloading {AIRPORTS_URL} …")
    with urllib.request.urlopen(AIRPORTS_URL, timeout=120) as response:
        cfg.airports_csv.write_bytes(response.read())

    from .airports import get_airport_db

    print(f"Wrote {cfg.airports_csv} ({len(get_airport_db())} airports with IATA codes)")
    return 0


def cmd_learn(args) -> int:
    """Derive calendar conventions from an exported calendar."""
    from .icsparse import read
    from .learn import learn, write_conventions

    path = Path(args.path).expanduser()
    if not path.exists():
        raise SystemExit(f"No such file: {path}")

    events = read(path)
    if not events:
        raise SystemExit(f"No events found in {path}. Is it an .ics export or a Takeout .zip?")

    conventions, text = learn(events)
    print(text)

    if args.write:
        destination = Path(args.write).expanduser()
        write_conventions(conventions, destination)
        print(f"\nWrote {destination}")
        print("Point WAYFARE_CONVENTIONS at it to use these.")
    else:
        print("\nRe-run with --write PATH to save these as a conventions file.")
    return 0


def cmd_token(args) -> int:
    print(generate_token())
    return 0


def cmd_doctor(args) -> int:
    """Report which parts of the pipeline are actually available."""
    from .airports import get_airport_db
    from .extractors import barcode, kitinerary, llm
    from .ocr import available as ocr_available

    cfg = get_config()
    checks = [
        ("OCR (tesseract)", ocr_available(), "screenshots and scanned PDFs"),
        ("Barcodes (zbarimg)", barcode.available(), "boarding-pass barcodes — the exact source"),
        ("KItinerary", kitinerary.available(), "airline/rail/hotel document parsers (optional)"),
        ("Model backend", llm.available(), "reading text that has no barcode"),
        ("Airport database", get_airport_db().available, "timezone and block-time checks"),
        ("Google credentials", cfg.oauth_token.exists(), "writing to the calendar"),
        ("Owner token", bool(cfg.owner_token), "the review site"),
        ("Agent token", bool(cfg.agent_token), "the scoped agent API"),
    ]
    for name, ok, why in checks:
        mark = _colour("✓", GREEN) if ok else _colour("✗", RED)
        print(f" {mark} {name:<22} {_colour(why, DIM)}")
    if get_airport_db().available:
        print(f"\n   {len(get_airport_db())} airports loaded.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wayfare", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_input_args(p):
        p.add_argument(
            "paths",
            nargs="*",
            help="Files to read, or '-' for stdin. Give a whole trip at once — "
            "outbound, return and hotel are checked against each other.",
        )
        p.add_argument("--text", help="Parse this string as well as any files")
        p.add_argument("--json", action="store_true", help="Machine-readable output")

    p = sub.add_parser("parse", help="Read a document and print the verdict, writing nothing")
    add_input_args(p)
    p.set_defaults(func=cmd_parse)

    p = sub.add_parser("add", help="Read a document and write it to the calendar")
    add_input_args(p)
    p.add_argument("--hold", action="store_true", help="Force everything into pending")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("promote", help="Move a held record onto the real calendar")
    p.add_argument("submission_id")
    p.add_argument("index", type=int)
    p.set_defaults(func=cmd_promote)

    p = sub.add_parser("undo", help="Reverse the most recent calendar writes")
    p.add_argument("--count", type=int, default=1)
    p.set_defaults(func=cmd_undo)

    sub.add_parser("auth", help="Run the Google OAuth consent flow").set_defaults(func=cmd_auth)

    p = sub.add_parser("serve", help="Run the review microsite")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8791)
    p.set_defaults(func=cmd_serve)

    sub.add_parser("fetch-airports", help="Download the airport database").set_defaults(
        func=cmd_fetch_airports
    )
    p = sub.add_parser(
        "learn", help="Derive title conventions from an exported calendar (.ics or Takeout .zip)"
    )
    p.add_argument("path")
    p.add_argument("--write", metavar="PATH", help="Save the derived conventions to this file")
    p.set_defaults(func=cmd_learn)

    sub.add_parser("token", help="Print a fresh random bearer token").set_defaults(func=cmd_token)
    sub.add_parser("doctor", help="Show which pipeline components are available").set_defaults(
        func=cmd_doctor
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
