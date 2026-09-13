"""schema.org markup, which the operator publishes about its own booking.

The one extractor here that never had to be generalised: it reads a published
standard rather than an operator's layout, so it serves carriers nobody has
heard of and does not drift when a carrier redesigns its ticket.

The awkward cases are all real publisher errors from KItinerary's corpus, and
none of them are about a particular airline — they are about what sites
actually ship when they mean to ship schema.org.
"""

from wayfare.extractors import structured
from wayfare.schema import Kind


def page(body: str) -> str:
    return f'<html><head><script type="application/ld+json">{body}</script></head></html>'


FLIGHT = page(
    """
{
  "@context": "http://schema.org",
  "@type": "FlightReservation",
  "reservationNumber": "RXJ34P",
  "reservationFor": {
    "@type": "Flight",
    "flightNumber": "110",
    "airline": {"@type": "Airline", "name": "United", "iataCode": "UA"},
    "departureAirport": {"@type": "Airport", "name": "San Francisco", "iataCode": "SFO"},
    "departureTime": "2027-03-04T20:15:00-08:00",
    "arrivalAirport": {"@type": "Airport", "name": "John F. Kennedy", "iataCode": "JFK"},
    "arrivalTime": "2027-03-05T04:55:00-05:00"
  }
}
"""
)


def test_a_published_reservation_is_read_without_interpretation():
    (leg,) = structured.extract(FLIGHT, "booking.html")

    assert leg.kind is Kind.FLIGHT
    assert (leg.carrier, leg.number) == ("UA", "110")
    assert leg.confirmation == "RXJ34P"
    assert (leg.origin.iata, leg.destination.iata) == ("SFO", "JFK")


def test_the_offset_is_read_and_then_dropped():
    """An itinerary shows the time on the departure board.

    The offset says what the local time was, not what zone the airport keeps,
    so it fixes the reading and is then discarded.
    """
    (leg,) = structured.extract(FLIGHT, "booking.html")

    assert leg.departure.local.hour == 20
    assert leg.departure.local.tzinfo is None


def test_a_missing_comma_between_objects_does_not_lose_the_booking():
    """One airline ships both its flights in one block and forgets the comma.

    Refusing the block loses two real flights to a single missing character.
    """
    two = page(
        '[{"@context":"http://schema.org","@type":"FlightReservation",'
        '"reservationNumber":"XXX007","reservationFor":{"@type":"Flight",'
        '"flightNumber":"8102","departureTime":"2018-02-02T18:35:00"}}'
        '{"@context":"http://schema.org","@type":"FlightReservation",'
        '"reservationNumber":"XXX007","reservationFor":{"@type":"Flight",'
        '"flightNumber":"8103","departureTime":"2018-02-04T20:40:00"}}]'
    )
    first, second = structured.extract(two, "ew.html")
    assert (first.number, second.number) == ("8102", "8103")


def test_both_editions_of_the_lodging_vocabulary_are_read():
    """schema.org renamed these; sites use whichever they were written against."""
    hotel = page(
        """
{
  "@context": "http://schema.org",
  "@type": "LodgingReservation",
  "reservationNumber": "KDEKDEKDE",
  "checkinDate": "2017-06-15T14:00:00+0:00",
  "checkoutDate": "2017-06-18T11:00:00+0:00",
  "reservationFor": {"@type": "LodgingBusiness", "name": "Parser & Breaking Hotels"}
}
"""
    )
    (stay,) = structured.extract(hotel, "hotel.html")

    assert stay.kind is Kind.LODGING
    assert stay.property_name == "Parser & Breaking Hotels"
    # "+0:00" is not a legal offset either, and the booking is not worth
    # losing over it.
    assert stay.check_in.local.day == 15
    assert stay.check_out.local.day == 18


def test_a_date_that_is_not_iso_is_left_to_the_model():
    """"02-02-2018 18:35" does not say whether "02-04" is April or February.

    These records outrank anything read off the page, so a guess here would
    overwrite a correct reading with a confident wrong one.
    """
    ambiguous = page(
        '{"@context":"http://schema.org","@type":"FlightReservation",'
        '"reservationFor":{"@type":"Flight","flightNumber":"8102",'
        '"departureTime":"02-02-2018 18:35"}}'
    )
    assert structured.extract(ambiguous, "ew.html") == []


