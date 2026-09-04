# wayfare

Feed it a screenshot of a booking confirmation, a PDF, a boarding pass or a
pasted email, and it puts the flight, train or hotel on your Google Calendar,
after checking that what it read is actually possible.

Plenty of tools will hand a booking document to a language model and write
whatever comes back onto your calendar. That works until the model reads 09:35
as 19:35, and then you have a wrong departure time in the one place you will
trust without looking twice.

## How it avoids putting a wrong time in your calendar

**The model never reads the document.** `tesseract` and `pdftotext` extract the
text, give the same answer every time, and report a per-word confidence. The
model's only job is to say which parts of that text are a flight and which are a
hotel. A small free model does that reliably.

**Every value must be quoted from the source.** For each field it fills in, the
model has to return the exact substring it took the value from. Each quote is
checked against the extracted text, and any field it cannot quote is thrown away
before a record is even built. A model that invents a departure time cannot
produce evidence for it, so the invention never reaches your calendar.

**A document can be read twice, and compared.** Quoting catches a wrong value,
but the usual failure of a free model is an *absent* one: a flight number left
out, a leg of a two-flight receipt skipped, occasionally a whole document
returning nothing at all. Those readings are consistent with the source, so no
amount of checking against the source finds them. A second reading rarely drops
the same field, so its answer fills the gap. Agreement is the only positive
evidence in the pipeline; every other check can establish only that a record is
not contradicted.

Set `WAYFARE_LLM_QUORUM=2` to turn it on. It is off by default because on a
free tier the second reading is not free of consequence: measured over six
documents in one afternoon, a quorum of two read 12 legs of 22 and a quorum of
one read 17 — the extra calls spent the daily allowance of the only provider
still answering, and the later documents got nothing at all. A cross-check that
costs a whole document is not worth making. Raise it where the budget is real:
a paid tier, a local endpoint, or a day with two providers answering.

Where the two readings differ, the model that read the document is asked which
value the source supports, as a continuation of the same conversation, so it
answers with the document and its own reading still in view. It may only pick
from the two values already on the table, and it has to quote the line that
decides it — a ruling it cannot point to is an opinion, and the dispute stands.
Anything left standing becomes a choice on the review page, because the person
holding the ticket can simply look.

The two readings come from different models when the backend has two to spare.
Where only one model is answering it is asked twice instead, which catches the
same failure because these models are not deterministic even at temperature
zero — but sequentially, after the first answer, never as two simultaneous
requests to one endpoint. The event says which it was.

**Boarding pass barcodes outrank everything.** The barcode on a boarding pass is
a fixed-width string written by the airline's own system: route, flight number,
date, seat, booking reference. No OCR, no interpretation. When a barcode is
present it becomes the reference the rest of the document is checked against,
and any disagreement is reported rather than resolved silently.

**The itinerary has to be physically possible.** Airport coordinates and
timezones come from the OurAirports database, offline. From those, the tool
knows how far the flight actually is and how long it therefore has to take. A
misread hour, a swapped am/pm, a wrong timezone, or origin and destination read
backwards all produce a duration that no aircraft flies, and get caught with no
API and no network.

**The records have to agree with each other.** A check-in before you land, a
check-out after you have flown home, a connection that departs before the
previous leg arrives, a hotel stay that runs for eleven months because a year
was misread. None of these can be caught by looking at one document, so the
records are compared to each other and to what is already on your calendar.

**Nothing is written irreversibly.** Every event is created on a separate
"Travel (pending)" calendar first. Anything that passes every check is moved to
your real calendar automatically; anything with a warning stays in pending until
you tap confirm. Every write is logged with its event id, so `wayfare undo`
reverses it.

## What it costs to run

Nothing, and it stays that way. OCR, barcode decoding, the airport database and
every validator are local and free. The one component that talks to a paid-ish
service is the model backend, which is an OpenAI-compatible endpoint you choose.
A free tier is adequate, because the model is only classifying text that a
deterministic tool already extracted.

The live flight-schedule check is the exception, and it is optional. Turned on,
it confirms that the flight number really does fly at that time. Turned off (the
default), everything else still works.

## Install

```sh
git clone https://github.com/<you>/wayfare
cd wayfare
./deploy/setup.sh
```

Then the system packages, which need root — see
[`deploy/ROOT_STEPS.md`](deploy/ROOT_STEPS.md):

```sh
sudo apt install -y tesseract-ocr zbar-tools poppler-utils
```

Then start it and open `/setup` in a browser. That page links straight to the
two Google Cloud Console pages you need, shows the exact redirect URI to paste,
takes the downloaded JSON as an upload, and runs the consent flow. You never
have to find a file path.

```sh
wayfare serve --port 8791     # then open http://127.0.0.1:8791/setup
```

That page also takes the model API key, links to OpenRouter's key page, and
makes one real request to prove the key works before calling it set up. A key
that is merely *present* proves nothing, and finding out it was revoked while
reading a boarding pass at an airport is too late.

