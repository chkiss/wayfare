"""One chain, more than one endpoint.

A free tier that rotates is exactly the case where having two providers is
worth the plumbing. They serve overlapping but not identical catalogues — one
offers a model the other does not, and the one that is rate limited today is
not the one that will be rate limited tomorrow — so a chain confined to a
single endpoint inherits that endpoint's bad days as its own.

The cost of a single endpoint, measured: a daily free-request cap was reached,
every model in the chain came back "rate limited", and the application
reported that it could not read a document at all — while a second free
endpoint on the same machine was answering in under a second.

This module holds only the naming policy, in keeping with the rest of the
library: which providers exist, what a chain entry means, and where a
provider's key is kept. It opens no sockets and reads no files. The caller
resolves an entry to an endpoint and makes its own request.

A chain entry is either ``"model"``, meaning the default provider, or
``"provider:model"``. The split is on the first colon and only when the prefix
names a configured provider, because model ids carry their own colons:
``"nous:tencent/hy3:free"`` is provider ``nous``, model ``tencent/hy3:free``,
while ``"tencent/hy3:free"`` on its own is a model on the default provider.
"""

from __future__ import annotations

from typing import Any, Iterable

#: Endpoints known to serve an OpenAI-compatible free tier, so a consumer gets
#: working defaults without copying URLs between projects. Anything here can be
#: overridden, and a provider not listed here can simply be added.
#:
#: ``console_url`` is where a *person* goes when a provider needs one. A
#: benched model that tells its owner to "look at the config" names the one
#: place the answer is not: what a provider still serves lives on the
#: provider's own page.
DEFAULTS: dict[str, dict[str, Any]] = {
    "zen": {
        "api_base": "https://opencode.ai/zen/v1",
        "console_url": "https://opencode.ai/zen",
    },
    "nous": {
        "api_base": "https://inference-api.nousresearch.com/v1",
        "console_url": "https://portal.nousresearch.com",
        # Nous rejects an untagged request outright ("missing user tag").
        # These are attribution, not identity: nothing here names a person.
        "tags": ["client=modelchain"],
    },
    "openrouter": {
        "api_base": "https://openrouter.ai/api/v1",
        "console_url": "https://openrouter.ai/settings/credits",
    },
}


class Providers:
    """A registry of endpoints, and the rules for naming models across them."""

    def __init__(
        self,
        configured: dict[str, dict] | None = None,
        default: str | None = None,
        base_url: str | None = None,
        key_file: str | None = None,
    ) -> None:
        """Merge configured providers over the built-in ones.

        ``base_url`` is the compatibility path: an application written before
        providers existed has one endpoint and bare model ids, and keeps
        working. Its endpoint becomes the default provider, named after a
        built-in if it matches one.
        """
        merged: dict[str, dict[str, Any]] = {
            name: dict(conf) for name, conf in DEFAULTS.items()
        }
        for name, conf in (configured or {}).items():
            merged.setdefault(name, {}).update(conf or {})

        if not default and base_url:
            default = next(
                (n for n, c in merged.items() if c.get("api_base") == base_url), None
            )
            if default is None:
                merged["default"] = {"api_base": base_url}
                default = "default"

        if key_file:
            for conf in merged.values():
                conf.setdefault("key_file", key_file)

        self._providers = merged
        self.default = default if default in merged else next(iter(merged), "")

    # -- naming ----------------------------------------------------------

    def __contains__(self, name: str) -> bool:
        return name in self._providers

    def names(self) -> list[str]:
        return list(self._providers)

    def split(self, entry: str) -> tuple[str, str]:
        """``"nous:tencent/hy3:free"`` -> ``("nous", "tencent/hy3:free")``."""
        name, separator, rest = (entry or "").partition(":")
        if separator and name in self._providers and rest:
            return name, rest
        return self.default, entry

    def qualify(self, provider: str, model: str) -> str:
        """The chain entry for a model on a named provider.

        The default provider's models stay bare, so a single-endpoint
        application never sees a prefix it did not ask for.
        """
        if provider == self.default or not provider:
            return model
        return f"{provider}:{model}"

    # -- lookups ---------------------------------------------------------

    def conf(self, name: str | None = None) -> dict[str, Any]:
        return self._providers.get(name or self.default) or {}

    def base_url(self, entry: str) -> str:
        """The endpoint a chain entry belongs to."""
        return self.conf(self.split(entry)[0]).get("api_base", "")

    def key_file(self, entry: str) -> str:
        """Where this provider's key is kept, for the caller to read.

        A path rather than a key: this module does no I/O, and a library that
        reads secrets is a library that has to be trusted with them.
        """
        return self.conf(self.split(entry)[0]).get("key_file", "")

    def tags(self, entry: str) -> list[str]:
        return list(self.conf(self.split(entry)[0]).get("tags") or [])

    def console_url(self, entry: str) -> str:
        return self.conf(self.split(entry)[0]).get("console_url", "")

    def console_link(self, entry: str) -> str:
        """``"nous — https://…"``, or empty when the provider has no page.

        Empty rather than invented: a dead link reads as an answer.
        """
        provider, _ = self.split(entry)
        url = self.conf(provider).get("console_url")
        return f"{provider} — {url}" if url else ""

    # -- building a chain ------------------------------------------------

    def spread(self, per_provider: dict[str, Iterable[str]]) -> list[str]:
        """Interleave each provider's models into one chain, best first.

        Round-robin rather than provider by provider, because the point of a
        second endpoint is that it is *not* having the same day as the first.
        Exhausting one provider's whole catalogue before trying the other
        spends the entire chain inside one outage.
        """
        queues = {
            name: [self.qualify(name, model) for model in models]
            for name, models in per_provider.items()
            if models
        }
        chain: list[str] = []
        while queues:
            for name in list(queues):
                chain.append(queues[name].pop(0))
                if not queues[name]:
                    del queues[name]
        return chain