def test_a_page_describing_a_hotel_is_not_a_booking():
    """`Hotel` and `Event` markup describe something; they confirm nothing."""
    brochure = page(
        '{"@context":"http://schema.org","@type":"Hotel",'
        '"name":"Discover a 4 star hotel in the heart of Paris."}'
    )
    assert structured.extract(brochure, "brochure.html") == []


def test_a_reservation_nested_in_the_page_graph_is_still_found():
    """Publishers ship a bare object, a list, or a @graph. Walking beats listing."""
    graph = page(
        '{"@context":"http://schema.org","@graph":[{"@type":"WebPage"},'
        '{"@type":"FlightReservation","reservationNumber":"ABC123",'
        '"reservationFor":{"@type":"Flight","flightNumber":"42",'
        '"departureTime":"2027-01-01T09:00:00"}}]}'
    )
    (leg,) = structured.extract(graph, "graph.html")
    assert leg.number == "42"


MICRODATA = """\
<html><body>
<div itemscope itemtype="http://schema.org/FlightReservation">
  <meta itemprop="reservationNumber" content="XXX007" />
  <div itemprop="underName" itemscope itemtype="http://schema.org/Person">
    <meta itemprop="name" content="VOLKER" />
  </div>
  <div itemprop="reservationFor" itemscope itemtype="http://schema.org/Flight">
    <meta itemprop="flightNumber" content="8588" />
    <div itemprop="airline" itemscope itemtype="http://schema.org/Airline">
      <meta itemprop="name" content="OPERADOR AIR NOSTRUM" />
      <meta itemprop="iataCode" content="IB" />
    </div>
    <div itemprop="departureAirport" itemscope itemtype="http://schema.org/Airport">
      <meta itemprop="name" content="MADRID" />
      <meta itemprop="iataCode" content="MAD" />
    </div>
    <meta itemprop="departureTime" content="2017-07-20T17:50:00+02:00" />
    <div itemprop="arrivalAirport" itemscope itemtype="http://schema.org/Airport">
      <meta itemprop="iataCode" content="LEI" />
    </div>
  </div>
</div>
</body></html>
"""


def test_the_same_standard_spelled_as_html_attributes_is_read():
    (leg,) = structured.extract(MICRODATA, "ib.html")

    assert (leg.carrier, leg.number) == ("IB", "8588")
    assert (leg.origin.iata, leg.destination.iata) == ("MAD", "LEI")
    assert leg.confirmation == "XXX007"
    assert leg.departure.local.hour == 17


def test_a_nested_scope_does_not_close_its_parent():
    """`underName` and `airline` are objects inside the reservation.

    Scopes are closed by counting tags, not by matching names: an itemscope
    div closes several divs later, and matching on the name would close it at
    the first one — taking the flight with it.
    """
    (leg,) = structured.extract(MICRODATA, "ib.html")
    assert leg.number == "8588"  # survived two nested scopes before it


def test_several_reservations_on_one_page_stay_separate():
    two = MICRODATA.replace("</body>", MICRODATA.split("<body>")[1].split("</body>")[0]
                            .replace("8588", "8589").replace("MAD", "LEI2") + "</body>")
    assert len(structured.extract(two, "two.html")) == 2


def test_a_property_value_can_be_the_text_on_the_page():
    """Not every value hides in a `content` attribute."""
    visible = """\
<div itemscope itemtype="http://schema.org/LodgingReservation">
  <span itemprop="reservationNumber">ABC123</span>
  <meta itemprop="checkinTime" content="2027-06-15T14:00:00" />
  <meta itemprop="checkoutTime" content="2027-06-18T11:00:00" />
  <div itemprop="reservationFor" itemscope itemtype="http://schema.org/LodgingBusiness">
    <span itemprop="name">Hotel Konqi</span>
  </div>
</div>
"""
    (stay,) = structured.extract(visible, "hotel.html")
    assert stay.confirmation == "ABC123"
    assert stay.property_name == "Hotel Konqi"


def test_markup_that_does_not_balance_still_yields_its_booking():
    """One corpus page ships an unterminated `<base href="...">` and says so."""
    broken = MICRODATA.replace("<body>", '<body><base href="http://www.iberia.com">')
    (leg,) = structured.extract(broken, "ib.html")
    assert leg.number == "8588"


def test_a_page_with_no_markup_is_left_alone():
    assert structured.extract("<html><body>Dear passenger</body></html>", "x.html") == []
    assert not structured.looks_like_structured("Dear passenger")
