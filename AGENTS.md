# Operating wayfare as an agent

Instructions for an automated agent submitting travel documents. Written to be
pasted into an agent's system prompt or read by it directly.

## What you can and cannot do

With the agent token you can submit a document and read back the result. You
cannot promote an event to the real calendar, delete anything, or read existing
calendar entries. Everything you submit lands in a quarantine calendar for a
human to confirm. There is no flag, header or phrasing that changes this, so do
not look for one.

## Submitting

```sh
curl -sS -X POST https://wayfare.example.com/api/v1/ingest \
  -H "Authorization: Bearer $WAYFARE_AGENT_TOKEN" \
  -F upload=@/path/to/confirmation.png
```

Text instead of a file:

```sh
curl -sS -X POST https://wayfare.example.com/api/v1/ingest \
  -H "Authorization: Bearer $WAYFARE_AGENT_TOKEN" \
  --form-string text="$BOOKING_TEXT"
```

A whole trip in one request, by repeating `upload`:

```sh
curl -sS -X POST https://wayfare.example.com/api/v1/ingest \
  -H "Authorization: Bearer $WAYFARE_AGENT_TOKEN" \
  -F upload=@outbound.pdf -F upload=@return.pdf -F upload=@hotel.eml
```

Send everything belonging to one trip together. The checks that catch real
mistakes are the ones comparing documents — a hotel booked for the wrong month
looks perfectly consistent on its own, and only contradicts something once the
flights are in front of it. Splitting a round trip across two requests loses
that. `upload` and `text` can both appear in the same request.

Separate trips go in separate requests.

If you have a five-page itinerary PDF, send the PDF rather than five
screenshots of it: the extractor reads a text layer far more accurately than
it reads pixels.

## Reading the result

```json
{
  "submission_id": "8f2a1c04d3e1",
  "source_file": "confirmation.png",
  "summary": { "promoted": 0, "pending": 1, "rejected": 0 },
  "records": [
    {
      "summary": "BA117 LHR → JFK",
      "confidence": 0.62,
      "status": "pending",
      "reason": "Held for review — OCR confidence was only 61%...",
      "issues": [ { "level": "warn", "code": "ocr.low_confidence", "message": "..." } ]
    }
  ]
}
```

`status` is `promoted`, `pending` or `rejected`. With an agent token it will
never be `promoted`, because you are not permitted to promote.

## Rules

**Report the status you were given.** If a record came back `pending`, say it
is waiting for confirmation. Do not describe it as added, scheduled or done. A
false "already on your calendar" is worse than any parse error, because it
stops anyone from checking.

**Never retype a value to make it look better.** If the tool reports a
departure time you believe is wrong, say so and quote what it reported. Do not
resubmit with a corrected time you worked out yourself. You may be wrong, and
the tool's whole design assumes the model in the loop might be.

**Pass `rejected` records back to the human with the reason.** A rejection
means the document contradicts itself or physics. That is information about
the document, and usually means the screenshot was cropped, blurred, or is of
something other than a booking.

**An empty result is not a success.** `"records": []` means nothing was
extracted. Report that, and say which file it was, rather than moving on.

**A failed request is not an absent booking.** On a non-2xx response, or no
response, report the failure. Do not conclude the document had no travel in it.

**Do not put booking references, seat numbers, addresses or traveller names
into logs, commit messages, or anywhere else outside the request itself.**

## If it fails

| Status | Meaning | What to do |
| --- | --- | --- |
| 401 | Token missing or wrong | Stop. Ask the human. Do not try another token. |
| 403 | You attempted an owner-only action | Stop. That action is not available to you. |
| 413 | File over 20 MB | Downscale an image, or send the original PDF. |
| 400 | Neither a file nor text was sent | Check the form field names above. |
| 503 | Server not configured or Google auth expired | Stop. Ask the human. |

Never retry a 401 or 403 with a different credential. Repeated failed
authentication looks like an attack and can get the source address blocked.
