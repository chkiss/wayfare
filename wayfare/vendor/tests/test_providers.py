"""Naming models across more than one endpoint.

The cost of a single endpoint, measured: a daily free-request cap was reached,
every model in the chain came back "rate limited", and the application
reported that it could not read a document at all — while a second free
endpoint on the same machine answered in under a second.
"""

import pytest

from modelchain import DEFAULTS, Providers


def test_a_bare_entry_belongs_to_the_default_provider():
    providers = Providers(default="zen")
    assert providers.split("big-pickle") == ("zen", "big-pickle")


def test_a_prefixed_entry_names_its_provider():
    providers = Providers(default="zen")
    assert providers.split("nous:tencent/hy3:free") == ("nous", "tencent/hy3:free")


def test_a_model_id_may_contain_its_own_colons():
    """"tencent/hy3:free" is a model, not provider "tencent"."""
    providers = Providers(default="zen")
    assert providers.split("tencent/hy3:free") == ("zen", "tencent/hy3:free")


def test_an_unknown_prefix_is_part_of_the_model_id():
    """Otherwise a typo silently routes to the wrong endpoint."""
    providers = Providers(default="zen")
    assert providers.split("openai:gpt-4") == ("zen", "openai:gpt-4")


def test_the_default_provider_keeps_its_models_bare():
    providers = Providers(default="zen")
    assert providers.qualify("zen", "big-pickle") == "big-pickle"
    assert providers.qualify("nous", "hy3:free") == "nous:hy3:free"


# --- lookups ------------------------------------------------------------


def test_each_provider_has_its_own_endpoint():
    providers = Providers(default="zen")
    assert providers.base_url("big-pickle") == DEFAULTS["zen"]["api_base"]
    assert providers.base_url("nous:hy3:free") == DEFAULTS["nous"]["api_base"]


def test_a_provider_may_require_tags():
    """Nous rejects an untagged request outright."""
    providers = Providers(default="zen")
    assert providers.tags("nous:hy3:free")
    assert providers.tags("big-pickle") == []


def test_configuration_overrides_the_built_in_endpoint():
    providers = Providers({"zen": {"api_base": "http://localhost:8080/v1"}}, default="zen")
    assert providers.base_url("big-pickle") == "http://localhost:8080/v1"


def test_a_provider_nobody_shipped_can_be_added():
    providers = Providers(
        {"mine": {"api_base": "http://10.0.0.2:8000/v1"}}, default="mine"
    )
    assert providers.split("mine:qwen") == ("mine", "qwen")
    assert providers.base_url("mine:qwen") == "http://10.0.0.2:8000/v1"


def test_the_console_link_is_empty_rather_than_invented():
    """A dead link reads as an answer."""
    providers = Providers({"mine": {"api_base": "http://x/v1"}}, default="mine")
    assert providers.console_link("mine:qwen") == ""
    assert "portal.nousresearch.com" in providers.console_link("nous:hy3:free")


def test_the_key_is_named_not_read():
    """A library that reads secrets is one that has to be trusted with them."""
    providers = Providers({"nous": {"key_file": "/etc/nous.key"}}, default="zen")
    assert providers.key_file("nous:hy3:free") == "/etc/nous.key"


# --- the single-endpoint application ------------------------------------


def test_an_application_with_one_endpoint_keeps_working():
    """Written before providers existed: one base url, bare model ids."""
    providers = Providers(base_url="https://openrouter.ai/api/v1")
    assert providers.default == "openrouter"
    assert providers.split("google/gemma:free") == ("openrouter", "google/gemma:free")
    assert providers.qualify("openrouter", "google/gemma:free") == "google/gemma:free"


def test_an_endpoint_nobody_has_heard_of_still_becomes_the_default():
    providers = Providers(base_url="http://192.168.1.5:8082/v1")
    assert providers.base_url("qwen") == "http://192.168.1.5:8082/v1"


def test_one_key_file_covers_every_provider_that_has_none():
    providers = Providers(base_url="https://openrouter.ai/api/v1", key_file="/k")
    assert providers.key_file("google/gemma:free") == "/k"


# --- building the chain -------------------------------------------------


def test_providers_are_interleaved_rather_than_exhausted_in_turn():
    """The point of a second endpoint is that it is not having the same day as
    the first. Trying one catalogue to the end spends the whole chain inside
    one outage."""
    providers = Providers(default="zen")
    chain = providers.spread({"zen": ["a", "b", "c"], "nous": ["x", "y"]})
    assert chain == ["a", "nous:x", "b", "nous:y", "c"]


def test_a_provider_with_nothing_to_offer_is_skipped():
    providers = Providers(default="zen")
    assert providers.spread({"zen": ["a"], "nous": []}) == ["a"]
    assert providers.spread({}) == []
