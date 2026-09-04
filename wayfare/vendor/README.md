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

## More than one endpoint

`Providers` holds the naming policy for a chain that spans several
OpenAI-compatible endpoints. A chain entry is either `"model"`, meaning the
default provider, or `"provider:model"` — split on the first colon and only
when the prefix names a configured provider, because model ids carry their own
colons (`"nous:tencent/hy3:free"` is provider `nous`, model
`tencent/hy3:free`).

```python
providers = Providers(default="zen")
providers.split("nous:tencent/hy3:free")   # ("nous", "tencent/hy3:free")
providers.base_url("big-pickle")           # https://opencode.ai/zen/v1
providers.spread({"zen": [...], "nous": [...]})   # interleaved, best first
```

Free tiers rotate, and the provider that is capped today is not the one that
will be capped tomorrow. Measured: a daily free-request cap was reached, every
model in a single-endpoint chain reported "rate limited", and the application
told its user it could not read the document — while a second free endpoint on
the same machine answered in under a second. `spread` interleaves the
providers rather than exhausting one catalogue and then the next, so a chain
does not spend itself inside one outage.

Zen, Nous and OpenRouter ship as defaults; anything can be overridden and
anything else added. An application written before providers existed passes
its single `base_url` and keeps working, with bare ids and no prefixes.

This module names endpoints, it does not call them — `key_file` gives the path
to a provider's key and the caller reads it. A library that reads secrets is a
library that has to be trusted with them.

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
