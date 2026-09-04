"""How many journeys does this document describe?

Answered before any model reads it, and answered from several independent
signals, because no single one is trustworthy on its own. Measured against real
tickets:

* a SATA receipt names both flights as "S4246" and "S4120" but writes its
  routes as full airport names, so the route signal sees nothing;
* an Amtrak eTicket writes its route as "BBY » NYP" but its service number is a
  bare "85" with no carrier code, so the designator signal sees nothing.

Either document alone would defeat either signal. Together they cover both, and
a boarding-pass barcode states its own leg count outright, which beats reading
the page at all.

The rule throughout is that a signal must *identify* a journey, not merely
count something. Counting was tried and is hopeless: "TRAIN NORTHEAST REGIONAL
... 85" yields the right number, and the same pattern also yields the baggage
allowance, the minimum check-in time and two "1"s from the piece counts. A
signal that cannot say which journey it found cannot be used to ask for it
either.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: A carrier code and a service number: "S4246", "BA 117". Two digits minimum,
#: because "10:26 AM 2:29 PM" otherwise reads as service "AM2" — and Amtrak's
#: carrier code being AM meant that passed every filter downstream.
_DESIGNATOR_RE = re.compile(r"\b([A-Z][A-Z0-9]|[0-9][A-Z])\s?(\d{2,4})\b")

#: The single-digit flight numbers the rule above gives up: "QF 8", "LH 4".
#: Admitted only immediately after a word that means this is a service, which
#: is what keeps the time-of-day case out.
_SHORT_DESIGNATOR_RE = re.compile(
    r"\b(?:flight|flt|voo|vol|vuelo|volo|flug)\b\W{0,10}([A-Z][A-Z0-9])\s?(\d)\b", re.I
)

#: "BBY » NYP", "LHR - JFK", "PDL → LIS". Identifies a journey with no service
#: number at all, which is how a train whose number is bare gets counted.
_ROUTE_RE = re.compile(r"\b([A-Z]{3})\s*(»|>>|>|→|-{1,2}|–|—|to)\s*([A-Z]{3})\b")

#: Separators that mean "travelling to" and nothing else. An arrow between two
#: three-letter words is a route; a hyphen might be a hyphen.
_STRONG_SEPARATORS = {"»", ">>", ">", "→"}

#: Three-letter words that turn up either side of a *weak* separator and are
#: not airports. Only consulted for those: this list contains WAS, which is
#: also Washington, and skipping "NYP » WAS" lost a real leg.
_NOT_A_CODE = {
    "THE", "AND", "FOR", "YOU", "ALL", "ONE", "TWO", "NEW", "VIA", "PER", "SEE",
    "NOT", "ARE", "WAS", "USE", "MAY", "CAN", "OUT", "OFF", "PDF", "USD", "EUR",
    "GBP", "TEL", "FAX", "ADT", "CHD", "INF", "MON", "TUE", "WED", "THU", "FRI",
    "SAT", "SUN", "JAN", "FEB", "MAR", "APR", "JUN", "JUL", "AUG", "SEP", "OCT",
    "NOV", "DEC",
}

#: Words that mean the document describes a journey rather than mentioning one.
#: Several languages, because a ticket is printed in the carrier's and the
#: passenger's.
_TRANSPORT_WORDS = re.compile(
    r"\b(flight|voo|vol|flug|vuelo|volo|train|comboio|coach|ferry|"
    r"depart|departure|partida|arrival|chegada|boarding|embarque|"
    r"check-?in|gate|terminal|airline|airways|airport|aeroporto|amtrak|rail)\b",
    re.I,
)


@dataclass
class Manifest:
    """What the document itself says about how many journeys it holds."""

    #: Journeys that can be named: service designators and coded routes. These
    #: are what a follow-up can ask for by name.
    named: list[str] = field(default_factory=list)
    #: Lower bound on the number of legs, from whichever signal saw most.
    expected: int = 0
    #: What each signal contributed, for the record and for explaining a
    #: warning to somebody who has to decide whether to believe it.
    signals: dict[str, list[str]] = field(default_factory=dict)
    #: Barcodes read, and pages seen. Context rather than counts — see below.
    barcodes: int = 0
    pages: int = 0

    def summary(self) -> str:
        parts = [f"{source}: {', '.join(found)}" for source, found in self.signals.items() if found]
        if self.barcodes:
            parts.append(f"barcodes: {self.barcodes}")
        if self.pages > 1:
            parts.append(f"pages: {self.pages}")
        return "; ".join(parts) or "nothing identifiable"


def looks_like_transport(text: str) -> bool:
    return bool(_TRANSPORT_WORDS.search(text))


#: Labels that say a code on this line is not a journey of its own.
#:
#: "EQUIPMENT: AIRBUS INDUSTRIE A330-200" is the aircraft. "OPERATED BY: KLM
#: ROYAL DUTCH AIRLINES, KL 1824" is the partner's number for a flight already
#: counted under the marketing carrier — one seat, one journey, two numbers,
#: and counting both asks the model for a leg that does not exist.
_NOT_A_JOURNEY = re.compile(
    r"\b(equipment|aircraft|aeronave|flugzeug|ger[äa]t|"
    r"operated\s+by|operado\s+por|op[ée]r[ée]\s+par|durchgef[üu]hrt\s+von|"
    r"marketed\s+by|codeshare|code\s?share)\b",
    re.I,
)


def _labelled_line(text: str, position: int) -> bool:
    """Does the line this code sits on say it is something other than a service?"""
    start = text.rfind("\n", 0, position) + 1
    return bool(_NOT_A_JOURNEY.search(text[start:position]))


def designators(text: str) -> list[str]:
    """Services named with a carrier code, e.g. S4246.

    This list becomes a checklist handed to the model — "each of these needs
    its own record" — which makes a false positive here expensive in the
    opposite direction to the one it was built for. Measured on a real Amadeus
    itinerary of four flights, it named eleven: three aircraft types, three
    codeshare numbers for flights already counted, and a figure out of a
    sentence about carbon emissions. The model was then asked to find eleven
    journeys, and duly produced seven.

    Each of those has a label sitting on the same line, so none of them needs
    guessing at.
    """
    found = set()
    for match in _DESIGNATOR_RE.finditer(text):
        before = text[max(0, match.start() - 14) : match.start()]
        if re.search(r"(terminal|class|classe|gate|seat|pc|kg|lbs?|eur|usd|tel|fax)\W{0,3}$",
                     before, re.I):
            continue
        if _labelled_line(text, match.start()):
            continue
        # "EMISSIONS IS 978.44 KG": the number runs on into a decimal, so it is
        # a quantity in a sentence rather than a service.
        if text[match.end() : match.end() + 1] in {".", ","} and text[
            match.end() + 1 : match.end() + 2
        ].isdigit():
            continue
        found.add(f"{match.group(1)}{int(match.group(2))}")
    for match in _SHORT_DESIGNATOR_RE.finditer(text):
        found.add(f"{match.group(1).upper()}{int(match.group(2))}")
    return sorted(found)


def routes(text: str) -> list[str]:
    """Journeys named by their endpoints, e.g. BBY-NYP.

    The signal that covers a service with no number of its own. A route is a
    weaker identity than a flight number — the same pair flown twice in a day
    collapses to one — so this only ever raises the expected count to the
    number of *distinct* routes, never above it.
    """
    found = set()
    for match in _ROUTE_RE.finditer(text):
        origin, separator, destination = match.group(1), match.group(2), match.group(3)
        if origin == destination:
            continue
        if separator not in _STRONG_SEPARATORS and (
            origin in _NOT_A_CODE or destination in _NOT_A_CODE
        ):
            continue
        found.add(f"{origin}-{destination}")
    return sorted(found)


def barcode_legs(payloads: list[str]) -> int:
    """Legs stated by boarding-pass barcodes themselves.

    IATA Resolution 792 puts the number of legs in the second character, so a
    pass beginning "M2" says outright that this booking has two. Machine
    written, so it beats anything read off the page — but it exists only for
    airline boarding passes, not for the QR code on a rail ticket, which
    encodes a booking reference and says nothing about legs.
    """
    total = 0
    for payload in payloads:
        if len(payload) > 1 and payload[0] == "M" and payload[1].isdigit():
            total += int(payload[1])
    return total


def read(text: str, barcode_payloads: list[str] | None = None, pages: int = 0) -> Manifest:
    """Everything the document says about its own size, before it is read.

    Barcodes and pages are recorded but deliberately do not raise the expected
    count. A rail ticket carries one QR code for a booking of any number of
    legs, and a receipt's second page is as likely to be conditions of carriage
    as another flight; letting either raise the expectation would hold correct
    submissions, which is the failure that makes a check worth ignoring.
    """
    payloads = barcode_payloads or []
    manifest = Manifest(barcodes=len(payloads), pages=pages)

    manifest.signals["services"] = designators(text)
    manifest.signals["routes"] = routes(text)

    legs_from_barcodes = barcode_legs(payloads)
    if legs_from_barcodes:
        manifest.signals["boarding passes"] = [f"{legs_from_barcodes} legs encoded"]

    manifest.named = sorted(set(manifest.signals["services"]) | set(manifest.signals["routes"]))
    manifest.expected = max(
        len(manifest.signals["services"]),
        len(manifest.signals["routes"]),
        legs_from_barcodes,
    )
    return manifest
