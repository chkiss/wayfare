# modelchain

Try a chain of models until one answers, and remember which ones are not worth
trying again yet.

Free and cheap model endpoints fail constantly, in ways that need different
responses. A rate limit clears in a minute. A spent free-tier window clears
when the window rolls over. A withdrawn model never comes back on its own.
Treating all three alike either hammers a struggling endpoint or disables a
working one for a day.

## What it does not do

No HTTP requests, no logging, no alerts, no configuration file. The caller
passes a function that attempts one model and says whether it worked. That is
what lets a long-running assistant and a stateless web service share it without
either inheriting the other's plumbing.

```python
from modelchain import JsonFileBench, run

def attempt(model):
    response = my_http_client.post(url, json={"model": model, ...})
    if response.status_code in (429, 503):
        return None, f"{response.status_code} from provider"
    response.raise_for_status()
    return response.json(), None

result = run(
    ["preferred/model", "fallback/one", "fallback/two"],
    attempt,
    bench=JsonFileBench("~/.local/state/myapp/models.json"),
    on_bench=lambda model, kind, error, seconds: log.warning("benched %s (%s)", model, kind),
)

if result.ok:
    use(result.value)      # result.model says which one answered
else:
    give_up(result.summary())
```

## The three kinds of failure

| Kind | Examples | Bench for |
| --- | --- | --- |
| `temporary` | timeout, 429, 502/503, connection reset | 2 minutes |
| `capped` | free usage exceeded, quota, needs credits | the provider's own retry hint, else a day |
| `gone` | 404, deprecated, 401, invalid key | until a human clears it |

An unrecognised error is `temporary` on purpose. Disabling a channel on
evidence we do not understand is worse than retrying it.

A `gone` bench never expires by itself — "disabled until someone looks" must
not quietly un-disable itself. `bench.restore(model)` puts it back.

## Discovering free models

`free_models(base_url)` asks an OpenAI-compatible `/models` endpoint which
models currently cost nothing, ordered by context length. Image, audio and
video models are filtered out; they are listed as free too and would be tried
and fail. A hard-coded list rots — models are withdrawn constantly, and when
the last one on a stale list disappears the application stops working for a
reason its user cannot see.

## Storage

`MemoryBench` for a process that does not outlive the request. `JsonFileBench`
for a service that should not forget a bench across a restart; it writes via a
temporary file, because a half-written state file reads as no bench at all and
would silently re-enable every channel.

Implement `Bench._load` and `Bench._save` for anything else.

## Vendored, not installed

Consumers vendor this with `git subtree`, so each one works standalone with no
install step and no network at deploy time:

```sh
git subtree add --prefix=path/to/modelchain <this-repo> main --squash
git subtree pull --prefix=path/to/modelchain <this-repo> main --squash   # later
```

The trade-off is deliberate: a fix here does not reach a consumer until someone
pulls it, and that is visible in a diff rather than surprising at runtime.

## Tests

```sh
pytest
```

No network, no keys, no fixtures beyond a fake clock.

## Licence

MIT.
