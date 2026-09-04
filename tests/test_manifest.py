"""Counting the journeys in a document from several independent signals.

Both documents here are real. Each one defeats one of the signals, which is
why there is more than one.
"""

from wayfare import manifest

# Names its services (S4246, S4120); writes its routes as full airport names.
SATA = """
From                        To                      Flight      Departure   Arrival
NEW YORK JOHN F KENNEDY     PONTA DELGADA JOAO      S4246       20:55       06:45
INTL                        PAULO II                            20Sep2026   21Sep2026
Terminal / Terminal: 1

PONTA DELGADA JOAO PAULO II LISBON AIRPORT          S4120       08:25       11:40
                            Terminal / Terminal: 1              23Sep2026   23Sep2026
"""

# Names its route (BBY » NYP); its service number is a bare "85".
AMTRAK = """
RES# BBDO3F-O7AUG26
BBY » NYP One-Way
Boston, MA New York, NY SEPTEMBER 8, 2026
Back Bay Station Moynitan Train Hall at Penn Sta
TRAIN NORTHEAST REGIONAL, DEPARTS. ARRIVES
85 Sep 8, 2026 10:26 AM 2:29 PM
Bagagem: 1PC, check-in 30 minutes, 45 minutes with baggage
"""


# --- each document defeats one signal -----------------------------------


def test_the_flight_receipt_is_counted_by_its_service_numbers():
    found = manifest.read(SATA)
    assert found.signals["services"] == ["S4120", "S4246"]
    assert found.expected == 2


def test_the_flight_receipt_has_no_coded_routes_to_count():
    """It spells "PONTA DELGADA JOAO PAULO II" out in full."""
    assert manifest.read(SATA).signals["routes"] == []


def test_the_rail_ticket_is_counted_by_its_route():
    """Its service number is a bare "85" with no carrier code to find."""
    found = manifest.read(AMTRAK)
    assert found.signals["routes"] == ["BBY-NYP"]
    assert found.expected == 1


def test_the_rail_ticket_has_no_designator_to_count():
    assert manifest.read(AMTRAK).signals["services"] == []


def test_a_time_of_day_is_still_not_a_service():
    """"10:26 AM 2:29 PM" read as AM2, and Amtrak's carrier code is AM."""
    assert "AM2" not in manifest.read(AMTRAK).named


def test_the_baggage_allowance_is_not_a_journey():
    """Counting bare numbers yields 85, and also 1, 30 and 45."""
    assert manifest.read(AMTRAK).expected == 1


# --- single-digit flight numbers ----------------------------------------


def test_a_single_digit_flight_number_is_found_after_the_word_flight():
    assert "QF8" in manifest.read("Flight QF 8 to Sydney, departs 21:30").named


def test_a_single_digit_number_needs_that_word():
    """Without it, every "AM 2" and "PM 5" on every page becomes a flight."""
    assert manifest.read("Boarding 10:26 AM 2 minutes early").named == []


def test_a_partner_earning_table_is_not_a_journey_list():
    text = "Flight BA 117 JFK\nEarn Avios on AA 100, IB 342\nSee ba.com"
    assert manifest.read(text).signals["services"] == ["AA100", "BA117", "IB342"]


# --- barcodes ------------------------------------------------------------


def test_a_boarding_pass_states_its_own_leg_count():
    """IATA 792 puts the number of legs in the second character."""
    assert manifest.barcode_legs(["M2DESMARAIS/LUC       EABC123 YULFRAAC 0834 326J004A00025100"]) == 2


def test_two_boarding_passes_are_two_legs():
    assert manifest.barcode_legs(["M1AAA", "M1BBB"]) == 2


def test_a_rail_qr_code_says_nothing_about_legs():
    """It encodes a booking reference. Counting it as a leg would be a guess."""
    assert manifest.barcode_legs(["BBD03F-07AUG26"]) == 0


def test_the_encoded_leg_count_raises_the_expectation():
    found = manifest.read(AMTRAK, barcode_payloads=["M2SMITH/J          EABC123 BOSNYPAM 0085 251Y"])
    assert found.expected == 2


def test_barcodes_are_recorded_even_when_they_count_for_nothing():
    found = manifest.read(AMTRAK, barcode_payloads=["BBD03F-07AUG26"])
    assert found.barcodes == 1
    assert found.expected == 1  # The QR code did not raise it.


# --- pages ---------------------------------------------------------------


def test_pages_are_recorded_but_never_counted_as_legs():
    """A second page is as likely to be conditions of carriage as a flight."""
    found = manifest.read(SATA, pages=4)
    assert found.pages == 4
    assert found.expected == 2


def test_the_summary_says_where_each_number_came_from():
    summary = manifest.read(SATA, barcode_payloads=["M1AAA"], pages=2).summary()
    assert "services: S4120, S4246" in summary
    assert "barcodes: 1" in summary
    assert "pages: 2" in summary


# --- routes are a weaker identity than a number -------------------------


def test_a_route_flown_twice_counts_once():
    """Two identical routes cannot be told apart, so this never over-counts."""
    text = "LHR - JFK on Monday\nLHR - JFK on Friday"
    assert manifest.read(text).signals["routes"] == ["LHR-JFK"]


def test_a_dash_between_two_words_is_not_a_route():
    assert manifest.read("NON-STOP SEE THE-FOR details").signals["routes"] == []


def test_a_place_to_itself_is_not_a_route():
    assert manifest.read("JFK - JFK").signals["routes"] == []


def test_an_arrow_beats_the_list_of_words_that_are_not_codes():
    """WAS is a word and also Washington. After an arrow it is Washington."""
    assert manifest.read("NYP » WAS").signals["routes"] == ["NYP-WAS"]


def test_a_hyphen_still_defers_to_that_list():
    assert manifest.read("THE-WAS").signals["routes"] == []


def test_two_rail_legs_are_two_routes():
    text = "BOS » NYP  10:26\nNYP » WAS  15:05\nTRAIN NORTHEAST REGIONAL"
    assert manifest.read(text).signals["routes"] == ["BOS-NYP", "NYP-WAS"]
