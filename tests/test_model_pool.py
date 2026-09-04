"""Which models are worth asking at all."""

import pytest

import wayfare.config as config
from wayfare.extractors import llm

def test_models_that_refuse_this_kind_of_client_are_not_offered(monkeypatch):
    """Both inkling models answer every request with
    "403 ... is only available on agentic harnesses".

    The bench is for a model that is temporarily unavailable; these are
    permanently unavailable to us, so benching them for a day only means
    asking again tomorrow. Measured: they took two of the four slots a quorum
    of two asks for, on every upload.
    """
    monkeypatch.setenv("WAYFARE_LLM_PROVIDERS", "")  # One endpoint, as this test means.
    config._config = None
    monkeypatch.setattr(
        llm.modelchain,
        "free_models",
        lambda url: [
            "thinkingmachines/inkling:free",
            "google/gemma-4-31b-it:free",
            "thinkingmachines/inkling-small:free",
            "minimax/minimax-m3:free",
        ],
    )
    assert llm.free_models() == ["google/gemma-4-31b-it:free", "minimax/minimax-m3:free"]


def test_the_quorum_pool_skips_them_too(monkeypatch):
    monkeypatch.setenv("WAYFARE_LLM_PROVIDERS", "")  # One endpoint, as this test means.
    config._config = None
    monkeypatch.setenv("WAYFARE_LLM_PROVIDERS", "")
    config._config = None
    monkeypatch.setattr(
        llm.modelchain,
        "free_models",
        lambda url: ["thinkingmachines/inkling:free", "google/gemma-4-31b-it:free"],
    )
    assert "thinkingmachines/inkling:free" not in llm.usable_models(4)


def test_a_benched_model_does_not_take_a_place_in_the_chain(monkeypatch, tmp_path):
    """Measured: one provider's whole free tier was capped for the day, its
    dead models took three of the chain's four slots, and the one working
    endpoint got the last. That endpoint returned a transient 500 and the
    document was reported unreadable with usable models never tried."""
    monkeypatch.setenv("WAYFARE_SECRETS_DIR", str(tmp_path))
    monkeypatch.setenv("WAYFARE_LLM_FALLBACKS", "3")
    config._config = None
    cfg = config.get_config()

    monkeypatch.setattr(
        llm,
        "free_models",
        lambda c=None: ["capped-a", "capped-b", "capped-c", "working-a", "working-b"],
    )
    bench = llm._bench(cfg)
    for model in ("capped-a", "capped-b", "capped-c"):
        bench.bench(model, "429 rate limited", 600)

    chain = llm._candidates(cfg)

    assert "working-a" in chain and "working-b" in chain
    assert not any(m.startswith("capped") for m in chain)


def test_everything_benched_is_tried_anyway(monkeypatch, tmp_path):
    """A bench duration is an estimate; one stale entry must not mean silence."""
    monkeypatch.setenv("WAYFARE_SECRETS_DIR", str(tmp_path))
    config._config = None
    cfg = config.get_config()

    monkeypatch.setattr(llm, "free_models", lambda c=None: ["a", "b"])
    bench = llm._bench(cfg)
    for model in ("a", "b", cfg.llm_model):
        bench.bench(model, "429", 600)

    assert llm._candidates(cfg)
