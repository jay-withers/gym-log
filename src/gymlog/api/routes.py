"""Every page the application serves.

Server-rendered Jinja2. `POST` handlers redirect rather than render, so a reload
after logging a session does not offer to submit it twice — which on an
append-only log would mean a duplicate session rather than a harmless no-op.
"""

from __future__ import annotations

import logging
import pathlib
import re
from datetime import UTC, date, datetime
from typing import Any

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import store
from ..model import Block, Day, Entry, Exercise, Session, SetLog
from ..progression import suggest
from ..settings import settings
from . import deps

logger = logging.getLogger(__name__)

PACKAGE_DIR = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

public = APIRouter()
router = APIRouter()


class ConflictResponse(Exception):
    """Raised when a write lost its race twice. Handled in main.create_app."""


def _today() -> date:
    return datetime.now(UTC).date()


# --- login -------------------------------------------------------------------


@public.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_form(request: Request) -> Any:
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@public.post("/login", include_in_schema=False)
def login(request: Request, passcode: str = Form(default="")) -> Any:
    if not deps.check_passcode(passcode):
        # No detail about *why*. "Wrong passcode" and "no passcode configured"
        # are the same message to whoever is typing.
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "That is not it."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        deps.COOKIE_NAME,
        deps.issue(),
        max_age=settings().cookie_max_age_seconds,
        httponly=True,
        samesite="lax",
        # The platform terminates TLS and there is no plain-HTTP route in, so
        # this costs nothing and stops the cookie leaking if that ever changes.
        secure=True,
    )
    return response


@public.get("/manifest.json", include_in_schema=False)
def manifest() -> JSONResponse:
    """What lets the phone install this to the home screen.

    `display: standalone` is the point of it — opened from the home screen there
    is no browser chrome, which on a phone is the difference between seeing three
    exercises and seeing five.
    """
    return JSONResponse(
        {
            "name": "gym-log",
            "short_name": "gym",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#101014",
            "theme_color": "#101014",
            "icons": [],
        }
    )


# --- the log -----------------------------------------------------------------


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request) -> Any:
    log, _etag = store.load()
    block = log.current_block
    today = _today()

    if block is None:
        return templates.TemplateResponse(request, "empty.html", {})

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "block": block,
            "week": block.week_of(today),
            "due": block.due(today),
            "next_day": _next_day(log, block),
            "recent": sorted(log.sessions, key=lambda s: s.date, reverse=True)[:8],
            "slots": log.slots,
        },
    )


@router.get("/session/{day}", response_class=HTMLResponse, include_in_schema=False)
def session_form(request: Request, day: str) -> Any:
    log, _etag = store.load()
    block = log.current_block
    if block is None or day not in block.days:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    today = _today().isoformat()
    # Suggestions come from everything *except* today's own session. With it
    # included, saving an exercise would immediately re-suggest against the set
    # just typed in, and the card would chase itself up the rep range.
    history = log.excluding(today, day)
    started = log.session_on(today, day)

    cards = []
    for index, exercise in enumerate(block.days[day].exercises):
        previous = history.last_entry(exercise.name)
        last_entry, last_date = previous if previous else (None, "")
        cards.append(
            {
                "index": index,
                "exercise": exercise,
                "suggestion": suggest(exercise, last_entry, last_date),
                "last": last_entry,
                "last_date": last_date,
                # What is already in the log for this exercise today, which the
                # card shows back as values rather than placeholders so that
                # re-saving it is an edit rather than a fresh guess.
                "recorded": _recorded(started, exercise.name),
            }
        )

    return templates.TemplateResponse(
        request,
        "session.html",
        {
            "block": block,
            "day": day,
            "label": block.days[day].label,
            "week": block.week_of(_today()),
            "cards": cards,
            "today": today,
            "done": sum(1 for c in cards if c["recorded"]),
        },
    )


