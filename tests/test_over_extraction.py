"""More journeys than the document describes.

Everything else here guards against a leg going missing. This is the opposite
failure, and nothing looked for it: measured on a four-flight Amadeus
itinerary, one reading returned seven records — three flights nobody has a
seat on, heading for a calendar.

The cause was this project's own checklist. The scan that counts journeys
before anyone reads named eleven services on that page: three aircraft types,
three codeshare numbers for flights already counted, and a figure out of a
sentence about carbon emissions. The model was handed those eleven and asked
to find a record for each.
"""

from datetime import datetime

import pytest

from wayfare import manifest
from wayfare.schema import (
    FlightRecord,
    Itinerary,
    LocalTime,
    Place,
    Provenance,
)
from wayfare.validate import completeness


AMADEUS = """
  AIR FRANCE                    DL 9520      26 SEP 2018
           OPERATED BY:              KLM ROYAL DUTCH AIRLINES, KL 1824
           EQUIPMENT:                AIRBUS INDUSTRIE A330-200
  DELTA AIR LINES               DL 139       26 SEP 2018
           EQUIPMENT:                AIRBUS INDUSTRIE A340-300
  DELTA AIR LINES               DL 8573      03 OCT 2018
           OPERATED BY:              AIR FRANCE, AF 1534
           EQUIPMENT:                AIRBUS INDUSTRIE A321
  DELTA AIR LINES               DL 8680      03 OCT 2018
FLIGHT(S) CALCULATED AVERAGE CO2 EMISSIONS IS 978.44 KG/PERSON
"""


# --- the checklist that caused it ---------------------------------------


def test_the_aircraft_is_not_a_flight():
    """"EQUIPMENT: AIRBUS INDUSTRIE A330-200" is what you sit in."""
    named = manifest.designators(AMADEUS)
    assert not [code for code in named if code.startswith("A3")]


def test_the_operating_carriers_number_is_not_a_second_leg():
    """One seat, one journey, two numbers. Counting both asks for a leg that
    does not exist."""
    named = manifest.designators(AMADEUS)
    assert "KL1824" not in named
    assert "AF1534" not in named


def test_a_quantity_in_a_sentence_is_not_a_service():
    """"EMISSIONS IS 978.44 KG" — the number runs on into a decimal."""
    assert "IS978" not in manifest.designators(AMADEUS)


def test_the_real_flights_are_all_still_named():
    assert manifest.designators(AMADEUS) == ["DL139", "DL8573", "DL8680", "DL9520"]


def test_the_expected_count_matches_the_ticket():
    assert manifest.read(AMADEUS).expected == 4


# --- the guard, for when a model invents anyway --------------------------


def flight(number, day=26):
    return FlightRecord(
        carrier="DL",
        number=number,
        origin=Place(iata="LHR"),
        destination=Place(iata="JFK"),
        departure=LocalTime(local=datetime(2018, 9, day, 9, 0)),
        provenance=Provenance(extractor="llm"),
    )


def itinerary_of(*numbers):
    it = Itinerary(records=[flight(n) for n in numbers])
    it.source_text["amadeus.txt"] = AMADEUS
    return it


def test_a_flight_the_document_never_names_is_held():
    it = completeness.run(itinerary_of("9520", "4242"))
    invented = [r for r in it.records if r.number == "4242"]
    assert "leg.not_named_in_document" in [i.code for i in invented[0].issues]


def test_the_flights_that_are_named_are_not_held():
    it = completeness.run(itinerary_of("9520", "139"))
    for record in it.records:
        assert "leg.not_named_in_document" not in [i.code for i in record.issues]


def test_another_airlines_flight_is_not_second_guessed():
    """The scan reads one carrier's numbers off a page; a partner's leg it
    never saw is a gap in the scan, not an invention."""
    it = itinerary_of("9520")
    it.records.append(
        FlightRecord(
            carrier="BA",
            number="117",
            origin=Place(iata="LHR"),
            destination=Place(iata="JFK"),
            departure=LocalTime(local=datetime(2018, 9, 26, 9, 0)),
            provenance=Provenance(extractor="llm"),
        )
    )
    completeness.run(it)
    assert "leg.not_named_in_document" not in [i.code for i in it.records[-1].issues]


def test_a_document_naming_no_services_holds_nothing():
    it = Itinerary(records=[flight("9520")])
    it.source_text["ticket.txt"] = "Your trip is confirmed. Have a good journey."
    completeness.run(it)
    assert "leg.not_named_in_document" not in [i.code for i in it.records[0].issues]
