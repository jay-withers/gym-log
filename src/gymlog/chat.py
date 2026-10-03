"""AI chat: a running conversation about training and health, on `/chat`.

One conversation, not a list of them. It is stored as its own blob beside the
log (`store.CHAT_BLOB_NAME`) rather than inside it, because it is talk about
the record rather than part of it, and cleared by hand from the page.

Every turn re-sends the same context the weekly insight is built from —
recent sessions, active conditions and goals, Garmin recovery — as a second
system message, built fresh from the log as it stands at that moment. So a
session logged mid-conversation is visible on the very next reply, and nothing
derived from the log is ever stored twice.

Chat adds two things the weekly insight leaves out: the current block's full
prescription, and recent results per slot. The insight only lists either when
a block is about to end. A chat is where someone asks "what do you think of my
new block?" in week one, and without them the model cannot see a single
exercise.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from . import insights, store
from .model import Log

# Turns re-sent to the model with each new message. Enough to keep a thread
# coherent; bounded so a long-running conversation does not grow every
# request's token bill without limit.
CONTEXT_MESSAGES = 20

# Recent results per slot included in the context — enough to compare a new
# block's choices against what the last one was doing, without re-sending the
# whole history every turn.
SLOT_HISTORY = 3

# Messages kept on disk. Older ones fall off rather than growing the blob
# forever — this is a chat, not an archive.
STORED_MESSAGES = 200

SYSTEM_PROMPT = (
    "You are a knowledgeable, friendly training and health companion built into "
    "someone's personal training log. Talk with them about their training, "
    "recovery, sleep, nutrition, injuries and general wellbeing, using the "
    "training log context you are given wherever it is relevant, and refer to "
    "specifics from it rather than speaking in generalities. Keep replies "
    "conversational and reasonably short, in plain text without markdown "
    "headings. Never suggest anything that would aggravate a listed active "
    "injury or condition. You are not a doctor: for anything that sounds like "
    "a medical problem — chest pain, fainting, a sudden or worsening injury, "
    "symptoms that persist — say plainly that they should see a GP or "
    "physiotherapist, and do not diagnose."
)


@dataclass(frozen=True)
class Message:
    role: str  # "user" or "assistant", as the chat completions API names them
    content: str
    at: str

    def to_json(self) -> dict[str, Any]:
        return {"role": self.role, "content": self.content, "at": self.at}

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Message:
        return cls(
            role=str(payload.get("role", "")),
            content=str(payload.get("content", "")),
            at=str(payload.get("at", "")),
        )


def load() -> tuple[Message, ...]:
    raw = store.load_chat()
    if raw is None:
        return ()
    return tuple(Message.from_json(m) for m in json.loads(raw).get("messages", []))


def save(messages: tuple[Message, ...]) -> None:
    kept = messages[-STORED_MESSAGES:]
    store.save_chat(json.dumps({"messages": [m.to_json() for m in kept]}, indent=2))


def clear() -> None:
    save(())


def send(
    log: Log, history: tuple[Message, ...], text: str, today: date | None = None
) -> tuple[Message, ...]:
    """`history` plus `text` and the model's reply to it.

    Raises if the model call fails, leaving nothing saved — the caller decides
    what to show.
    """
    today = today or datetime.now(UTC).date()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    asked = Message(role="user", content=text, at=now)
    reply = insights.complete(_messages(log, (*history, asked), today))
    answered = Message(role="assistant", content=reply, at=now)
    return (*history, asked, answered)


def _messages(log: Log, conversation: tuple[Message, ...], today: date) -> list[dict[str, str]]:
    context = (
        f"Today is {today.isoformat()}. Their training log as it stands right now:\n\n"
        + insights._prompt(log, today)
        + _block_lines(log)
        + _history_lines(log)
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": context},
        *({"role": m.role, "content": m.content} for m in conversation[-CONTEXT_MESSAGES:]),
    ]


def _block_lines(log: Log) -> str:
    """The current block's prescription: every day's exercises, sets and reps."""
    block = log.current_block
    if block is None:
        return ""

    lines = [f"\n\nCurrent block's exercises ({block.name}, started {block.started}):"]
    for day in block.days.values():
        lines.append(f"{day.label}:")
        for e in day.exercises:
            if not e.tracked:
                lines.append(f"- {e.slot}: {e.name} (untracked finisher)")
                continue
            detail = f"{e.sets} sets of {e.rep_range_label}"
            if e.rest_seconds:
                detail += f", {e.rest_seconds}s rest"
            if e.seed_weight is not None:
                detail += f", starting at {e.seed_weight:g}kg"
            lines.append(f"- {e.slot}: {e.name} ({detail})")
    return "\n".join(lines)


def _history_lines(log: Log) -> str:
    """The last few heaviest sets per slot, across blocks — the before to compare with."""
    lines = []
    for slot in log.slots:
        recent = log.history(slot)[-SLOT_HISTORY:]
        if recent:
            progression = ", ".join(
                f"{when} {exercise} {set_.reps}x{set_.weight:g}kg"
                for when, exercise, set_ in recent
            )
            lines.append(f"- {slot}: {progression}")
    if not lines:
        return ""
    return "\n\nRecent results by slot (heaviest set per session):\n" + "\n".join(lines)