@router.post("/session/{day}/{index}", include_in_schema=False)
async def log_exercise(request: Request, day: str, index: int) -> Any:
    """Record one exercise into today's session, starting the session if needed.

    **One submit per exercise, not one per session.** A session is an hour of a
    phone in a pocket, and a single Save at the end means an hour of typing
    riding on the tab surviving that long. Each exercise is banked as it is
    finished instead, so the worst case is the one in progress.

    They still make one session in the log, because one training session is what
    happened — see `Log.with_entry`.
    """
    log, _etag = store.load()
    block = log.current_block
    if block is None or day not in block.days:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    exercises = block.days[day].exercises
    if not 0 <= index < len(exercises):
        return RedirectResponse(f"/session/{day}", status_code=status.HTTP_303_SEE_OTHER)

    exercise = exercises[index]
    form = await request.form()
    when = str(form.get("date") or _today().isoformat())
    entry = _entry(exercise, index, form)

    if entry is None:
        # Nothing filled in. Recording it would put a zero-rep entry into the
        # history and drag every later suggestion down.
        return _back(day, index, "empty")

    try:
        store.update(lambda current: current.with_entry(when, block.id, day, entry))
    except store.ConflictError as exc:
        raise ConflictResponse(
            f"the log was changed elsewhere while {exercise.name} was being saved; "
            "it was not recorded"
        ) from exc

    logger.info("recorded %s on %s day %s", exercise.name, when, day)
    return _back(day, index, "saved")


def _back(day: str, index: int, outcome: str) -> RedirectResponse:
    """Back to the session, at the card just submitted.

    The fragment matters on a phone: without it every save scrolls back to the
    top and the next exercise has to be found again, six times a session.
    """
    return RedirectResponse(
        f"/session/{day}?{outcome}={index}#e{index}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _recorded(session: Session | None, name: str) -> Entry | None:
    """What is already in today's session for `name`, if anything."""
    if session is None:
        return None
    return next((e for e in session.entries if e.exercise == name), None)


def _entry(exercise: Exercise, index: int, form: Any) -> Entry | None:
    """One exercise's posted fields, or None when nothing was filled in.

    A set with no reps is a set that was not performed — the card always renders
    the full prescription, and stopping at two sets of three is ordinary. Only
    what was filled in is recorded.

    The finisher is the exception to the shape rather than to the rule: it is
    timed rather than loaded, so a time on its own counts as having done it and
    the tick box is not also required.
    """
    if not exercise.tracked:
        seconds = _seconds(form.get(f"seconds_{index}"))
        if form.get(f"done_{index}") or seconds:
            return Entry(slot=exercise.slot, exercise=exercise.name, note="done", seconds=seconds)
        return None

    sets: list[SetLog] = []
    for position in range(exercise.sets):
        reps = _int(form.get(f"reps_{index}_{position}"))
        weight = _float(form.get(f"weight_{index}_{position}"))
        if reps is None or weight is None:
            continue
        sets.append(SetLog(reps=reps, weight=weight))

    if not sets:
        return None
    return Entry(slot=exercise.slot, exercise=exercise.name, sets=tuple(sets))


@router.get("/history/{slot}", response_class=HTMLResponse, include_in_schema=False)
def history(request: Request, slot: str) -> Any:
    log, _etag = store.load()
    rows = log.history(slot)
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "slot": slot,
            "slots": log.slots,
            # Newest first on screen; `Log.history` returns oldest first because
            # that is the order a chart wants.
            "rows": tuple(reversed(rows)),
            "peak": max((r[2].weight for r in rows), default=0.0),
        },
    )


# --- blocks ------------------------------------------------------------------


@router.get("/block", response_class=HTMLResponse, include_in_schema=False)
def block_form(request: Request) -> Any:
    log, _etag = store.load()
    block = log.current_block
    if block is None:
        return templates.TemplateResponse(request, "empty.html", {})
    return templates.TemplateResponse(
        request,
        "block.html",
        {
            "block": block,
            "week": block.week_of(_today()),
            "due": block.due(_today()),
            "today": _today().isoformat(),
        },
    )


