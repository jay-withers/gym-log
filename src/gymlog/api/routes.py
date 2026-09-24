"""Every page the application serves.

Server-rendered Jinja2. `POST` handlers redirect rather than render, so a reload
after logging a session does not offer to submit it twice — which on an
append-only log would mean a duplicate session rather than a harmless no-op.
"""

from __future__ import annotations

import logging
import pathlib
import re
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import store
from ..model import Achievement, Block, Condition, Day, Entry, Exercise, Goal, Log, Session, SetLog
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

# Days that carry no block prescription at all — logged purely by hand, via
# `log_extra_exercise`. A dict rather than a set so each has a label the same
# way a block's own days do, and so a second one is as cheap to add as this
# first one was. "manual" is not tied to a day of the week: it exists so
# anything can be logged any time, not just Tuesday or Thursday.
LOOSE_DAYS: dict[str, str] = {"manual": "Manual"}


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

    `start_url` points straight at `/strength` rather than the new home page:
    the phone icon exists for the twice-a-week fast path of logging a set, and
    a menu screen in front of it would be a tap this manifest exists to save.
    """
    return JSONResponse(
        {
            "name": "Health",
            "short_name": "Health",
            "start_url": "/strength",
            "display": "standalone",
            "background_color": "#101014",
            "theme_color": "#101014",
            "icons": [],
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
    history = log.excluding(today, day)
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

    logger.info("recorded extra exercise %s on %s day %s", name, when, day)
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
    return RedirectResponse("/strength", status_code=status.HTTP_303_SEE_OTHER)


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
def new_achievement_form(request: Request) -> Any:
    return templates.TemplateResponse(
        request, "achievement_form.html", {"achievement": None, "today": _today().isoformat()}
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


@router.get("/insights/hr-zones", response_class=HTMLResponse, include_in_schema=False)
def hr_zone_insights(request: Request) -> Any:
    log, _etag = store.load()
    return templates.TemplateResponse(
        request, "insights_hr_zones.html", {"zones": _zone_summary(log)}
    )


def _zone_summary(log: Any) -> list[dict[str, int]]:
    """Time in each heart rate zone, in minutes and as a share of the period.

    The percentage is of time *tracked in a zone*, not of the whole period —
    there is no untracked/rest bucket to make it sum against, since this only
    ever covers workout time. `or 1` on each total sidesteps a division by
    zero when nothing has synced yet; the numerators are then all 0 too, so
    the result is a correct 0% rather than a crash.
    """
    today = _today()
    week_seconds = log.garmin_zone_seconds_since((today - timedelta(days=7)).isoformat())
    month_seconds = log.garmin_zone_seconds_since((today - timedelta(days=30)).isoformat())
    week_total = sum(week_seconds) or 1
    month_total = sum(month_seconds) or 1
    return [
        {
            "zone": zone + 1,
            "week_minutes": week_seconds[zone] // 60,
            "week_pct": round(100 * week_seconds[zone] / week_total),
            "month_minutes": month_seconds[zone] // 60,
            "month_pct": round(100 * month_seconds[zone] / month_total),
        }
        for zone in range(len(week_seconds))
    ]


# --- Garmin sync: a cut-down browser for the last 30 days of synced data -----


@router.get("/garmin", response_class=HTMLResponse, include_in_schema=False)
def garmin_list(request: Request) -> Any:
    log, _etag = store.load()
    return templates.TemplateResponse(
        request,
        "garmin.html",
        {
            "activities": sorted(log.garmin_activities, key=lambda a: a.date, reverse=True),
            "days": sorted(log.garmin_days, key=lambda d: d.date, reverse=True),
        },
    )


@router.post("/garmin/sync", include_in_schema=False)
def sync_garmin_now() -> Any:
    """Same generation the scheduled job runs, triggered from the page instead.

    Blocks on the Garmin API calls rather than handing off to a background
    job — same trade-off as the insight button: a person clicking once, not a
    timer, so the wait is worth seeing the result immediately.
    """
    from ..garmin import GARMIN_RETENTION_DAYS, sync_garmin

    activities, days = sync_garmin()
    keep_since = (_today() - timedelta(days=GARMIN_RETENTION_DAYS)).isoformat()
    try:
        store.update(lambda current: current.with_garmin_sync(activities, days, keep_since))
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
