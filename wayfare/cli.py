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


def cmd_fetch_reference(args) -> int:
    """Download the airline and station tables.

    Kept out of the repository deliberately. These are other people's
    datasets — OpenTravelData under CC-BY, Trainline's stations under ODbL,
    which requires modifications to be published — and vendoring 70,000 rows
    of somebody else's data into a public repo is redistribution with the
    obligations that carries. Downloading them makes the licence the user's
    relationship with the publisher, and keeps the tables current.
    """
    from . import reference

    cfg = get_config()
    cfg.ensure_dirs()

    for label, url, name in (
        ("airlines", reference.AIRLINES_URL, "airlines.csv"),
        ("stations", reference.STATIONS_URL, "stations.csv"),
    ):
        print(f"Downloading {label} …")
        try:
            with urllib.request.urlopen(url, timeout=180) as response:
                (cfg.data_dir / name).write_bytes(response.read())
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            print(f"  could not fetch {label}: {type(exc).__name__}: {exc}")

    reference.clear_cache()
    airlines, stations = reference.available()
    print(f"\n{len(reference._airlines()) if airlines else 0} airlines, "
          f"{len(reference._stations()) if stations else 0} stations.")
    print("Airline data from OpenTravelData (CC-BY); stations from Trainline (ODbL).")
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


def cmd_bench(args) -> int:
    """Run the corpus and print what matched.

    No pass mark. The output is a set of numbers to put beside the previous
    set of numbers, because "did this change help" is the question a benchmark
    can answer and "is this good" is not.
    """
    import json as jsonlib
    from pathlib import Path

    from . import bench

    root = Path(args.corpus).expanduser()
    if not root.is_dir():
        print(f"No such corpus directory: {root}")
        return 1

    def tick(index, total, case):
        if not args.json:
            print(f"\r  {index}/{total} {case.name[:60]:<60}", end="", flush=True)

    results = bench.run(
        root, limit=args.limit, only=args.only, use_llm=args.llm, progress=tick
    )
    if not args.json:
        print("\r" + " " * 72 + "\r", end="")

    if not results:
        print("No documents with answers found. Is this the extractordata directory?")
        return 1

    summary = bench.summarise(results)
    if args.json:
        print(jsonlib.dumps(summary, indent=2))
        return 0

    reading = "model + deterministic extractors" if args.llm else "deterministic extractors only"
    print(f"{summary['documents']} documents, {reading}\n")

    print(f"  {'category':<10} {'docs':>5} {'right count':>12} {'legs found':>12}")
    for name, bucket in sorted(summary["categories"].items()):
        share = f"{bucket['right_count']}/{bucket['documents']}"
        legs = f"{bucket['found']}/{bucket['expected']}"
        print(f"  {name:<10} {bucket['documents']:>5} {share:>12} {legs:>12}")

    print("\n  field           correct")
    for name, (got, total) in summary["fields"].items():
        percent = f"{got / total:.0%}" if total else "—"
        print(f"  {name:<15} {got:>4}/{total:<4} {percent:>5}")

    if summary["errors"]:
        print(f"\n  {summary['errors']} document(s) could not be read at all.")

    if args.failures:
        print("\n  worst documents:")
        ranked = sorted(
            results,
            key=lambda r: (
                r.error is None,
                sum(g for g, _ in r.fields.values()) / max(1, sum(t for _, t in r.fields.values())),
            ),
        )
        for result in ranked[:15]:
            if result.error:
                print(f"    {result.case.name:<50} {result.error[:40]}")
            else:
                got = sum(g for g, _ in result.fields.values())
                total = sum(t for _, t in result.fields.values())
                print(
                    f"    {result.case.name:<50} {got}/{total} fields, "
                    f"{result.found}/{result.expected_count} legs"
                )
    return 0


def cmd_token(args) -> int:
    print(generate_token())
    return 0


def cmd_doctor(args) -> int:
    """Report which parts of the pipeline are actually available."""
    from .airports import get_airport_db
    from . import reference
    from .extractors import barcode, kitinerary, llm
    from .ocr import available as ocr_available

    cfg = get_config()
    checks = [
        ("OCR (tesseract)", ocr_available(), "screenshots and scanned PDFs"),
        ("Barcodes (zbarimg)", barcode.available(), "boarding-pass barcodes — the exact source"),
        ("KItinerary", kitinerary.available(), "airline/rail/hotel document parsers (optional)"),
        ("Model backend", llm.available(), "reading text that has no barcode"),
        ("Airport database", get_airport_db().available, "timezone and block-time checks"),
        ("Airline names", reference.available()[0], "\"S4 246\" is not a readable calendar entry"),
        ("Station table", reference.available()[1], "rail timezones without guessing at a city"),
        ("Google credentials", cfg.oauth_token.exists(), "writing to the calendar"),
        ("Owner token", bool(cfg.owner_token), "the review site"),
        ("Agent token", bool(cfg.agent_token), "the scoped agent API"),
    ]
    for name, ok, why in checks:
        mark = _colour("✓", GREEN) if ok else _colour("✗", RED)
        print(f" {mark} {name:<22} {_colour(why, DIM)}")
    if get_airport_db().available:
        print(f"\n   {len(get_airport_db())} airports loaded.")

    # Which endpoints, not just whether there is one. Free tiers cap
    # independently, and "the model backend is configured" was true on a day
    # when every model on the only endpoint was refusing.
    if llm.available():
        providers = cfg.providers
        print()
        for name in cfg.enabled_providers:
            base = providers.conf(name).get("api_base", "?")
            keyed = "key" if cfg.provider_key(name) else "keyless"
            named = providers.models(name)
            how = f"{len(named)} named model{'s' if len(named) != 1 else ''}" if named else "discovers its free models"
            print(f"   {name:<12} {_colour(base, DIM)}  ({keyed}, {how})")
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

    sub.add_parser(
        "fetch-reference",
        help="Download the airline and station tables (OpenTravelData, Trainline)",
    ).set_defaults(func=cmd_fetch_reference)

    sub.add_parser("fetch-airports", help="Download the airport database").set_defaults(
        func=cmd_fetch_airports
    )
    p = sub.add_parser(
        "learn", help="Derive title conventions from an exported calendar (.ics or Takeout .zip)"
    )
    p.add_argument("path")
    p.add_argument("--write", metavar="PATH", help="Save the derived conventions to this file")
    p.set_defaults(func=cmd_learn)

    p = sub.add_parser(
        "bench", help="Score wayfare against a corpus of documents with known answers"
    )
    p.add_argument("corpus", help="Directory of documents each paired with a .json answer")
    p.add_argument("--limit", type=int, help="Only the first N documents")
    p.add_argument(
        "--only",
        choices=["flight", "train", "bus", "lodging", "mixed"],
        help="Only documents of one kind",
    )
    p.add_argument(
        "--llm",
        action="store_true",
        help="Ask the model too. Off by default: a free tier allows ~50 requests a day "
        "and one document can cost six.",
    )
    p.add_argument("--json", action="store_true", help="Print the summary as JSON")
    p.add_argument("--failures", action="store_true", help="List the documents that scored worst")
    p.set_defaults(func=cmd_bench)

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