@router.post("/block", include_in_schema=False)
async def rotate(request: Request) -> Any:
    """Start a new block, carrying the slot structure and prescriptions forward.

    The exercise *names* and *rep ranges* are whatever was typed into the form;
    everything else about each slot defaults to the outgoing block's
    prescription, because changing the movement rarely means changing the sets
    or the rest.

    History is untouched. Sessions reference the block they were performed in and
    carry their own slot, so a rotation adds to the record rather than resetting
    it — which is the single thing the spreadsheet could not do.
    """
    log, _etag = store.load()
    previous = log.current_block
    if previous is None:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    form = await request.form()
    started = str(form.get("started") or _today().isoformat())
    name = str(form.get("name") or f"Block from {started}")

    days: dict[str, Day] = {}
    for key, day in previous.days.items():
        exercises: list[Exercise] = []
        for index, exercise in enumerate(day.exercises):
            chosen = str(form.get(f"name_{key}_{index}") or exercise.name).strip()
            rep_low, rep_high = _chosen_range(form, key, index, exercise)
            exercises.append(
                Exercise(
                    slot=exercise.slot,
                    name=chosen or exercise.name,
                    sets=exercise.sets,
                    rep_low=rep_low,
                    rep_high=rep_high,
                    # Per-set targets describe the range they were written for —
                    # `10/12/12` inside 10-12. Carrying them into a range that
                    # has moved would put the old numbers in the session
                    # placeholders and contradict the target printed above them.
                    rep_targets=(
                        exercise.rep_targets
                        if (rep_low, rep_high) == (exercise.rep_low, exercise.rep_high)
                        else ()
                    ),
                    rest_seconds=exercise.rest_seconds,
                    increment=exercise.increment,
                    # No seed weight: a changed movement starts from what gets
                    # logged, and an unchanged one already has real history to
                    # progress from. Carrying the old sheet's number forward
                    # would override both.
                    seed_weight=None,
                )
            )
        days[key] = Day(label=day.label, exercises=tuple(exercises))

    block = Block(id=started, name=name, started=started, weeks=previous.weeks, days=days)
    try:
        store.update(lambda current: current.with_block(block))
    except store.ConflictError as exc:
        raise ConflictResponse(
            "the log was changed elsewhere; the new block was not saved"
        ) from exc

    logger.info("rotated to block %s (%s)", block.id, block.name)
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


# --- helpers -----------------------------------------------------------------


def _next_day(log: Any, block: Block) -> str:
    """Whichever day was not done last. Alternates, which is what twice a week means."""
    keys = sorted(block.days)
    if not keys:
        return ""
    for session in sorted(log.sessions, key=lambda s: s.date, reverse=True):
        if session.day in keys:
            return keys[(keys.index(session.day) + 1) % len(keys)]
    return keys[0]


def _seconds(value: Any) -> int:
    """A finisher's time, as seconds. 0 means nothing was entered.

    Accepts what a phone keyboard makes easy mid-session: `90`, `1:30`, `2m`,
    `1m30`. A bare number is seconds, because the sheet's Rest column already
    trained the habit of writing seconds. Anything unparseable is treated as
    untimed rather than rejected — the finisher is optional, and losing the
    whole session to a typo in a field nobody had to fill in would be absurd.
    """
    raw = str(value or "").strip().lower()
    if not raw:
        return 0
    if ":" in raw:
        minutes, _, rest = raw.partition(":")
        parts = [minutes, rest]
    elif "m" in raw:
        minutes, _, rest = raw.partition("m")
        parts = [minutes, rest.rstrip("s")]
    else:
        return max(0, int(m.group()) if (m := re.search(r"\d+", raw)) else 0)
    try:
        return max(0, int(parts[0] or 0) * 60 + int(parts[1] or 0))
    except ValueError:
        return 0


def _chosen_range(form: Any, key: str, index: int, exercise: Exercise) -> tuple[int, int]:
    """The rep range typed into the block form, or the outgoing one.

    A blank, a zero or a word falls back per field rather than for the pair, so
    raising only the top of 8-10 to 8-12 does not need both boxes retyped. A
    range entered backwards is read as a transposition rather than rejected:
    there is no error path on this form, and refusing the whole rotation over
    `12-10` would be worse than reading it the only way it can be meant.
    """

    def typed(field: str, fallback: int) -> int:
        value = _int(form.get(f"{field}_{key}_{index}"))
        # `min` on the input is advice, not a guarantee: anything can be posted.
        return value if value is not None and value > 0 else fallback

    low = typed("rep_low", exercise.rep_low)
    high = typed("rep_high", exercise.rep_high)
    if high < low:
        low, high = high, low
    return low, high


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None
