"""AI training insight.

Generated on a weekly schedule by a Container App Job (see
terraform/main.container-apps-job.tf), and also on request — from the CLI by
hand, or from a button on the web app's `/insights` page. There is no
cooldown between runs; whatever calls this gets a fresh insight.

DeepSeek's API is OpenAI-compatible, so a plain `urllib.request` call is
enough — the same reasoning that keeps the Dockerfile's own healthcheck off a
dependency (Dockerfile's HEALTHCHECK uses urllib too), rather than adding an
HTTP client just for one call a week.
"""

from __future__ import annotations

import json
import logging
import urllib.request
import uuid
from datetime import UTC, date, datetime, timedelta

from .model import Insight, Log
from .settings import secret

logger = logging.getLogger(__name__)

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"

# How many weeks of headroom before a block is due count as "ending soon".
# 1 gives exactly one weekly insight with next-block suggestions before the
# block page's own "due" banner appears.
BLOCK_ENDING_SOON_WEEKS = 1

SYSTEM_PROMPT = (
    "You write a short, encouraging weekly summary of someone's strength "
    "training for them to read in their training log. Two or three short "
    "paragraphs, plain text, no headings or bullet points. Note real "
    "progress, flag anything that looks stalled, and never suggest anything "
    "that would aggravate a listed active injury or condition. When the "
    "prompt says the current block is ending soon and gives recent "
    "progression by slot, add a short closing paragraph suggesting specific "
    "exercises or progressions for one or two slots that look ready for a "
    "change in the next block, weighing active goals and avoiding anything "
    "that would aggravate a listed condition."
)


def generate_insight(log: Log, today: date | None = None) -> Insight:
    """A new training insight, built from the log as it stands right now."""
    today = today or datetime.now(UTC).date()
    summary = _complete(_prompt(log, today))
    return Insight(
        id=uuid.uuid4().hex[:12],
        week_of=today.isoformat(),
        summary=summary,
        generated_at=today.isoformat(),
    )


def _prompt(log: Log, today: date) -> str:
    cutoff = (today - timedelta(days=7)).isoformat()
    recent = [s for s in log.sessions if s.date >= cutoff]
    achieved = [a for a in log.achievements if a.date >= cutoff]
    active_conditions = [c for c in log.conditions if c.status == "active"]
    active_goals = [g for g in log.goals if g.status == "active"]

    lines = [f"Training sessions in the last 7 days (since {cutoff}):"]
    if recent:
        for session in recent:
            performed = ", ".join(
                f"{entry.exercise} ({len(entry.sets)} sets)" if entry.sets else entry.exercise
                for entry in session.entries
            )
            lines.append(f"- {session.date} ({session.day}): {performed or 'nothing logged'}")
    else:
        lines.append("- none")

    lines.append("\nAchievements logged this week:")
    if achieved:
        lines.extend(f"- {a.title}" for a in achieved)
    else:
        lines.append("- none")

    lines.append("\nActive injuries/conditions to avoid aggravating:")
    if active_conditions:
        lines.extend(
            f"- {c.body_part}: {c.note}" if c.note else f"- {c.body_part}"
            for c in active_conditions
        )
    else:
        lines.append("- none")

    lines.append("\nActive goals:")
    if active_goals:
        lines.extend(f"- {g.title}" for g in active_goals)
    else:
        lines.append("- none")

    block = log.current_block
    lines.append("\nCurrent training block:")
    if block is None:
        lines.append("- none")
    else:
        week = block.week_of(today)
        remaining = block.weeks - week
        status = " (due for rotation)" if block.due(today) else ""
        lines.append(f"- {block.name}, week {week} of {block.weeks}{status}")

        if remaining <= BLOCK_ENDING_SOON_WEEKS:
            lines.append(
                "\nThis block is ending soon. Recent progression by slot, for "
                "suggesting exercises for the next block:"
            )
            for slot in log.slots:
                history = log.history(slot)
                if not history:
                    continue
                progression = ", ".join(
                    f"{when} {exercise} ({set_.reps}x{set_.weight}kg)"
                    for when, exercise, set_ in history[-3:]
                )
                lines.append(f"- {slot}: {progression}")

    return "\n".join(lines)


def _complete(prompt: str) -> str:
    body = json.dumps(
        {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        DEEPSEEK_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {secret('DEEPSEEK-API-KEY')}",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return str(payload["choices"][0]["message"]["content"]).strip()