Google insists that every app use its own OAuth client, so the two Console steps
cannot be automated away. Everything after them is.

If you would rather stay in the terminal, `wayfare auth` does the same thing
with a Desktop-type client.

Check what is actually wired up:

```sh
wayfare doctor
```

## Use

Read a document and print the verdict, writing nothing:

```sh
wayfare parse ~/Downloads/boarding-pass.pdf
```

```
boarding-pass.pdf — 8f2a1c04d3e1
  1 added · 0 held · 0 rejected

  [0] ADDED     BA117 LHR → JFK  (97%)
       All checks passed (confidence 97%).
```

Write it to the calendar:

```sh
wayfare add ~/Downloads/hotel.png
wayfare undo                      # if it got something wrong
```

Run the review site:

```sh
wayfare serve --port 8791
```

## The microsite

`wayfare serve` gives you a page to drop a screenshot on and a review screen
showing what was read, what was checked, and what is waiting for confirmation.
For a real deployment put it behind nginx with TLS; the app binds to localhost
and is never exposed directly. Configuration is in
[`deploy/nginx-wayfare.conf`](deploy/nginx-wayfare.conf) and the systemd user
unit in [`deploy/wayfare.service`](deploy/wayfare.service).

## Letting an agent operate it

The HTTP API has two privilege levels.

The **agent token** can do exactly one thing: submit a document and read back
what happened to it. It cannot promote an event to your real calendar, cannot
delete anything, and cannot read your existing events. Everything it submits
lands in the pending calendar regardless of how clean it looks.

```sh
curl -H "Authorization: Bearer $WAYFARE_AGENT_TOKEN" \
     -F upload=@outbound.pdf -F upload=@return.pdf -F upload=@hotel.eml \
     https://wayfare.example.com/api/v1/ingest
```

Repeat `upload` to send a whole trip at once. A hotel booked for the wrong month
looks fine on its own and only contradicts something when the flights are
checked alongside it.

The **owner token** can do everything, including promote and undo.

The agent token is the credential most likely to leak, because it lives in an
environment variable on some other machine. Scoped this way, a leak costs you
some junk events in a calendar you can delete, and nothing else. See
[`AGENTS.md`](AGENTS.md) for the operating instructions to hand an agent.

## Calendar conventions

Titles and descriptions are personal taste, so they live in one JSON file rather
than in the code. Copy `conventions.example.json` somewhere outside the repo and
point `WAYFARE_CONVENTIONS` at it. Nothing personal is ever committed: no
itinerary, no address, no booking reference, no credential.

You can also let the tool read how you already do it, instead of editing the
file by hand. Export your calendar from Google Calendar (Settings → Import &
export → Export) and point `learn` at the download.

```sh
wayfare learn ~/Downloads/calendar-export.zip --write ~/.config/wayfare/conventions.json
```

```
Read 113 events.
  flights 73 · lodging 38 · rail 1

What your calendar does:
  Route separator              → 100%   (n=72)
  Flight number style          joined-leading 100%   (n=72)
  Flight title prefix          ✈ 99%, (none) 1%   (n=73)
  Hotel event shape            span 100%   (n=38)
  Confirmation in description  True 100%   (n=72)
```

It reports how consistent each pattern was and says so when the sample is too
thin to conclude anything, rather than presenting a guess from four events as a
convention. The export never leaves your machine.

## Shared code

The model fallback policy — which failures are temporary, which need a person,
and how long to stay away — lives in
[modelchain](https://github.com/chkiss/modelchain) and is vendored here as a git
subtree at `wayfare/vendor`, so this repo clones and runs with nothing to fetch.
Update it with:

```sh
git remote add modelchain https://github.com/chkiss/modelchain.git
git subtree pull --prefix=wayfare/vendor modelchain main --squash
```

## Tests

```sh
pytest
```

The suite covers barcode decoding, the physical-plausibility checks, the
itinerary cross-checks, and the evidence rule that keeps the model honest. It
needs no network and no keys.

## Licence

MIT.

Airport data from [OurAirports](https://ourairports.com/data/) (public domain).
Airline names from [OpenTravelData](https://github.com/opentraveldata/opentraveldata)
(CC-BY). European station names, UIC codes and timezones from
[Trainline](https://github.com/trainline-eu/stations) (ODbL — modifications to
the data must be published, so nothing here modifies it). US stations from
[Amtrak's published GTFS feed](https://content.amtrak.com/content/gtfs/GTFS.zip),
which is where "NYP" becomes Penn Station and its timezone. Optional document
parsing by [KItinerary](https://invent.kde.org/pim/kitinerary) (LGPL).

None of those datasets is committed here. `wayfare fetch-airports` and
`wayfare fetch-reference` download them into the data directory, which keeps
this a program that reads other people's data rather than a copy of it, and
keeps the tables current.
