"""AI chat: what is sent to the model, and what is kept.

`insights.complete` is always mocked here — the suite must never reach
api.deepseek.com, the same reasoning conftest.py's `fake_secrets` applies to
Key Vault and blob storage.
"""

from __future__ import annotations

from datetime import date

import pytest

from gymlog import chat
from gymlog.model import Log

from .factories import condition, entry, session


@pytest.fixture
def captured(monkeypatch):
    sent: list[list[dict[str, str]]] = []

    def fake_complete(messages):
        sent.append(messages)
        return "Sounds like a good week."

    monkeypatch.setattr("gymlog.insights.complete", fake_complete)
    return sent


def test_send_appends_the_question_and_the_reply(captured):
    conversation = chat.send(Log(), (), "How am I doing?", today=date(2026, 9, 15))

    assert [(m.role, m.content) for m in conversation] == [
        ("user", "How am I doing?"),
        ("assistant", "Sounds like a good week."),
    ]


def test_every_turn_carries_the_log_as_context(captured):
    log = Log(
        sessions=(session("2026-09-14", "b", "A", entry("Cable Flyes", "chest", (10, 7.5))),),
        conditions=(condition(body_part="left knee"),),
    )

    chat.send(log, (), "Can I squat?", today=date(2026, 9, 15))

    system = [m["content"] for m in captured[0] if m["role"] == "system"]
    assert "not a doctor" in system[0]
    assert "Cable Flyes" in system[1]
    assert "left knee" in system[1]
    assert captured[0][-1] == {"role": "user", "content": "Can I squat?"}


def test_only_the_most_recent_turns_are_resent(captured):
    history = tuple(
        chat.Message(role="user" if i % 2 == 0 else "assistant", content=f"m{i}", at="")
        for i in range(chat.CONTEXT_MESSAGES + 10)
    )

    chat.send(Log(), history, "latest", today=date(2026, 9, 15))

    turns = [m for m in captured[0] if m["role"] != "system"]
    assert len(turns) == chat.CONTEXT_MESSAGES
    assert turns[-1]["content"] == "latest"


def test_a_failed_call_propagates(monkeypatch):
    def boom(_messages):
        raise OSError("deepseek down")

    monkeypatch.setattr("gymlog.insights.complete", boom)
    with pytest.raises(OSError):
        chat.send(Log(), (), "hello")


def test_no_conversation_yet_is_empty():
    assert chat.load() == ()


def test_a_saved_conversation_reads_back():
    messages = (
        chat.Message(role="user", content="hi", at="2026-09-15T08:00:00+00:00"),
        chat.Message(role="assistant", content="hello", at="2026-09-15T08:00:00+00:00"),
    )
    chat.save(messages)
    assert chat.load() == messages


def test_storage_keeps_only_the_most_recent_messages():
    messages = tuple(
        chat.Message(role="user", content=str(i), at="") for i in range(chat.STORED_MESSAGES + 5)
    )
    chat.save(messages)

    loaded = chat.load()
    assert len(loaded) == chat.STORED_MESSAGES
    assert loaded[-1].content == str(chat.STORED_MESSAGES + 4)


def test_clear_empties_the_conversation():
    chat.save((chat.Message(role="user", content="hi", at=""),))
    chat.clear()
    assert chat.load() == ()
