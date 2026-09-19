"""Every page the application serves.

Server-rendered Jinja2. `POST` handlers redirect rather than render, so a reload
after logging a session does not offer to submit it twice — which on an
append-only log would mean a duplicate session rather than a harmless no-op.
"""

from __future__ import annotations

import logging
import pathlib
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

    cards = []
    for index, exercise in enumerate(block.days[day].exercises):
        previous = log.last_entry(exercise.name)
        last_entry, last_date = previous if previous else (None, "")
        cards.append(
            {
                "index": index,
                "exercise": exercise,
                "suggestion": suggest(exercise, last_entry, last_date),
                "last": last_entry,
                "last_date": last_date,
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
            "today": _today().isoformat(),
        },
    )


@router.post("/session/{day}", include_in_schema=False)
async def log_session(request: Request, day: str) -> Any:
    log, _etag = store.load()
    block = log.current_block
    if block is None or day not in block.days:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    form = await request.form()
    when = str(form.get("date") or _today().isoformat())
    entries = _entries(block.days[day], form)

    if not entries:
        # Nothing was filled in. Recording an empty session would put a
        # zero-rep entry into the history and drag every suggestion down.
        return RedirectResponse(f"/session/{day}?empty=1", status_code=status.HTTP_303_SEE_OTHER)

    session = Session(date=when, block=block.id, day=day, entries=entries)
    try:
        store.update(lambda current: current.with_session(session))
    except store.ConflictError as exc:
        raise ConflictResponse(
            "the log was changed elsewhere while this session was being saved; nothing was recorded"
        ) from exc

    logger.info("logged %s day %s — %d exercise(s)", when, day, len(entries))
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


def _entries(day: Day, form: Any) -> tuple[Entry, ...]:
    """Read the posted form into entries, dropping anything left blank.

    A set with no reps is a set that was not performed — the form always renders
    the full prescription, and stopping at two sets of three is ordinary. Only
    what was filled in is recorded.
    """
    out: list[Entry] = []
    for index, exercise in enumerate(day.exercises):
        if not exercise.tracked:
            if form.get(f"done_{index}"):
                out.append(Entry(slot=exercise.slot, exercise=exercise.name, note="done"))
            continue

        sets: list[SetLog] = []
        for position in range(exercise.sets):
            reps = _int(form.get(f"reps_{index}_{position}"))
            weight = _float(form.get(f"weight_{index}_{position}"))
            if reps is None or weight is None:
                continue
            sets.append(SetLog(reps=reps, weight=weight))

        if sets:
            out.append(Entry(slot=exercise.slot, exercise=exercise.name, sets=tuple(sets)))
    return tuple(out)


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

    The exercise *names* are whatever was typed into the form; everything else
    about each slot defaults to the outgoing block's prescription, because
    changing the movement rarely means changing the sets or the rep range.

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
            exercises.append(
                Exercise(
                    slot=exercise.slot,
                    name=chosen or exercise.name,
                    sets=exercise.sets,
                    rep_low=exercise.rep_low,
                    rep_high=exercise.rep_high,
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
