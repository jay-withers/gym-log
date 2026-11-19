"""Every page the application serves.

Server-rendered Jinja2. `POST` handlers redirect rather than render, so a reload
after logging a session does not offer to submit it twice — which on an
append-only log would mean a duplicate session rather than a harmless no-op.
"""

from __future__ import annotations

import logging
import math
import pathlib
import re
import statistics
import uuid
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Form, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import store
from ..model import (
    GARMIN_ZONE_COUNT,
    Achievement,
    Block,
    Condition,
    Day,
    Entry,
    Exercise,
    Goal,
    Log,
    Session,
    SetLog,
)
from ..progression import suggest
from ..settings import settings
from . import deps

logger = logging.getLogger(__name__)


def _loggable(value: object) -> str:
    """A request-supplied value with line breaks removed, safe to log.

    Form fields and path segments are whatever the client sent: a line break in
    an exercise name would otherwise start a new, forged-looking log line.
    """
    return str(value).replace("\r", "").replace("\n", "")


PACKAGE_DIR = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

public = APIRouter()
router = APIRouter()

# Days that carry no block prescription at all — logged purely by hand, via
# `log_extra_exercise`. A dict rather than a set so each has a label the same
# way a block's own days do, and so a second one is as cheap to add as this
# first one was. "manual" is not tied to a day of the week: it exists so
# anything can be logged any time, not just Tuesday or Thursday.
LOOSE_DAYS: dict[str, str] = {"manual": "Manual"}


class ConflictResponse(Exception):
    """Raised when a write lost its race twice. Handled in main.create_app."""


class GarminSyncFailed(Exception):
    """Raised when a manual Garmin sync errors out. Handled in main.create_app.

    `garmin.sync_garmin` deliberately lets auth/API failures propagate
    uncaught, so the scheduled CLI job exits non-zero and pages whoever
    watches it. A person tapping "Sync Past 7 Days" in the browser needs the other
    behaviour — a readable message instead of a stack trace — so this route
    is the one place that catches it.
    """


class ChatFailed(Exception):
    """Raised when the model call behind `/chat` fails. Handled in main.create_app.

    A 502 with a readable message rather than a 500 with a stack trace, for
    the same reason as `GarminSyncFailed`: the failure is upstream, and the
    person on the other end needs to know to try again, not to file a bug.
    """


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


# The Android home-screen icons, rendered by scripts/render-icons.py and
# committed. PNG because Android Chrome builds the installed app from a raster
# icon, and with none at all it would not offer to install. The maskable one is
# full-bleed with the glyph inside the middle 80%, because the launcher crops it
# to its own shape. Read once at import: three small files.
ICON_DIR = PACKAGE_DIR / "icons"
# (filename, sizes, purpose) for each icon, which the manifest and route share.
PNG_ICONS = (
    ("icon-192.png", "192x192", "any"),
    ("icon-512.png", "512x512", "any"),
    ("icon-maskable-512.png", "512x512", "maskable"),
)
PNG_BYTES = {name: (ICON_DIR / name).read_bytes() for name, _, _ in PNG_ICONS}
# A week: the icon changes about never.
ICON_HEADERS = {"Cache-Control": "public, max-age=604800"}


@public.get("/icons/{name}", include_in_schema=False)
def png_icon(name: str) -> Response:
    """A home-screen icon. Public like the manifest: the install starts on the
    login page, before any cookie exists."""
    content = PNG_BYTES.get(name)
    if content is None:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    return Response(content=content, media_type="image/png", headers=ICON_HEADERS)


@public.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    """Browsers ask for this by name; the small icon beats a 404 in the logs."""
    return png_icon("icon-192.png")


@public.get("/manifest.json", include_in_schema=False)
def manifest() -> JSONResponse:
    """What lets the phone install this to the home screen.

    `display: standalone` is the point of it — opened from the home screen there
    is no browser chrome, which on a phone is the difference between seeing three
    exercises and seeing five.

    `start_url` points straight at `/strength` rather than the new home page:
    the phone icon exists for the twice-a-week fast path of logging a set, and
    a menu screen in front of it would be a tap this manifest exists to save.
    """
    return JSONResponse(
        {
            # `id` pins the installed app's identity apart from `start_url`, so
            # moving the start page later updates the app rather than forking it.
            "id": "/",
            "name": "Health",
            "short_name": "Health",
            "start_url": "/strength",
            "scope": "/",
            "display": "standalone",
            "background_color": "#101014",
            "theme_color": "#101014",
            "icons": [
                {"src": f"/icons/{name}", "type": "image/png", "sizes": sizes, "purpose": purpose}
                for name, sizes, purpose in PNG_ICONS
            ],
        }
    )


# --- home ----------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def home(request: Request) -> Any:
    """A light landing page. Everything below it is one section among several."""
    return templates.TemplateResponse(request, "home.html", {})


# --- strength training -----------------------------------------------------


@router.get("/strength", response_class=HTMLResponse, include_in_schema=False)
def strength_index(request: Request) -> Any:
    log, _etag = store.load()
    block = log.current_block
    today = _today()

    if block is None:
        return templates.TemplateResponse(request, "empty.html", {})

    return templates.TemplateResponse(
        request,
        "strength.html",
        {
            "block": block,
            "week": block.week_of(today),
            "due": block.due(today),
            "next_day": _next_day(log, block),
            "recent": sorted(log.sessions, key=lambda s: s.date, reverse=True)[:8],
            "slots": log.slots,
            "loose_days": LOOSE_DAYS,
            "recovery": _recovery(log, today),
        },
    )


@router.get("/session/{day}", response_class=HTMLResponse, include_in_schema=False)
def session_form(request: Request, day: str) -> Any:
    log, _etag = store.load()
    block = log.current_block
    if block is None or (day not in block.days and day not in LOOSE_DAYS):
        return RedirectResponse("/strength", status_code=status.HTTP_303_SEE_OTHER)

    today = _today().isoformat()
    # Suggestions come from everything *except* today's own session. With it
    # included, saving an exercise would immediately re-suggest against the set
    # just typed in, and the card would chase itself up the rep range.
    # And only from this block: its first week starts with empty boxes rather
    # than numbers carried over from a different programme.
    history = log.excluding(today, day).within_block(block.id)
    started = log.session_on(today, day)

    prescribed = block.days[day].exercises if day in block.days else ()
    cards = []
    for index, exercise in enumerate(prescribed):
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
                "note": log.exercise_notes.get(exercise.name, ""),
            }
        )

    # Anything logged today that is not one of the prescribed cards above —
    # every entry, for a loose day; an improvised extra, for a block day.
    prescribed_names = {exercise.name for exercise in prescribed}
    extras = [e for e in (started.entries if started else ()) if e.exercise not in prescribed_names]

    return templates.TemplateResponse(
        request,
        "session.html",
        {
            "block": block,
            "day": day,
            "label": block.days[day].label if day in block.days else LOOSE_DAYS[day],
            "week": block.week_of(_today()),
            "cards": cards,
            "extras": extras,
            "today": today,
            "done": sum(1 for c in cards if c["recorded"]),
            "slots": log.slots,
            # Offered as suggestions on the "add an exercise" field, so typing
            # "Bicep" surfaces the "Bicep Curls" already on record instead of
            # inviting a slightly different name for the same movement.
            "known_exercises": _known_exercise_names(log),
        },
    )


