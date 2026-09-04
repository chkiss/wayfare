"""Which models are worth asking at all."""

import pytest

from wayfare.extractors import llm

def test_models_that_refuse_this_kind_of_client_are_not_offered(monkeypatch):
    """Both inkling models answer every request with
    "403 ... is only available on agentic harnesses".

    The bench is for a model that is temporarily unavailable; these are
    permanently unavailable to us, so benching them for a day only means
    asking again tomorrow. Measured: they took two of the four slots a quorum
    of two asks for, on every upload.
    """
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
    monkeypatch.setattr(
        llm.modelchain,
        "free_models",
        lambda url: ["thinkingmachines/inkling:free", "google/gemma-4-31b-it:free"],
    )
    assert "thinkingmachines/inkling:free" not in llm.usable_models(4)
