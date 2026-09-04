"""Asking the model which of two readings the document supports.

The risk here is obvious: a model asked to choose is a model given a fresh
chance to invent. Every constraint below exists to close that door.
"""

import json

import pytest

from wayfare import config
from wayfare.extractors import llm


SOURCE = "Depart John F. Kennedy International Airport at 12:58 for Lisbon"
CONVERSATION = {"prompt": SOURCE, "reply": {"records": [{"kind": "flight"}]}}
DISPUTE = [
    {
        "field": "destination.name",
        "values": ["John F Kennedy", "John F. Kennedy International Airport"],
    }
]


class Reply:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return {"choices": [{"message": {"content": json.dumps(self._payload)}}]}


def answer(monkeypatch, payload, status=200):
    seen = {}

    def fake_post(model, text, cfg, messages=None):
        seen["messages"] = messages
        return Reply(payload, status)

    monkeypatch.setattr(llm, "_post", fake_post)
    return seen


def test_a_quoted_choice_is_accepted(monkeypatch):
    answer(
        monkeypatch,
        {
            "choices": {"destination.name": "John F. Kennedy International Airport"},
            "evidence": {"destination.name": "John F. Kennedy International Airport"},
        },
    )
    picked = llm.adjudicate("a:free", CONVERSATION, DISPUTE, SOURCE)
    assert picked == {"destination.name": "John F. Kennedy International Airport"}


def test_the_question_continues_the_original_conversation(monkeypatch):
    """The model is asked with the document and its own answer still in view."""
    seen = answer(
        monkeypatch,
        {"choices": {}, "evidence": {}},
    )
    llm.adjudicate("a:free", CONVERSATION, DISPUTE, SOURCE)

    roles = [m["role"] for m in seen["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    assert SOURCE in seen["messages"][1]["content"]
    assert json.loads(seen["messages"][2]["content"]) == CONVERSATION["reply"]
    assert "John F Kennedy" in seen["messages"][3]["content"]


def test_a_third_value_is_refused(monkeypatch):
    """Adjudicating is choosing between two readings, not writing a new one."""
    answer(
        monkeypatch,
        {
            "choices": {"destination.name": "JFK Airport, New York"},
            "evidence": {"destination.name": "John F. Kennedy International Airport"},
        },
    )
    assert llm.adjudicate("a:free", CONVERSATION, DISPUTE, SOURCE) == {}


def test_a_ruling_with_no_quote_is_refused(monkeypatch):
    """The adjudicator is held to the evidence rule like any other reading."""
    answer(
        monkeypatch,
        {"choices": {"destination.name": "John F Kennedy"}, "evidence": {}},
    )
    assert llm.adjudicate("a:free", CONVERSATION, DISPUTE, SOURCE) == {}


def test_a_quote_that_is_not_on_the_page_is_refused(monkeypatch):
    answer(
        monkeypatch,
        {
            "choices": {"destination.name": "John F Kennedy"},
            "evidence": {"destination.name": "Terminal 4, Gate B22"},
        },
    )
    assert llm.adjudicate("a:free", CONVERSATION, DISPUTE, SOURCE) == {}


def test_a_field_nobody_disputed_is_ignored(monkeypatch):
    answer(
        monkeypatch,
        {
            "choices": {"confirmation": "GBUQV6"},
            "evidence": {"confirmation": "12:58"},
        },
    )
    assert llm.adjudicate("a:free", CONVERSATION, DISPUTE, SOURCE) == {}


def test_a_failing_model_settles_nothing(monkeypatch):
    answer(monkeypatch, {"choices": {}}, status=429)
    assert llm.adjudicate("a:free", CONVERSATION, DISPUTE, SOURCE) == {}


def test_nothing_is_asked_when_nothing_is_disputed(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("should not have called the provider")

    monkeypatch.setattr(llm, "_post", explode)
    assert llm.adjudicate("a:free", CONVERSATION, [], SOURCE) == {}