@router.post("/session/{day}/extra", include_in_schema=False)
async def log_extra_exercise(request: Request, day: str) -> Any:
    """Log an exercise the block does not prescribe for `day`.

    Registered before `/session/{day}/{index}` — that route's `index` is an
    int, but a mismatched type only fails *validation*, not routing, so with
    the order reversed this "extra" would 422 rather than ever being reached.

    Uses `Log.with_entry`, the same mechanism a prescribed card saves through,
    so a loose day and an improvised extra on a block day both end up as one
    more entry in today's session rather than a different kind of record.
    """
    log, _etag = store.load()
    block = log.current_block
    if block is None or (day not in block.days and day not in LOOSE_DAYS):
        return RedirectResponse("/strength", status_code=status.HTTP_303_SEE_OTHER)

    form = await request.form()
    name = str(form.get("name") or "").strip()
    slot = str(form.get("slot") or "").strip()
    if not name or not slot:
        # Nothing worth recording without knowing what it was or where it
        # counts toward — same "silently do nothing" choice as an empty
        # prescribed card.
        return RedirectResponse(f"/session/{day}", status_code=status.HTTP_303_SEE_OTHER)

    when = str(form.get("date") or _today().isoformat())
    sets = []
    for position in range(3):
        reps = _int(form.get(f"reps_{position}"))
        if reps is None:
            continue
        # Unlike a prescribed exercise, weight is optional here: a manually
        # added set is as likely to be pull-ups or a plank as a loaded lift,
        # and a blank weight is bodyweight, not a set that was not performed.
        sets.append(SetLog(reps=reps, weight=_float(form.get(f"weight_{position}")) or 0.0))

    if not sets:
        return RedirectResponse(f"/session/{day}", status_code=status.HTTP_303_SEE_OTHER)

    entry = Entry(
        slot=slot, exercise=name, sets=tuple(sets), note=str(form.get("note") or "").strip()
    )
    try:
        store.update(
            lambda current: current.with_entry(when, block.id, day, entry).ensuring_slot(slot)
        )
    except store.ConflictError as exc:
        raise ConflictResponse(
            f"the log was changed elsewhere while {name} was being saved; it was not recorded"
        ) from exc

    logger.info(
        "recorded extra exercise %s on %s day %s", _loggable(name), _loggable(when), _loggable(day)
    )
    return RedirectResponse(f"/session/{day}", status_code=status.HTTP_303_SEE_OTHER)


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
        return RedirectResponse("/strength", status_code=status.HTTP_303_SEE_OTHER)

    exercises = block.days[day].exercises
    if not 0 <= index < len(exercises):
        return RedirectResponse(f"/session/{day}", status_code=status.HTTP_303_SEE_OTHER)

    exercise = exercises[index]
    form = await request.form()
    when = str(form.get("date") or _today().isoformat())
    entry = _entry(exercise, index, form)
    # The card always posts its note box, prefilled with the current note, so
    # whatever arrives is the new note — including blank, which clears it.
    # Absent only from an older page still open on the phone; leave it alone.
    note = form.get(f"note_{index}")
    note_changed = note is not None and str(note).strip() != log.exercise_notes.get(
        exercise.name, ""
    )

    if entry is None and not note_changed:
        # Nothing filled in. Recording it would put a zero-rep entry into the
        # history and drag every later suggestion down.
        return _back(day, index, "empty")

    def apply(current: Log) -> Log:
        if entry is not None:
            current = current.with_entry(when, block.id, day, entry)
        if note is not None:
            current = current.with_exercise_note(exercise.name, str(note))
        return current

    try:
        store.update(apply)
    except store.ConflictError as exc:
        raise ConflictResponse(
            f"the log was changed elsewhere while {exercise.name} was being saved; "
            "it was not recorded"
        ) from exc

    if entry is None:
        # Only the note changed: a reminder written before the sets, say.
        return _back(day, index, "noted")
    logger.info("recorded %s on %s day %s", exercise.name, _loggable(when), _loggable(day))
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
            "untracked": tuple(reversed(log.untracked_history(slot))),
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
        return RedirectResponse("/strength", status_code=status.HTTP_303_SEE_OTHER)

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
                    # has moved would leave numbers the new range contradicts.
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
    return RedirectResponse("/strength", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/block/edit", response_class=HTMLResponse, include_in_schema=False)
def edit_block_form(request: Request) -> Any:
    log, _etag = store.load()
    block = log.current_block
    if block is None:
        return templates.TemplateResponse(request, "empty.html", {})
    return templates.TemplateResponse(request, "block_edit.html", {"block": block})


@router.post("/block/edit", include_in_schema=False)
async def edit_block(request: Request) -> Any:
    """Change the current block in place: same id, same days, same exercises in order.

    The id is the original start date and stays put even when `started` moves,
    because every session logged against this block references it by that id.

    Like the rotation form, nothing here has an error path: a blank or
    nonsensical field keeps the block's current value, per field. A start date
    on or before another block's would make this block stop being the current
    one, so that is kept as it was too.

    History is untouched. Sessions carry their own exercise names, so renaming
    a movement here relabels the prescription, not what was lifted — though its
    weight suggestions restart, since those follow the name.
    """
    form = await request.form()

    def apply_edit(current: Log) -> Log:
        block = current.current_block
        if block is None:
            return current

        started = block.started
        typed_start = str(form.get("started") or "").strip()
        try:
            date.fromisoformat(typed_start)
        except ValueError:
            typed_start = ""
        if typed_start and all(
            typed_start > other.started for other in current.blocks if other.id != block.id
        ):
            started = typed_start

        weeks = _int(form.get("weeks"))
        days = {
            key: Day(
                label=day.label,
                exercises=tuple(
                    _edited_exercise(form, key, index, exercise)
                    for index, exercise in enumerate(day.exercises)
                ),
            )
            for key, day in block.days.items()
        }
        return current.with_block(
            replace(
                block,
                name=str(form.get("name") or "").strip() or block.name,
                started=started,
                weeks=weeks if weeks is not None and 0 < weeks <= 52 else block.weeks,
                days=days,
            )
        )

    try:
        store.update(apply_edit)
    except store.ConflictError as exc:
        raise ConflictResponse("the log was changed elsewhere; the block was not saved") from exc
    return RedirectResponse("/strength", status_code=status.HTTP_303_SEE_OTHER)


def _edited_exercise(form: Any, key: str, index: int, exercise: Exercise) -> Exercise:
    """One exercise as typed into the edit form, falling back per field."""
    name = str(form.get(f"name_{key}_{index}") or "").strip() or exercise.name
    if not exercise.tracked:
        # The finisher is offered nothing but its name: it takes no load.
        return replace(exercise, name=name)

    rep_low, rep_high = _chosen_range(form, key, index, exercise)
    sets = _int(form.get(f"sets_{key}_{index}"))
    rest = _int(form.get(f"rest_{key}_{index}"))
    increment = _float(form.get(f"increment_{key}_{index}"))
    # Blank clears the starting weight rather than keeping it: the box is
    # rendered holding the current value, so emptying it is deliberate.
    typed_seed = str(form.get(f"seed_{key}_{index}") or "").strip()
    seed = _float(typed_seed) if typed_seed else None
    return replace(
        exercise,
        name=name,
        sets=sets if sets is not None and 0 < sets <= 20 else exercise.sets,
        rep_low=rep_low,
        rep_high=rep_high,
        # Same rule as rotating: per-set targets were written for one range.
        rep_targets=(
            exercise.rep_targets
            if (rep_low, rep_high) == (exercise.rep_low, exercise.rep_high)
            else ()
        ),
        rest_seconds=rest if rest is not None and rest >= 0 else exercise.rest_seconds,
        increment=increment if increment is not None and increment > 0 else exercise.increment,
        seed_weight=seed if seed is None or seed >= 0 else exercise.seed_weight,
    )


# --- achievements --------------------------------------------------------------


@router.get("/achievements", response_class=HTMLResponse, include_in_schema=False)
def achievements_list(request: Request) -> Any:
    """Read-only, newest first. Adding or editing happens on its own page."""
    log, _etag = store.load()
    return templates.TemplateResponse(
        request,
        "achievements.html",
        {"achievements": sorted(log.achievements, key=lambda a: a.date, reverse=True)},
    )


@router.get("/achievements/new", response_class=HTMLResponse, include_in_schema=False)
def new_achievement_form(request: Request, title: str = "", when: str = "") -> Any:
    """`title` and `when` prefill the form, for the strength page's "log as an
    achievement" link beside a PR. Nothing is saved until the form is sent."""
    return templates.TemplateResponse(
        request,
        "achievement_form.html",
        {"achievement": None, "today": when or _today().isoformat(), "title": title},
    )


@router.get(
    "/achievements/{achievement_id}/edit", response_class=HTMLResponse, include_in_schema=False
)
def edit_achievement_form(request: Request, achievement_id: str) -> Any:
    log, _etag = store.load()
    achievement = next((a for a in log.achievements if a.id == achievement_id), None)
    if achievement is None:
        return RedirectResponse("/achievements", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request, "achievement_form.html", {"achievement": achievement, "today": achievement.date}
    )


@router.post("/achievements", include_in_schema=False)
async def add_achievement(request: Request) -> Any:
    form = await request.form()
    title = str(form.get("title") or "").strip()
    if not title:
        # Nothing worth recording. Same "silently do nothing" choice as an
        # empty exercise card in log_exercise.
        return RedirectResponse("/achievements", status_code=status.HTTP_303_SEE_OTHER)

    achievement = Achievement(
        id=uuid.uuid4().hex[:12],
        date=str(form.get("date") or _today().isoformat()),
        title=title,
        note=str(form.get("note") or "").strip(),
    )
    try:
        store.update(lambda current: current.with_achievement(achievement))
    except store.ConflictError as exc:
        raise ConflictResponse(
            "the log was changed elsewhere; the achievement was not saved"
        ) from exc
    return RedirectResponse("/achievements", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/achievements/{achievement_id}", include_in_schema=False)
async def edit_achievement(request: Request, achievement_id: str) -> Any:
    form = await request.form()
    title = str(form.get("title") or "").strip()
    if not title:
        return RedirectResponse("/achievements", status_code=status.HTTP_303_SEE_OTHER)

    date = str(form.get("date") or "").strip()
    note = str(form.get("note") or "").strip()

    def apply_edit(current: Log) -> Log:
        existing = next((a for a in current.achievements if a.id == achievement_id), None)
        if existing is None:
            return current
        edited = replace(existing, date=date or existing.date, title=title, note=note)
        return current.with_achievement(edited)

    try:
        store.update(apply_edit)
    except store.ConflictError as exc:
        raise ConflictResponse(
            "the log was changed elsewhere; the achievement was not saved"
        ) from exc
    return RedirectResponse("/achievements", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/achievements/{achievement_id}/delete", include_in_schema=False)
def delete_achievement(achievement_id: str) -> Any:
    try:
        store.update(lambda current: current.without_achievement(achievement_id))
    except store.ConflictError as exc:
        raise ConflictResponse(
            "the log was changed elsewhere; the achievement was not deleted"
        ) from exc
    return RedirectResponse("/achievements", status_code=status.HTTP_303_SEE_OTHER)


# --- goals -----------------------------------------------------------------


@router.get("/goals", response_class=HTMLResponse, include_in_schema=False)
def goals_list(request: Request) -> Any:
    """Read-only, soonest target date first. Adding or editing has its own page."""
    log, _etag = store.load()
    return templates.TemplateResponse(
        request,
        "goals.html",
        {
            "active": sorted((g for g in log.goals if g.status == "active"), key=_goal_sort_key),
            "achieved": sorted((g for g in log.goals if g.status != "active"), key=_goal_sort_key),
        },
    )


def _goal_sort_key(goal: Goal) -> str:
    """A goal with no target date sorts after every one that has it, rather
    than first — an undated goal is not more urgent than a dated one."""
    return goal.target_date or "9999-12-31"


@router.get("/goals/new", response_class=HTMLResponse, include_in_schema=False)
def new_goal_form(request: Request) -> Any:
    return templates.TemplateResponse(request, "goal_form.html", {"goal": None})


@router.get("/goals/{goal_id}/edit", response_class=HTMLResponse, include_in_schema=False)
def edit_goal_form(request: Request, goal_id: str) -> Any:
    log, _etag = store.load()
    goal = next((g for g in log.goals if g.id == goal_id), None)
    if goal is None:
        return RedirectResponse("/goals", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "goal_form.html", {"goal": goal})


@router.post("/goals", include_in_schema=False)
async def add_goal(request: Request) -> Any:
    form = await request.form()
    title = str(form.get("title") or "").strip()
    if not title:
        return RedirectResponse("/goals", status_code=status.HTTP_303_SEE_OTHER)

    goal = Goal(
        id=uuid.uuid4().hex[:12],
        title=title,
        target_date=str(form.get("target_date") or "").strip(),
        note=str(form.get("note") or "").strip(),
    )
    try:
        store.update(lambda current: current.with_goal(goal))
    except store.ConflictError as exc:
        raise ConflictResponse("the log was changed elsewhere; the goal was not saved") from exc
    return RedirectResponse("/goals", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/goals/{goal_id}/achieve", include_in_schema=False)
def achieve_goal(goal_id: str) -> Any:
    # Looked up and mutated inside the retry-safe closure, not before it, so a
    # conflict retries against whatever the goal actually looks like on the
    # newer document rather than clobbering it with a stale copy.
    def mark_achieved(current: Log) -> Log:
        goal = next((g for g in current.goals if g.id == goal_id), None)
        if goal is None:
            return current
        return current.with_goal(replace(goal, status="achieved"))

    try:
        store.update(mark_achieved)
    except store.ConflictError as exc:
        raise ConflictResponse("the log was changed elsewhere; the goal was not updated") from exc
    return RedirectResponse("/goals", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/goals/{goal_id}", include_in_schema=False)
async def edit_goal(request: Request, goal_id: str) -> Any:
    form = await request.form()
    title = str(form.get("title") or "").strip()
    if not title:
        return RedirectResponse("/goals", status_code=status.HTTP_303_SEE_OTHER)

    target_date = str(form.get("target_date") or "").strip()
    note = str(form.get("note") or "").strip()

    def apply_edit(current: Log) -> Log:
        existing = next((g for g in current.goals if g.id == goal_id), None)
        if existing is None:
            return current
        edited = replace(existing, title=title, target_date=target_date, note=note)
        return current.with_goal(edited)

    try:
        store.update(apply_edit)
    except store.ConflictError as exc:
        raise ConflictResponse("the log was changed elsewhere; the goal was not saved") from exc
    return RedirectResponse("/goals", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/goals/{goal_id}/delete", include_in_schema=False)
def delete_goal(goal_id: str) -> Any:
    try:
        store.update(lambda current: current.without_goal(goal_id))
    except store.ConflictError as exc:
        raise ConflictResponse("the log was changed elsewhere; the goal was not deleted") from exc
    return RedirectResponse("/goals", status_code=status.HTTP_303_SEE_OTHER)


# --- injuries & conditions -----------------------------------------------------


@router.get("/conditions", response_class=HTMLResponse, include_in_schema=False)
def conditions_list(request: Request) -> Any:
    """Read-only, most recently started first. Adding or editing has its own page."""
    log, _etag = store.load()
    return templates.TemplateResponse(
        request,
        "conditions.html",
        {
            "active": sorted(
                (c for c in log.conditions if c.status == "active"),
                key=lambda c: c.started,
                reverse=True,
            ),
            "resolved": sorted(
                (c for c in log.conditions if c.status != "active"),
                key=lambda c: c.started,
                reverse=True,
            ),
        },
    )


@router.get("/conditions/new", response_class=HTMLResponse, include_in_schema=False)
def new_condition_form(request: Request) -> Any:
    return templates.TemplateResponse(
        request, "condition_form.html", {"condition": None, "today": _today().isoformat()}
    )


@router.get("/conditions/{condition_id}/edit", response_class=HTMLResponse, include_in_schema=False)
def edit_condition_form(request: Request, condition_id: str) -> Any:
    log, _etag = store.load()
    condition = next((c for c in log.conditions if c.id == condition_id), None)
    if condition is None:
        return RedirectResponse("/conditions", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request, "condition_form.html", {"condition": condition, "today": condition.started}
    )


@router.post("/conditions", include_in_schema=False)
async def add_condition(request: Request) -> Any:
    form = await request.form()
    body_part = str(form.get("body_part") or "").strip()
    if not body_part:
        return RedirectResponse("/conditions", status_code=status.HTTP_303_SEE_OTHER)

    condition = Condition(
        id=uuid.uuid4().hex[:12],
        body_part=body_part,
        started=str(form.get("started") or _today().isoformat()),
        note=str(form.get("note") or "").strip(),
    )
    try:
        store.update(lambda current: current.with_condition(condition))
    except store.ConflictError as exc:
        raise ConflictResponse(
            "the log was changed elsewhere; the condition was not saved"
        ) from exc
    return RedirectResponse("/conditions", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/conditions/{condition_id}/resolve", include_in_schema=False)
def resolve_condition(condition_id: str) -> Any:
    today = _today().isoformat()

    def mark_resolved(current: Log) -> Log:
        condition = next((c for c in current.conditions if c.id == condition_id), None)
        if condition is None:
            return current
        return current.with_condition(replace(condition, status="resolved", resolved=today))

    try:
        store.update(mark_resolved)
    except store.ConflictError as exc:
        raise ConflictResponse(
            "the log was changed elsewhere; the condition was not updated"
        ) from exc
    return RedirectResponse("/conditions", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/conditions/{condition_id}", include_in_schema=False)
async def edit_condition(request: Request, condition_id: str) -> Any:
    form = await request.form()
    body_part = str(form.get("body_part") or "").strip()
    if not body_part:
        return RedirectResponse("/conditions", status_code=status.HTTP_303_SEE_OTHER)

    started = str(form.get("started") or "").strip()
    note = str(form.get("note") or "").strip()

    def apply_edit(current: Log) -> Log:
        existing = next((c for c in current.conditions if c.id == condition_id), None)
        if existing is None:
            return current
        edited = replace(
            existing, body_part=body_part, started=started or existing.started, note=note
        )
        return current.with_condition(edited)

    try:
        store.update(apply_edit)
    except store.ConflictError as exc:
        raise ConflictResponse(
            "the log was changed elsewhere; the condition was not saved"
        ) from exc
    return RedirectResponse("/conditions", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/conditions/{condition_id}/delete", include_in_schema=False)
def delete_condition(condition_id: str) -> Any:
    try:
        store.update(lambda current: current.without_condition(condition_id))
    except store.ConflictError as exc:
        raise ConflictResponse(
            "the log was changed elsewhere; the condition was not deleted"
        ) from exc
    return RedirectResponse("/conditions", status_code=status.HTTP_303_SEE_OTHER)


# --- Insights: an index over an AI summary and a heart-rate-zone breakdown ---


@router.get("/insights", response_class=HTMLResponse, include_in_schema=False)
def insights_index(request: Request) -> Any:
    """Just the two tiles below — neither subsection needs the log to link to itself."""
    return templates.TemplateResponse(request, "insights.html", {})


@router.get("/insights/ai", response_class=HTMLResponse, include_in_schema=False)
def ai_insights_list(request: Request) -> Any:
    log, _etag = store.load()
    return templates.TemplateResponse(
        request,
        "insights_ai.html",
        {"insights": sorted(log.insights, key=lambda i: i.generated_at, reverse=True)},
    )


@router.post("/insights/ai", include_in_schema=False)
def generate_insight_now() -> Any:
    """The same generation the scheduled job runs, done here instead of waiting for it.

    Blocks on the DeepSeek call rather than handing off to a background job:
    this is a person clicking a button once, not a timer, so the few seconds
    it takes is the acceptable trade-off for seeing the result immediately.
    """
    from ..insights import generate_insight

    log, _etag = store.load()
    new_insight = generate_insight(log)
    try:
        store.update(lambda current: current.with_insight(new_insight))
    except store.ConflictError as exc:
        raise ConflictResponse("the log was changed elsewhere; the insight was not saved") from exc
    return RedirectResponse("/insights/ai", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/insights/ai/{insight_id}/delete", include_in_schema=False)
def delete_insight(insight_id: str) -> Any:
    try:
        store.update(lambda current: current.without_insight(insight_id))
    except store.ConflictError as exc:
        raise ConflictResponse(
            "the log was changed elsewhere; the insight was not deleted"
        ) from exc
    return RedirectResponse("/insights/ai", status_code=status.HTTP_303_SEE_OTHER)


# --- Chat: a conversation with the model, grounded in the log --------------


@router.get("/chat", response_class=HTMLResponse, include_in_schema=False)
def chat_page(request: Request) -> Any:
    from .. import chat

    return templates.TemplateResponse(request, "chat.html", {"messages": chat.load()})


@router.post("/chat", include_in_schema=False)
def chat_send(message: str = Form(default="")) -> Any:
    """Send one message and wait for the reply — a blocking call, like
    "Generate insight now", and greyed out by base.html's script meanwhile."""
    from .. import chat

    text = message.strip()
    if not text:
        return RedirectResponse("/chat", status_code=status.HTTP_303_SEE_OTHER)

    log, _etag = store.load()
    try:
        conversation = chat.send(log, chat.load(), text)
    except Exception as exc:
        logger.exception("chat reply failed")
        raise ChatFailed(
            "could not get a reply from DeepSeek — your message was not saved. Try again shortly."
        ) from exc
    chat.save(conversation)
    return RedirectResponse("/chat#latest", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/chat/clear", include_in_schema=False)
def chat_clear() -> Any:
    from .. import chat

    chat.clear()
    return RedirectResponse("/chat", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/insights/hr-zones", response_class=HTMLResponse, include_in_schema=False)
def hr_zone_insights(request: Request) -> Any:
    log, _etag = store.load()
    return templates.TemplateResponse(
        request,
        "insights_hr_zones.html",
        {
            "zones": _zone_summary(log),
            "synced_at": _format_synced_at(log.garmin_synced_at),
        },
    )


def _zone_summary(log: Any) -> list[dict[str, Any]]:
    """Time in each heart rate zone, in minutes and as a share of the period.

    Two rows per zone, HR (% of max heart rate) then HRR (% of heart rate
    reserve), so the same zone under each method sits side by side. Both are
    counted from the same samples at sync; see `garmin._zones`.

    Zone 1 is left out of the result entirely, not just zeroed: `Log.
    garmin_zone_seconds_since` never accumulates it, so a "Zone 1" row would
    only ever read 0m/0% and add nothing but noise.

    The percentage is of time *tracked in zones 2-5* by that method, not of
    the whole period — there is no untracked/rest bucket to make it sum
    against, since this only ever covers workout time. `or 1` on each total
    sidesteps a division by zero when nothing has synced yet; the numerators
    are then all 0 too, so the result is a correct 0% rather than a crash.

    `bpm_label` comes from the most recent activity's boundaries (`Log.
    latest_garmin_zone_boundaries`), not this period's — they move by a beat
    or two at most, not enough for "this week's zone 3" to mean anything
    distinct from "the current one". Empty when no activity has yielded
    boundaries yet. Built here rather than in the template so the "top zone
    is open-ended" rule lives in one place.
    """
    today = _today()
    week = (today - timedelta(days=7)).isoformat()
    month = (today - timedelta(days=30)).isoformat()
    methods = []
    for method, of_max in (("HR", True), ("HRR", False)):
        week_seconds = log.garmin_zone_seconds_since(week, of_max=of_max)
        month_seconds = log.garmin_zone_seconds_since(month, of_max=of_max)
        methods.append(
            (
                method,
                week_seconds,
                sum(week_seconds) or 1,
                month_seconds,
                sum(month_seconds) or 1,
                log.latest_garmin_zone_boundaries(of_max=of_max),
            )
        )
    return [
        {
            "zone": zone + 1,
            "method": method,
            "week_minutes": week_seconds[zone] // 60,
            "week_pct": round(100 * week_seconds[zone] / week_total),
            "month_minutes": month_seconds[zone] // 60,
            "month_pct": round(100 * month_seconds[zone] / month_total),
            "bpm_label": _bpm_label(boundaries, zone),
        }
        for zone in range(1, GARMIN_ZONE_COUNT)
        for method, week_seconds, week_total, month_seconds, month_total, boundaries in methods
    ]


def _bpm_label(boundaries: tuple[int, ...], zone: int) -> str:
    """ "142-155 bpm" for a bounded zone, "161+ bpm" for the top one, "" if unknown."""
    if not boundaries:
        return ""
    low = boundaries[zone]
    if zone + 1 < len(boundaries):
        return f"{low}-{boundaries[zone + 1] - 1} bpm"
    return f"{low}+ bpm"


@router.get("/insights/running", response_class=HTMLResponse, include_in_schema=False)
def running_insights(request: Request) -> Any:
    log, _etag = store.load()
    return templates.TemplateResponse(
        request,
        "insights_running.html",
        {
            "totals": _running_totals(log),
            "weeks": _weekly_running_series(log),
            "synced_at": _format_synced_at(log.garmin_synced_at),
        },
    )


def _week_start(day: date) -> date:
    """The Monday on or before `day` — calendar weeks here always run Mon-Sun."""
    return day - timedelta(days=day.weekday())


def _running_totals(log: Any) -> dict[str, float | int]:
    """Distance run and run count, this calendar week and over the last 30 days.

    Scoped to `RUNNING_ACTIVITY_TYPES` (see `Log.garmin_running_distance_
    since`). "This week" is deliberately the same Mon-Sun bucket as
    `_weekly_running_series`'s last bar — not an independent "last 7 days"
    cutoff — so the stat card and the chart never disagree about what "this
    week" contains.
    """
    today = _today()
    monday = _week_start(today)
    week_meters, week_runs = log.garmin_running_distance_between(
        monday.isoformat(), today.isoformat()
    )
    month_meters, month_runs = log.garmin_running_distance_since(
        (today - timedelta(days=30)).isoformat()
    )
    return {
        "week_km": round(week_meters / 1000, 1),
        "week_runs": week_runs,
        "month_km": round(month_meters / 1000, 1),
        "month_runs": month_runs,
    }


def _weekly_running_series(log: Any) -> list[dict[str, Any]]:
    """Distance and run count per calendar week (Mon-Sun), oldest first, 4 weeks back.

    Bucketed on the Monday that starts the current week, not a rolling
    trailing-7-days window, so "this week" always means the same thing a
    calendar does and lines up with `_running_totals`'s figure regardless of
    which day of the week it's viewed on. 4 weeks is a compact-card choice,
    not a data limit — Garmin history goes back much further
    (`GARMIN_RETENTION_DAYS`); a longer chart is a matter of widening this
    range, not fetching more.

    `pct` is each week's share of the 4 weeks' single biggest one, not of a
    fixed scale, so the bars stay legible whether the peak week was a 5k or
    marathon-training volume; `or 1` guards the division when nothing has
    been run at all.
    """
    this_monday = _week_start(_today())
    weeks = []
    for weeks_ago in range(3, -1, -1):
        start = this_monday - timedelta(days=weeks_ago * 7)
        end = start + timedelta(days=6)
        meters, runs = log.garmin_running_distance_between(start.isoformat(), end.isoformat())
        weeks.append(
            {
                "meters": meters,
                "km": round(meters / 1000, 1),
                "runs": runs,
                "label": _week_label(weeks_ago),
                "range": f"{start.strftime('%d %b')}-{end.strftime('%d %b')}",
            }
        )
    peak_meters = max((w["meters"] for w in weeks), default=0) or 1
    for w in weeks:
        w["pct"] = round(100 * w["meters"] / peak_meters)
        del w["meters"]
    return weeks


def _week_label(weeks_ago: int) -> str:
    if weeks_ago == 0:
        return "This week"
    if weeks_ago == 1:
        return "Last week"
    return f"{weeks_ago} wks ago"


@router.get("/insights/strength", response_class=HTMLResponse, include_in_schema=False)
def strength_insights(request: Request, exercise: str = "") -> Any:
    log, _etag = store.load()
    block = log.current_block
    today = _today()
    e1rm = _e1rm_by_exercise(log, block)
    if exercise not in e1rm:
        exercise = next(iter(e1rm), "")
    return templates.TemplateResponse(
        request,
        "insights_strength.html",
        {
            "block": block,
            "adherence": _adherence(log, block, today),
            "e1rm": e1rm,
            "exercise": exercise,
            "e1rm_chart": _e1rm_chart(e1rm.get(exercise, [])),
            "volume_chart": _volume_chart(log, today),
            "stalled": _stalled(log, block),
            "prs": _recent_prs(log, today),
        },
    )


def _e1rm(set_log: Any) -> float:
    """Estimated one-rep max by Epley: weight x (1 + reps/30). 0 for an unloaded set.

    An estimate that drifts high past 10-12 reps, which is fine here: it is
    compared only with itself, on the same movement in the same rep range.
    """
    if set_log.weight <= 0 or set_log.reps <= 0:
        return 0.0
    return set_log.weight * (1 + set_log.reps / 30)


def _best_e1rm(entry: Any) -> float:
    return max((_e1rm(s) for s in entry.sets), default=0.0)


def _e1rm_by_exercise(log: Any, block: Any) -> dict[str, list[dict[str, Any]]]:
    """Each movement's best estimated 1RM per session in the current block, oldest first.

    Keyed by movement, not slot: a block's two days can put different
    movements in one slot, and a line through both would chart the gap
    between them as progress. The current block only, for the same reason
    across a rotation. Ordered by `log.slots`, then by first appearance;
    a movement with nothing loaded is left out.
    """
    if block is None:
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for session in sorted(log.sessions, key=lambda s: s.date):
        if session.block != block.id:
            continue
        for entry in session.entries:
            best = _best_e1rm(entry)
            if best:
                out.setdefault(entry.exercise, []).append(
                    {"date": session.date, "slot": entry.slot, "e1rm": best}
                )
    order = {slot: i for i, slot in enumerate(log.slots)}
    return dict(sorted(out.items(), key=lambda item: order.get(item[1][0]["slot"], len(order))))


def _e1rm_chart(points: list[dict[str, Any]]) -> dict[str, Any]:
    """One movement's e1RM line, a point a session. Ticks only at the ends: a block
    is 16 sessions, too many dates for a phone's width."""
    if len(points) < 2:
        return {}
    last = len(points) - 1
    chart = _line_chart(
        [
            {
                "tick": _short_date(p["date"]) if i in (0, last) else "",
                "when": _short_date(p["date"]),
                "value": p["e1rm"],
            }
            for i, p in enumerate(points)
        ],
        _kg_label,
        _kg_label,
        from_zero=False,
        min_step=2.5,
    )
    first, latest = points[0]["e1rm"], points[-1]["e1rm"]
    chart["headline"] = _kg_label(latest)
    chart["period"] = "latest, this block"
    chart["change_pct"] = round(100 * (latest - first) / first)
    return chart


def _volume_chart(log: Any, today: date) -> dict[str, Any]:
    """Weekly volume (reps x weight, every set) for the last 4 calendar weeks.

    Every session counts, in or out of the block, since this is about how much
    work was done rather than progress on one movement. An empty week is 0,
    not a gap: no lifting is a real zero here, unlike an unworn watch.
    """
    this_monday = _week_start(today)
    points = []
    for weeks_ago in range(3, -1, -1):
        start = this_monday - timedelta(weeks=weeks_ago)
        end = start + timedelta(days=6)
        kg = sum(
            s.reps * s.weight
            for session in log.sessions
            if start.isoformat() <= session.date <= end.isoformat()
            for entry in session.entries
            for s in entry.sets
        )
        points.append(
            {
                "tick": start.strftime("%d %b"),
                "when": f"Week of {start.strftime('%d %b')}",
                "value": kg,
            }
        )
    if not any(p["value"] for p in points):
        return {}
    chart = _line_chart(points, _volume_label, _volume_tick, from_zero=True, min_step=0)
    chart["headline"] = chart["points"][-1]["label"]
    chart["period"] = "this week"
    return chart


def _volume_label(kg: float) -> str:
    return f"{round(kg):,}"


def _volume_tick(kg: float) -> str:
    return f"{kg / 1000:g}K" if kg else "0"


def _adherence(log: Any, block: Any, today: date) -> dict[str, Any]:
    """Sessions logged this block against what its days ask for.

    "Expected" counts whole weeks behind you plus the current week in full,
    capped at the block's length: the current week is a target to hit, and
    the number to beat is shown as "this week", not as a shortfall.
    """
    if block is None or not block.days:
        return {}
    per_week = len(block.days)
    week = min(block.week_of(today), block.weeks)
    started = date.fromisoformat(block.started) if block.started else today
    this_week_start = started + timedelta(weeks=week - 1)
    done = [s for s in log.sessions if s.block == block.id and s.day in block.days]
    this_week = [s for s in done if s.date >= this_week_start.isoformat()]
    expected = per_week * week
    return {
        "done": len(done),
        "expected": expected,
        "pct": _gauge_pct(len(done), expected),
        "this_week": len(this_week),
        "per_week": per_week,
    }


def _stalled(log: Any, block: Any) -> list[dict[str, Any]]:
    """Movements in the current block whose last outing fell short of the range.

    The same rule the session screen flags (`progression.suggest`'s
    `stalled`), so the two never disagree about what "stalled" means.
    """
    if block is None:
        return []
    out = []
    for day in block.days.values():
        for exercise in day.exercises:
            previous = log.last_entry(exercise.name)
            if previous is None:
                continue
            suggestion = suggest(exercise, *previous)
            if suggestion is not None and suggestion.stalled:
                out.append(
                    {
                        "exercise": exercise.name,
                        "slot": exercise.slot,
                        "reason": suggestion.reason,
                        "date": _short_date(previous[1]),
                    }
                )
    return out


# How far back the strength page looks for personal records.
PR_WINDOW_DAYS = 30


def _recent_prs(log: Any, today: date) -> list[dict[str, Any]]:
    """Sessions in the last `PR_WINDOW_DAYS` that beat a movement's best e1RM, newest first.

    Against every earlier session of that movement, across blocks, since a
    record is a record whichever rotation it fell in. A first outing sets the
    bar rather than counting as a record.
    """
    cutoff = (today - timedelta(days=PR_WINDOW_DAYS)).isoformat()
    best: dict[str, float] = {}
    prs = []
    for session in sorted(log.sessions, key=lambda s: s.date):
        for entry in session.entries:
            e1rm = _best_e1rm(entry)
            if not e1rm:
                continue
            previous = best.get(entry.exercise)
            if previous is not None and e1rm > previous and session.date >= cutoff:
                top = max(entry.sets, key=_e1rm)
                prs.append(
                    {
                        "exercise": entry.exercise,
                        "date": session.date,
                        "when": _short_date(session.date),
                        "e1rm": _kg_label(e1rm),
                        "previous": _kg_label(previous),
                        "set": f"{top.reps} x {top.weight:g}kg",
                    }
                )
            best[entry.exercise] = max(e1rm, previous or 0)
    return list(reversed(prs))


@router.get("/insights/fitness", response_class=HTMLResponse, include_in_schema=False)
def fitness_insights(request: Request, span: str = "week") -> Any:
    log, _etag = store.load()
    if span not in FITNESS_SPANS:
        span = "week"
    today = _today()
    return templates.TemplateResponse(
        request,
        "insights_fitness.html",
        {
            "glance": _at_a_glance(log, today),
            "recovery": _recovery(log, today),
            "trends": _trends(log, today),
            "span": span,
            "steps_chart": _fitness_chart(
                log, today, span, lambda d: d.steps, _steps_label, _steps_tick, from_zero=True
            ),
            "sleep_chart": _fitness_chart(
                log,
                today,
                span,
                lambda d: d.sleep_seconds / 3600,
                _hours_label,
                _hours_tick,
                min_step=1,
                overnight=True,
            ),
            "hrv_chart": _fitness_chart(
                log,
                today,
                span,
                lambda d: d.hrv_ms,
                _ms_label,
                _ms_label,
                min_step=5,
                overnight=True,
            ),
            "hrv_status": _hrv_status(log),
            "resting_hr_chart": _fitness_chart(
                log,
                today,
                span,
                lambda d: d.resting_hr,
                _round_label,
                _round_label,
                min_step=1,
            ),
            "max_hr_chart": _fitness_chart(
                log,
                today,
                span,
                lambda d: d.max_hr,
                _round_label,
                _round_label,
                min_step=1,
            ),
            "stress_chart": _fitness_chart(
                log,
                today,
                span,
                lambda d: d.stress_avg,
                _round_label,
                _round_label,
                from_zero=True,
            ),
            # Garmin's latest reading, stamped with the day the sync ran (see
            # `GarminFitness`), so today's is as complete as it gets: overnight.
            "vo2max_chart": _fitness_chart(
                log,
                today,
                span,
                lambda f: f.vo2max,
                _vo2max_label,
                _vo2max_label,
                min_step=1,
                overnight=True,
                records=log.garmin_fitness,
            ),
            "lt_bpm_chart": _fitness_chart(
                log,
                today,
                span,
                lambda f: f.lactate_threshold_bpm,
                _round_label,
                _round_label,
                min_step=1,
                overnight=True,
                records=log.garmin_fitness,
            ),
            "lt_pace_chart": _fitness_chart(
                log,
                today,
                span,
                lambda f: f.lactate_threshold_pace_seconds_per_km,
                _pace_value_label,
                _pace_value_label,
                min_step=5,
                overnight=True,
                records=log.garmin_fitness,
            ),
            "intensity": _intensity_week(log, today),
            "body_battery_chart": _fitness_chart(
                log,
                today,
                span,
                lambda d: d.body_battery_high,
                _round_label,
                _round_label,
                min_step=5,
            ),
            "readiness_chart": _fitness_chart(
                log,
                today,
                span,
                lambda d: d.training_readiness,
                _round_label,
                _round_label,
                min_step=5,
                overnight=True,
            ),
            # A weigh-in today is as complete as it gets, so it counts.
            "weight_chart": _fitness_chart(
                log,
                today,
                span,
                lambda d: d.weight_grams / 1000,
                _kg_label,
                _kg_label,
                min_step=0.5,
                overnight=True,
            ),
            "synced_at": _format_synced_at(log.garmin_synced_at),
        },
    )


def _at_a_glance(log: Any, today: date) -> dict[str, Any]:
    """The last finished day's steps/resting-HR/stress, last night's sleep, VO2max and LT.

    Not today: a sync mid-morning has today's resting HR and little else, so
    the latest synced day was a snapshot of mostly dashes. The last finished
    day is yesterday, or the most recent before it if yesterday never synced.

    Sleep is the exception, as `overnight` is in `_fitness_chart`: Garmin dates
    a night by the morning it ends, so today's record is last night's and
    yesterday's is the night before. It is today's or nothing - falling back
    to an older night would show that one as if it were last night.

    The lactate threshold rides along with VO2max: both come from the latest
    `GarminFitness`, so they share its staleness. Built from whichever of
    `garmin_days`/`garmin_fitness` exists, and each independently — not
    "both or neither" — since VO2max only ever gets a value on the day the
    sync ran (see `GarminFitness`), so it can legitimately be a day or more
    stale relative to the latest `GarminDay`, or vice versa if one sub-fetch
    failed on the more recent sync.
    """
    finished = [d for d in log.garmin_days if d.date < today.isoformat()]
    day = finished[-1] if finished else None
    night = next((d for d in log.garmin_days if d.date == today.isoformat()), None)
    fitness = log.latest_garmin_fitness
    if day is None and night is None and fitness is None:
        return {}
    steps_goal = (day.steps_goal if day else 0) or DEFAULT_STEPS_GOAL
    return {
        "steps": day.steps if day else 0,
        "steps_goal": steps_goal,
        "steps_pct": _gauge_pct(day.steps if day else 0, steps_goal),
        "resting_hr": day.resting_hr if day else 0,
        "max_hr": day.max_hr if day else 0,
        "sleep_hours": (night.sleep_seconds // 3600) if night else 0,
        "sleep_minutes": ((night.sleep_seconds % 3600) // 60) if night else 0,
        "sleep_score": night.sleep_score if night else 0,
        "sleep_quality": _band(night.sleep_score if night else 0, SLEEP_SCORE_BANDS),
        "stress_avg": day.stress_avg if day else 0,
        "stress_level": _band(day.stress_avg if day else 0, STRESS_BANDS),
        "vo2max": fitness.vo2max if fitness else 0,
        "lactate_threshold_bpm": fitness.lactate_threshold_bpm if fitness else 0,
        "lactate_threshold_pace": (
            _pace_label(fitness.lactate_threshold_pace_seconds_per_km) if fitness else ""
        ),
        "as_of": _short_date(day.date if day else fitness.date) if day or fitness else "",
        "body_battery_high": day.body_battery_high if day else 0,
        "body_battery_low": day.body_battery_low if day else 0,
        # This morning's, like sleep: Garmin scores readiness on waking.
        "readiness": night.training_readiness if night else 0,
        "readiness_level": _title(night.training_readiness_level) if night else "",
        **_load_balance(fitness),
    }


def _title(garmin_word: str) -> str:
    """ "Moderate" from Garmin's "MODERATE", "Very High" from "VERY_HIGH"."""
    return garmin_word.replace("_", " ").title()


def _load_balance(fitness: Any) -> dict[str, Any]:
    """Acute over chronic training load, and a word for where it sits.

    The bands are the usual acute:chronic workload ones: under 0.8 is
    detraining, over 1.5 is a jump in load worth easing off from.
    """
    if fitness is None or not fitness.acute_load or not fitness.chronic_load:
        return {"load_ratio": "", "load_level": ""}
    ratio = fitness.acute_load / fitness.chronic_load
    level = "Low" if ratio < 0.8 else "High" if ratio > 1.5 else "Optimal"
    return {"load_ratio": f"{ratio:.1f}", "load_level": level}


# Garmin's default weekly goal, and the WHO's guideline for adults.
INTENSITY_MINUTES_GOAL = 150


def _intensity_week(log: Any, today: date) -> dict[str, Any]:
    """Intensity minutes this calendar week (Mon-today) against the weekly goal.

    Today counts even though it's unfinished: the total only ever goes up,
    and "how far to go this week" is the question it answers. Empty until any
    day this week has synced.
    """
    monday = _week_start(today).isoformat()
    week = [d for d in log.garmin_days if monday <= d.date <= today.isoformat()]
    if not week:
        return {}
    minutes = sum(d.intensity_minutes for d in week)
    return {
        "minutes": minutes,
        "goal": INTENSITY_MINUTES_GOAL,
        "pct": _gauge_pct(minutes, INTENSITY_MINUTES_GOAL),
    }


# Garmin's own wording for both scales, lower bound first, so a gauge reads the
# way the watch does. A zero is "no reading" on both and gets no word at all.
SLEEP_SCORE_BANDS = ((1, "Poor"), (60, "Fair"), (80, "Good"), (90, "Excellent"))
STRESS_BANDS = ((1, "Rest"), (26, "Low"), (51, "Medium"), (76, "High"))
# Garmin's starting goal, for days synced before `steps_goal` was kept.
DEFAULT_STEPS_GOAL = 10000


def _band(value: int, bands: tuple[tuple[int, str], ...]) -> str:
    return next((name for low, name in reversed(bands) if value >= low), "")


def _gauge_pct(value: float, full: float) -> int:
    """How much of a gauge's arc to fill: 0-100, a goal beaten still a full arc."""
    return max(0, min(100, round(100 * value / full))) if full else 0


# The weekly line charts' drawing area, in SVG user units. The left margin
# holds the y-axis tick labels, the bottom one the first and last week's.
CHART_WIDTH, CHART_HEIGHT = 320, 132
CHART_LEFT, CHART_RIGHT, CHART_TOP, CHART_BOTTOM = 36, 300, 12, 108

# The fitness page's toggle: the past 7 days a point a day, or the last 4
# calendar weeks a point a week.
FITNESS_SPANS = ("week", "month")


def _fitness_chart(
    log: Any,
    today: date,
    span: str,
    value: Callable[[Any], float],
    label: Callable[[float], str],
    tick: Callable[[float], str],
    from_zero: bool = False,
    min_step: float = 0,
    overnight: bool = False,
    records: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """A daily Garmin value as a line chart: a point a day for the past week,
    or a weekly average for the last 4 calendar weeks (`span` "month").

    Finished days only, as `_at_a_glance`: today's half-counted steps would
    drag the line down every morning. `overnight` values are the exception:
    Garmin dates sleep and HRV by the morning they end, so today's is last
    night's and already complete. A day with no reading (zero) is left out
    rather than counted as nothing, and a day or week with no readings is a
    break in the line rather than a drop to zero.

    The headline is the average over everything plotted for the week view
    (one day alone says little), and the latest week's for the month view.

    `records` is anything dated by an ISO `date`, `log.garmin_days` unless
    given: VO2max and the lactate threshold live in `log.garmin_fitness`.
    """
    last_day = today if overnight else today - timedelta(days=1)
    rows = log.garmin_days if records is None else records
    by_date = {d.date: value(d) for d in rows if d.date <= last_day.isoformat()}

    def mean(start: date, end: date) -> float | None:
        values = [
            v
            for when, v in by_date.items()
            if start.isoformat() <= when <= end.isoformat() and v > 0
        ]
        return sum(values) / len(values) if values else None

    points: list[dict[str, Any]] = []
    if span == "month":
        this_monday = _week_start(today)
        for weeks_ago in range(3, -1, -1):
            start = this_monday - timedelta(weeks=weeks_ago)
            points.append(
                {
                    "tick": start.strftime("%d %b"),
                    "when": f"Week of {start.strftime('%d %b')}",
                    "value": mean(start, min(start + timedelta(days=6), last_day)),
                }
            )
    else:
        for days_ago in range(6, -1, -1):
            day = last_day - timedelta(days=days_ago)
            points.append(
                {
                    "tick": day.strftime("%a"),
                    "when": day.strftime("%a %d %b"),
                    "value": mean(day, day),
                }
            )
    known = [p["value"] for p in points if p["value"] is not None]
    if len(known) < 2:
        return {}
    chart = _line_chart(points, label, tick, from_zero, min_step)
    if span == "month":
        chart["headline"] = chart["points"][-1]["label"]
        chart["period"] = f"week of {chart['points'][-1]['tick']}"
    else:
        chart["headline"] = label(sum(known) / len(known))
        chart["period"] = "past 7 days"
    return chart


def _line_chart(
    points: list[dict[str, Any]],
    label: Callable[[float], str],
    tick: Callable[[float], str],
    from_zero: bool,
    min_step: float,
) -> dict[str, Any]:
    """Lay `points` (oldest first, `value` None for a gap) out on the chart's grid.

    The geometry is worked out here rather than in the template so a wrong
    scale shows up in a test. Steps sit on a zero baseline; sleep doesn't,
    because its average moves within an hour or so and a zero-based axis
    would flatten that to a straight line.
    """
    known = [p["value"] for p in points if p["value"] is not None]
    low, high, step = _ticks(0 if from_zero else min(known), max(known), min_step)

    def y(v: float) -> float:
        return round(CHART_BOTTOM - (v - low) / (high - low) * (CHART_BOTTOM - CHART_TOP), 1)

    gap = (CHART_RIGHT - CHART_LEFT) / (len(points) - 1)
    path, pen_down = [], False
    for index, point in enumerate(points):
        point["x"] = round(CHART_LEFT + index * gap, 1)
        if point["value"] is None:
            pen_down = False
            continue
        point["y"] = y(point["value"])
        point["label"] = label(point["value"])
        path.append(f"{'L' if pen_down else 'M'}{point['x']},{point['y']}")
        pen_down = True

    grid = []
    at = low
    while at <= high + step / 2:
        grid.append({"y": y(at), "label": tick(at)})
        at += step
    return {
        "width": CHART_WIDTH,
        "height": CHART_HEIGHT,
        "left": CHART_LEFT,
        "right": CHART_RIGHT,
        "path": " ".join(path),
        "points": [p for p in points if p["value"] is not None],
        # Every slot gets an x-axis label, gaps included, so a missing day
        # or week reads as an empty slot rather than a squeezed axis.
        "ticks": [{"x": p["x"], "label": p["tick"]} for p in points],
        "grid": grid,
    }


def _ticks(low: float, high: float, min_step: float = 0) -> tuple[float, float, float]:
    """A clean axis around `low`..`high`: (bottom, top, step), about three steps.

    The step is 1, 2, 2.5 or 5 times a power of ten, and the ends are rounded
    out to it, so the gridlines land on numbers worth reading (0/5K/10K).
    `min_step` stops a narrow range splitting into steps that read badly in its
    unit: a week's sleep moving within an hour would otherwise tick 7.25h.
    """
    span = (high - low) or abs(high) or 1
    magnitude = 10 ** math.floor(math.log10(span / 3))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if span / (m * magnitude) <= 3)
    step = max(step, min_step)
    bottom = math.floor(low / step) * step
    top = math.ceil(high / step) * step
    if top == bottom:
        top += step
    return bottom, top, step


def _steps_label(steps: float) -> str:
    return f"{round(steps):,}"


def _steps_tick(steps: float) -> str:
    return f"{steps / 1000:g}K" if steps else "0"


def _hours_label(hours: float) -> str:
    minutes = round(hours * 60)
    return f"{minutes // 60}h {minutes % 60}m"


def _hours_tick(hours: float) -> str:
    return f"{hours:g}h"


def _ms_label(ms: float) -> str:
    return f"{round(ms)}ms"


def _round_label(value: float) -> str:
    return f"{round(value)}"


def _kg_label(kg: float) -> str:
    return f"{kg:.1f}".removesuffix(".0")


def _vo2max_label(vo2max: float) -> str:
    return f"{vo2max:.1f}".removesuffix(".0")


def _pace_value_label(seconds_per_km: float) -> str:
    """ "4:18" from a pace in seconds per km; `_pace_label` without the unit."""
    return _pace_label(round(seconds_per_km)).removesuffix(" /km")


def _hrv_status(log: Any) -> str:
    """Garmin's verdict on the latest night with an HRV reading, e.g. "Balanced"."""
    nights = [d for d in log.garmin_days if d.hrv_ms]
    return nights[-1].hrv_status.replace("_", " ").title() if nights else ""


# How far back the recovery card's "normal" reaches, and how many readings in
# that window it needs before a band means anything.
RECOVERY_BASELINE_DAYS = 30
RECOVERY_MIN_READINGS = 7

# (label, GarminDay field, unit, whether higher is better)
RECOVERY_SIGNALS = (
    ("HRV", "hrv_ms", "ms", True),
    ("Resting HR", "resting_hr", "bpm", False),
    ("Sleep score", "sleep_score", "", True),
    ("Readiness", "training_readiness", "", True),
    ("Body Battery", "body_battery_high", "", True),
)


def _recovery(log: Any, today: date) -> dict[str, Any]:
    """Last night's HRV, resting HR, sleep score, readiness and Body Battery
    peak against your own normal.

    "Normal" is the mean plus or minus one standard deviation of the previous
    `RECOVERY_BASELINE_DAYS` days, not counting today and ignoring zeros
    (unworn nights). Today's `GarminDay` is the one to judge: Garmin dates
    sleep and HRV by the morning they end, and a morning sync already has
    today's resting HR. A signal without a reading today, or with fewer than
    `RECOVERY_MIN_READINGS` behind it, is left out; with none left the card
    is hidden.

    The band is at least 1 wide either side, so a month of identical whole
    numbers doesn't call a one-beat change abnormal.

    Each signal is "better", "worse" or "normal" rather than above/below,
    since a high resting HR is the bad direction. The verdict: two or more
    worse is "Take it easy", one is "Normal", none with at least one better
    is "Recovered".
    """
    night = next((d for d in log.garmin_days if d.date == today.isoformat()), None)
    if night is None:
        return {}
    since = (today - timedelta(days=RECOVERY_BASELINE_DAYS)).isoformat()
    history = [d for d in log.garmin_days if since <= d.date < today.isoformat()]

    signals = []
    for label, field, unit, higher_is_better in RECOVERY_SIGNALS:
        value = getattr(night, field)
        past = [getattr(d, field) for d in history if getattr(d, field)]
        if not value or len(past) < RECOVERY_MIN_READINGS:
            continue
        mean = statistics.fmean(past)
        band = max(statistics.pstdev(past), 1)
        low, high = mean - band, mean + band
        if low <= value <= high:
            state = "normal"
        elif (value > high) == higher_is_better:
            state = "better"
        else:
            state = "worse"
        signals.append(
            {
                "label": label,
                "value": value,
                "unit": unit,
                "state": state,
                "normal": f"{round(low)}-{round(high)}",
            }
        )
    if not signals:
        return {}

    worse = sum(s["state"] == "worse" for s in signals)
    better = sum(s["state"] == "better" for s in signals)
    if worse >= 2:
        verdict = "Take it easy"
    elif worse == 0 and better:
        verdict = "Recovered"
    else:
        verdict = "Normal"
    return {"verdict": verdict, "signals": signals, "as_of": _short_date(night.date)}


# (label, unit, value, label format, higher is better (None: neither),
#  overnight, read from log.garmin_fitness rather than log.garmin_days)
TREND_METRICS: tuple[
    tuple[str, str, Callable[[Any], float], Callable[[float], str], bool | None, bool, bool], ...
] = (
    ("Steps", "", lambda d: d.steps, _steps_label, True, False, False),
    ("Sleep", "", lambda d: d.sleep_seconds / 3600, _hours_label, True, True, False),
    ("Sleep score", "", lambda d: d.sleep_score, _round_label, True, True, False),
    ("HRV", "ms", lambda d: d.hrv_ms, _round_label, True, True, False),
    ("Resting HR", "bpm", lambda d: d.resting_hr, _round_label, False, False, False),
    ("Max HR", "bpm", lambda d: d.max_hr, _round_label, None, False, False),
    ("Stress", "", lambda d: d.stress_avg, _round_label, False, False, False),
    ("Body Battery", "", lambda d: d.body_battery_high, _round_label, True, False, False),
    ("Readiness", "", lambda d: d.training_readiness, _round_label, True, True, False),
    ("Intensity", "min", lambda d: d.intensity_minutes, _round_label, True, False, False),
    ("VO2 max", "", lambda d: d.vo2max, _vo2max_label, True, True, True),
    ("Weight", "kg", lambda d: d.weight_grams / 1000, _kg_label, None, False, False),
)

# A change smaller than this, as a fraction of the earlier average, reads as flat.
TREND_FLAT = 0.01


def _trends(log: Any, today: date) -> list[dict[str, Any]]:
    """Each Garmin metric's latest finished day, and its 7-day and 4-week
    averages against the 7 days and 4 weeks before them.

    "Yesterday" is yesterday for daily totals, as `_fitness_chart`: today's
    are half counted. Overnight values (sleep, HRV, readiness) are dated by
    the morning they end, so last night's is today's record. Zeros are days
    without a reading and count nowhere. A metric with nothing in the last 4
    weeks is left out.

    Each change is "better", "worse" or "flat" (under `TREND_FLAT`), or
    "up"/"down" for a metric with no better direction (max HR, weight).
    """
    out = []
    for label, unit, value, fmt, higher_is_better, overnight, fitness in TREND_METRICS:
        last_day = today if overnight else today - timedelta(days=1)
        rows = log.garmin_fitness if fitness else log.garmin_days
        by_date = {r.date: value(r) for r in rows if r.date <= last_day.isoformat()}
        month = _trend_period(by_date, last_day, 28, fmt, higher_is_better)
        if month is None:
            continue
        latest = by_date.get(last_day.isoformat()) or 0
        out.append(
            {
                "label": label,
                "unit": unit,
                "yesterday": fmt(latest) if latest else "",
                "week": _trend_period(by_date, last_day, 7, fmt, higher_is_better),
                "month": month,
            }
        )
    return out


def _trend_mean(by_date: dict[str, float], end: date, length: int) -> float | None:
    """The mean of the non-zero readings in the `length` days ending `end`."""
    start = (end - timedelta(days=length - 1)).isoformat()
    values = [v for when, v in by_date.items() if start <= when <= end.isoformat() and v > 0]
    return statistics.fmean(values) if values else None


def _trend_period(
    by_date: dict[str, float],
    last_day: date,
    length: int,
    fmt: Callable[[float], str],
    higher_is_better: bool | None,
) -> dict[str, Any] | None:
    """The `length` days ending `last_day` against the `length` days before."""
    current = _trend_mean(by_date, last_day, length)
    if current is None:
        return None
    previous = _trend_mean(by_date, last_day - timedelta(days=length), length)
    trend = {"value": fmt(current), "change": "", "arrow": "", "pct": ""}
    if not previous:
        return trend
    ratio = (current - previous) / previous
    if abs(ratio) < TREND_FLAT:
        trend["change"] = "flat"
        return trend
    if higher_is_better is None:
        trend["change"] = "up" if ratio > 0 else "down"
    else:
        trend["change"] = "better" if (ratio > 0) == higher_is_better else "worse"
    trend["arrow"] = "▲" if ratio > 0 else "▼"
    trend["pct"] = f"{abs(ratio):.0%}"
    return trend


def _short_date(iso_date: str) -> str:
    """ "26 Sep" from an ISO date, or "" if it doesn't parse."""
    try:
        return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d %b")
    except ValueError:
        return ""


def _pace_label(seconds_per_km: int) -> str:
    """ "4:32 /km" from a per-km pace in seconds, or "" if there isn't one."""
    if seconds_per_km <= 0:
        return ""
    return f"{seconds_per_km // 60}:{seconds_per_km % 60:02d} /km"


# --- Garmin sync: a cut-down browser for the last 90 days of synced data -----


@router.get("/garmin", response_class=HTMLResponse, include_in_schema=False)
def garmin_list(request: Request, view: str = "activities", activity_type: str = "") -> Any:
    """Activities and days are two different shapes of data — steps/sleep vs.
    duration/heart rate — so only one is ever on screen, picked by `view`
    (`?view=days` for the other; anything else falls back to activities
    rather than 404ing on a typo'd link).

    `activity_type` narrows the activities list to one Garmin activity type
    (`running`, `cycling`, ...); the options offered come from the *full*
    list, not the filtered one, so picking "running" does not make every
    other type disappear from the chip row too.
    """
    log, _etag = store.load()
    activities = sorted(log.garmin_activities, key=lambda a: a.date, reverse=True)
    activity_types = sorted({a.activity_type for a in activities if a.activity_type})
    if activity_type:
        activities = [a for a in activities if a.activity_type == activity_type]
    return templates.TemplateResponse(
        request,
        "garmin.html",
        {
            "view": view if view == "days" else "activities",
            "activities": activities,
            "activity_types": activity_types,
            "selected_type": activity_type,
            "days": sorted(log.garmin_days, key=lambda d: d.date, reverse=True),
            "synced_at": _format_synced_at(log.garmin_synced_at),
        },
    )


def _format_synced_at(raw: str) -> str:
    """ "24 Sep 2026, 14:32 UTC" from the stored ISO timestamp, or "" before any sync.

    Formatted here rather than in the template so a change of format is one
    line, not a `.replace()` chain wherever it's shown.
    """
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return ""
    return parsed.strftime("%-d %b %Y, %H:%M UTC")


@router.post("/garmin/sync", include_in_schema=False)
def sync_garmin_now() -> Any:
    """Same generation the scheduled job runs, triggered from the page instead.

    Blocks on the Garmin API calls rather than handing off to a background
    job — same trade-off as the insight button: a person clicking once, not a
    timer, so the wait is worth seeing the result immediately.
    """
    from ..garmin import GARMIN_RETENTION_DAYS, sync_garmin

    try:
        activities, days, fitness = sync_garmin()
    except Exception as exc:
        logger.warning("manual garmin sync failed", exc_info=True)
        raise GarminSyncFailed(
            "could not reach Garmin — the credentials may need refreshing, or Garmin's "
            "own service may be down. Try again shortly."
        ) from exc

    keep_since = (_today() - timedelta(days=GARMIN_RETENTION_DAYS)).isoformat()
    synced_at = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        store.update(
            lambda current: current.with_garmin_sync(
                activities, days, keep_since, synced_at, fitness
            )
        )
    except store.ConflictError as exc:
        raise ConflictResponse("the log was changed elsewhere; the sync was not saved") from exc
    return RedirectResponse("/garmin", status_code=status.HTTP_303_SEE_OTHER)


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


def _known_exercise_names(log: Any) -> list[str]:
    """Every exercise name on record: prescribed by any block, or ever logged.

    Spans every block rather than just the current one, so a name retired by
    a rotation is still offered — the point is catching a near-duplicate of
    something performed months ago, which is exactly when it is easiest to
    forget the exact wording used last time.
    """
    names = {ex.name for b in log.blocks for d in b.days.values() for ex in d.exercises}
    names.update(e.exercise for s in log.sessions for e in s.entries)
    return sorted(names)


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
