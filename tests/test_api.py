"""The HTTP surface: the gate, the probes, and logging a session end to end."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from gymlog import settings as settings_module
from gymlog import store
from gymlog.api.main import create_app
from gymlog.model import Block, Day, Log, SetLog

from .factories import block, entry, exercise, garmin_activity, garmin_day, insight, session


@pytest.fixture
def client():
    # https, not the TestClient default of http://testserver: the session cookie
    # is set `Secure`, and an http client will accept it and then never send it
    # back — every gated request would 303 and the cause would look like a bug in
    # the gate. Browsers special-case localhost as a trustworthy origin, so a
    # local `make run` over http works; httpx does not.
    #
    # follow_redirects off: a redirect is the assertion in most of these.
    #
    # Entered as a context manager, not merely constructed: starlette runs the
    # lifespan only for a client that is entered, so a plain `return` here left
    # everything `lifespan` does — resolving the passcode at start-up — with no
    # coverage at all while the suite stayed green.
    with TestClient(create_app(), base_url="https://testserver", follow_redirects=False) as entered:
        yield entered


@pytest.fixture
def seeded():
    store.save(
        Log(
            blocks=(
                block(
                    "2026-09-01",
                    exercise(slot="chest", name="Cable Flyes", seed_weight=7.5),
                    exercise(slot="finisher", name="Sled Push", sets=0, rep_low=0, rep_high=0),
                ),
            )
        )
    )


def login(client: TestClient) -> None:
    response = client.post("/login", data={"passcode": "test-passcode"})
    assert response.status_code == 303


def test_healthz_does_not_touch_storage(client, monkeypatch):
    """Liveness must not depend on the blob.

    If it did, a storage blip would restart every replica and turn a brief
    outage into a crash loop.
    """
    monkeypatch.setattr(store, "load", lambda: (_ for _ in ()).throw(RuntimeError("blob down")))
    assert client.get("/healthz").status_code == 200


def test_readyz_does_touch_storage(client, monkeypatch):
    monkeypatch.setattr(store, "load", lambda: (_ for _ in ()).throw(RuntimeError("blob down")))
    assert client.get("/readyz").status_code == 503


def test_the_gate_is_closed_by_default(client):
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_a_wrong_passcode_is_refused(client):
    assert client.post("/login", data={"passcode": "wrong"}).status_code == 401


@pytest.mark.parametrize("passcode", ["bench-100\u00a3", "\u2696\ufe0f-day"])
def test_a_passcode_outside_ascii_is_compared_rather_than_crashing(monkeypatch, passcode):
    """A `£` in the passcode used to 500 every login, right and wrong alike.

    `hmac.compare_digest` refuses `str` operands holding any non-ASCII
    character, and the passcode is whatever was typed into Key Vault. Nothing
    validates it on the way in, so the comparison has to cope.
    """
    monkeypatch.setenv("APP_PASSCODE", passcode)
    settings_module.secret.cache_clear()
    settings_module.settings.cache_clear()

    with TestClient(create_app(), base_url="https://testserver", follow_redirects=False) as client:
        assert client.post("/login", data={"passcode": "wrong"}).status_code == 401
        assert client.post("/login", data={"passcode": passcode}).status_code == 303


def test_a_missing_passcode_fails_at_start_up_not_at_first_login(monkeypatch):
    """The point of resolving the passcode in `lifespan`.

    A replica that cannot read its passcode is of no use to anyone, so it should
    fail to start — visibly, in the revision's logs — rather than start, pass its
    liveness probe and refuse the one person who tries to log in.
    """
    monkeypatch.delenv("APP_PASSCODE", raising=False)
    settings_module.secret.cache_clear()
    settings_module.settings.cache_clear()

    with pytest.raises(RuntimeError, match="APP-PASSCODE"):  # noqa: SIM117
        with TestClient(create_app(), base_url="https://testserver"):
            pass


def test_the_right_passcode_opens_it(client, seeded):
    login(client)
    assert client.get("/").status_code == 200


def test_the_login_page_itself_is_not_gated(client):
    assert client.get("/login").status_code == 200


def test_the_manifest_is_not_gated(client):
    """The phone fetches it before any cookie exists, or the install is not offered."""
    response = client.get("/manifest.json")
    assert response.status_code == 200
    assert response.json()["display"] == "standalone"


def test_the_manifest_starts_at_strength_not_the_home_menu(client):
    """The phone icon exists for the twice-a-week fast path of logging a set."""
    assert client.get("/manifest.json").json()["start_url"] == "/strength"


def test_home_links_to_every_section(client, seeded):
    login(client)
    page = client.get("/").text
    for href in ("/strength", "/achievements", "/goals", "/conditions", "/insights", "/garmin"):
        assert f'href="{href}"' in page


def test_logging_a_session_records_only_what_was_filled_in(client, seeded):
    """Two saves, one session.

    Each exercise posts on its own, and they accumulate into the one session
    for the day rather than one session each.
    """
    login(client)
    response = client.post(
        "/session/A/0",
        data={
            "date": "2026-09-15",
            "reps_0_0": "12",
            "weight_0_0": "7.5",
            "reps_0_1": "12",
            "weight_0_1": "7.5",
            # Third set left blank: stopping at two of three is ordinary, and it
            # must not be recorded as a set of zero.
            "reps_0_2": "",
            "weight_0_2": "",
        },
    )
    assert response.status_code == 303
    client.post("/session/A/1", data={"date": "2026-09-15", "done_1": "1"})

    log, _ = store.load()
    assert len(log.sessions) == 1
    entries = log.sessions[0].entries
    assert len(entries[0].sets) == 2
    assert entries[1].exercise == "Sled Push"
    assert entries[1].sets == ()


def test_an_exercise_with_nothing_in_it_is_not_recorded(client, seeded):
    """Recording it would put a zero-rep entry in the history and drag every
    subsequent suggestion down.

    It must also not start a session: a save that recorded nothing would
    otherwise leave an empty session in the log for the day.
    """
    login(client)
    response = client.post("/session/A/0", data={"date": "2026-09-15"})
    assert response.status_code == 303
    assert "empty=0" in response.headers["location"]
    assert store.load()[0].sessions == ()


def test_the_suggestion_moves_after_a_session(client, seeded):
    login(client)
    client.post(
        "/session/A/0",
        data={
            "date": "2026-09-15",
            "reps_0_0": "12",
            "weight_0_0": "7.5",
            "reps_0_1": "12",
            "weight_0_1": "7.5",
            "reps_0_2": "12",
            "weight_0_2": "7.5",
        },
    )
    # Every set at the top of 10-12, so the weight goes up and the reps drop
    # back to the bottom. Asserted on the placeholders because that is now the
    # only place the suggestion is rendered — the card carries no prose.
    card = _card(client.get("/session/A").text, "Cable Flyes")
    assert 'placeholder="10 kg"' in card
    assert 'placeholder="10 reps"' in card


def test_rotating_keeps_the_slot_and_the_history(client, seeded):
    """The single thing the spreadsheet could not do."""
    login(client)
    client.post(
        "/session/A/0",
        data={"date": "2026-09-15", "reps_0_0": "12", "weight_0_0": "7.5"},
    )
    assert (
        client.post(
            "/block",
            data={"name": "Block 4", "started": "2026-10-27", "name_A_0": "Incline DB Press"},
        ).status_code
        == 303
    )

    log, _ = store.load()
    assert log.current_block is not None
    assert log.current_block.days["A"].exercises[0].name == "Incline DB Press"
    # The prescription carried over; the seed weight deliberately did not.
    assert log.current_block.days["A"].exercises[0].rep_high == 12
    assert log.current_block.days["A"].exercises[0].seed_weight is None
    # And the old session is still there, under the same slot.
    assert [r[1] for r in log.history("chest")] == ["Cable Flyes"]


def test_the_block_form_offers_the_rep_range_of_every_tracked_movement(client, seeded):
    """The prescription is edited here or nowhere — there is no other screen for it."""
    login(client)
    page = client.get("/block").text
    assert 'name="rep_low_A_0" value="10"' in page
    assert 'name="rep_high_A_0" value="12"' in page
    # The finisher takes no load, so it is offered no range to type into.
    assert "rep_low_A_1" not in page


def test_rotating_takes_the_rep_range_from_the_form(client):
    store.save(Log(blocks=(block("2026-09-01", exercise(rep_targets=(10, 12, 12))),)))
    login(client)
    assert (
        client.post(
            "/block",
            data={
                "name": "Block 4",
                "started": "2026-10-27",
                # Only the top of the range retyped, and the pair transposed, to
                # cover both things the form is forgiving about.
                "rep_high_A_0": "8",
            },
        ).status_code
        == 303
    )

    log, _ = store.load()
    assert log.current_block is not None
    chest = log.current_block.days["A"].exercises[0]
    assert (chest.rep_low, chest.rep_high) == (8, 10)
    # Per-set targets belonged to the old range and did not follow it.
    assert chest.rep_targets == ()


def test_a_write_that_loses_its_race_is_reported_not_swallowed(client, seeded, monkeypatch):
    """The person is the only one who can re-enter a session, so they must be told."""

    def conflict(_change):
        raise store.ConflictError("changed underneath")

    monkeypatch.setattr(store, "update", conflict)
    login(client)
    response = client.post(
        "/session/A/0", data={"date": "2026-09-15", "reps_0_0": "12", "weight_0_0": "7.5"}
    )
    assert response.status_code == 409


# --- the finisher ------------------------------------------------------------


def test_a_finisher_time_is_recorded(client, seeded):
    """Sled Push takes no load, but a time gets entered when it is remembered."""
    login(client)
    client.post("/session/A/1", data={"date": "2026-09-15", "seconds_1": "1:30"})

    log, _etag = store.load()
    finisher = log.sessions[-1].entries[-1]
    assert finisher.exercise == "Sled Push"
    assert finisher.sets == ()
    assert finisher.seconds == 90
    assert finisher.time_label == "1:30"


def test_a_finisher_time_alone_counts_as_having_done_it(client, seeded):
    """No tick box, just a time. Typing one is the stronger statement of the two."""
    login(client)
    response = client.post("/session/A/1", data={"date": "2026-09-15", "seconds_1": "45"})
    assert response.status_code == 303

    log, _etag = store.load()
    assert log.sessions[-1].entries[-1].seconds == 45


def test_a_ticked_finisher_with_no_time_is_still_recorded(client, seeded):
    """The time is optional. Ticking it off without one must keep working."""
    login(client)
    client.post("/session/A/1", data={"date": "2026-09-15", "done_1": "1"})

    log, _etag = store.load()
    finisher = log.sessions[-1].entries[-1]
    assert finisher.note == "done"
    assert finisher.seconds == 0
    assert finisher.time_label == ""


def test_an_unticked_untimed_finisher_is_not_recorded(client, seeded):
    """Neither field filled in is the same as not having done it."""
    login(client)
    response = client.post("/session/A/1", data={"date": "2026-09-15"})
    assert response.status_code == 303
    # `empty=<index>`: the card that had nothing in it, so the page can scroll
    # back to it rather than to the top.
    assert "empty=1" in response.headers["location"]

    log, _ = store.load()
    assert log.sessions == ()


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("90", 90),
        ("1:30", 90),
        ("2m", 120),
        ("1m30", 90),
        ("0:45", 45),
        ("", 0),
        # Unparseable is untimed, not a refused session.
        ("ages", 0),
        # Half-parseable too: `1:` reaches the int() and must not 500 there.
        ("1:ish", 0),
        ("m", 0),
    ],
)
def test_the_time_field_takes_what_a_phone_keyboard_makes_easy(typed, expected):
    from gymlog.api.routes import _seconds

    assert _seconds(typed) == expected


# --- the session card --------------------------------------------------------


def _card(page: str, name: str) -> str:
    """The markup for one exercise's card, which is now its own form."""
    for card in page.split('<form method="post"')[1:]:
        if f">{name}<" in card:
            return card
    raise AssertionError(f"no card for {name}")


def test_the_card_is_name_target_and_inputs_only(client, seeded):
    """No suggestion prose above the boxes.

    The suggestion still drives both placeholders, so what to reach for is in
    the field being typed into rather than on a line restating it.
    """
    login(client)
    card = _card(client.get("/session/A").text, "Cable Flyes")

    assert 'class="target' in card
    assert 'class="reason' not in card
    assert "nothing logged yet" not in card
    # Nor the previous session's line.
    assert 'class="last"' not in card


def test_the_target_line_shows_the_range(client, seeded):
    login(client)
    card = _card(client.get("/session/A").text, "Cable Flyes")
    assert "Target" in card
    assert "10\u201312 reps" in card


def test_falling_short_still_shows_on_the_stripped_card(client, seeded):
    """Losing the reason line must not lose the one warning that mattered.

    A session below the bottom of the range holds the weight rather than adding
    to it, and the card has to say so or it silently repeats the same number.
    """
    login(client)
    client.post(
        "/session/A/0",
        data={
            "date": "2026-09-15",
            "reps_0_0": "6",
            "weight_0_0": "7.5",
            "reps_0_1": "6",
            "weight_0_1": "7.5",
            "reps_0_2": "6",
            "weight_0_2": "7.5",
        },
    )
    assert 'class="target stalled"' in _card(client.get("/session/A").text, "Cable Flyes")


# --- the draft held on the phone ---------------------------------------------


def test_the_form_reports_that_nothing_is_logged_yet(client, seeded):
    """The flag the draft script reads to decide whether its draft has landed."""
    login(client)
    assert 'data-logged="0"' in client.get("/session/A").text


def test_the_form_reports_a_session_already_logged_today(client, seeded):
    login(client)
    client.post(
        "/session/A/0",
        data={"date": date.today().isoformat(), "reps_0_0": "10", "weight_0_0": "7.5"},
    )
    assert 'data-logged="1"' in client.get("/session/A").text


def test_a_session_logged_on_another_date_does_not_clear_today_s_draft(client, seeded):
    """Only today's session for this day means the draft has landed.

    Keyed on both, because a draft typed today must survive last week's session
    being in the log — which it always is by the second week of a block.
    """
    login(client)
    client.post(
        "/session/A/0",
        data={"date": "2026-01-02", "reps_0_0": "10", "weight_0_0": "7.5"},
    )
    assert 'data-logged="0"' in client.get("/session/A").text


def test_a_conflict_leaves_the_form_reporting_nothing_logged(client, seeded, monkeypatch):
    """A save that lost its race must not look like a save that landed.

    The draft is dropped on the server saying the session is recorded, not on
    the form being submitted, so a conflict keeps what was typed.
    """

    def _boom(_mutate):
        raise store.ConflictError("changed elsewhere")

    monkeypatch.setattr(store, "update", _boom)
    login(client)
    response = client.post(
        "/session/A/0",
        data={"date": date.today().isoformat(), "reps_0_0": "10", "weight_0_0": "7.5"},
    )
    assert response.status_code == 409

    monkeypatch.undo()
    assert 'data-logged="0"' in client.get("/session/A").text


# --- saving one exercise at a time -------------------------------------------


def test_six_saves_make_one_session(client, seeded):
    """The unit is the training day, not the submit."""
    login(client)
    when = date.today().isoformat()
    client.post("/session/A/0", data={"date": when, "reps_0_0": "10", "weight_0_0": "7.5"})
    client.post("/session/A/1", data={"date": when, "done_1": "1"})

    log, _ = store.load()
    assert len(log.sessions) == 1
    assert log.sessions[0].day == "A"
    assert [e.exercise for e in log.sessions[0].entries] == ["Cable Flyes", "Sled Push"]


def test_saving_the_same_exercise_again_replaces_it(client, seeded):
    """An 11 typed where a 1 was meant is noticed on the next set."""
    login(client)
    when = date.today().isoformat()
    client.post("/session/A/0", data={"date": when, "reps_0_0": "110", "weight_0_0": "7.5"})
    client.post("/session/A/0", data={"date": when, "reps_0_0": "11", "weight_0_0": "7.5"})

    log, _ = store.load()
    assert len(log.sessions) == 1
    entries = log.sessions[0].entries
    assert len(entries) == 1
    assert [s.reps for s in entries[0].sets] == [11]


def test_re_saving_keeps_the_order_exercises_were_performed_in(client, seeded):
    """A correction must not shuffle the log out of the order it happened."""
    login(client)
    when = date.today().isoformat()
    client.post("/session/A/0", data={"date": when, "reps_0_0": "10", "weight_0_0": "7.5"})
    client.post("/session/A/1", data={"date": when, "done_1": "1"})
    client.post("/session/A/0", data={"date": when, "reps_0_0": "12", "weight_0_0": "7.5"})

    log, _ = store.load()
    assert [e.exercise for e in log.sessions[0].entries] == ["Cable Flyes", "Sled Push"]


def test_a_saved_exercise_comes_back_as_values_not_placeholders(client, seeded):
    """So that re-saving it is an edit rather than a fresh guess."""
    login(client)
    when = date.today().isoformat()
    client.post("/session/A/0", data={"date": when, "reps_0_0": "11", "weight_0_0": "8"})

    card = _card(client.get("/session/A").text, "Cable Flyes")
    assert 'value="11"' in card
    assert 'value="8"' in card
    assert ">Update<" in card


def test_a_suggestion_does_not_chase_the_set_just_typed_into_it(client, seeded):
    """Today's own entries are excluded from what the suggestion is built on.

    Without that, saving 12 at the top of the range would immediately re-suggest
    against it, and the card would climb itself every time it was saved.
    """
    login(client)
    when = date.today().isoformat()
    for _ in range(3):
        client.post(
            "/session/A/0",
            data={
                "date": when,
                "reps_0_0": "12",
                "weight_0_0": "7.5",
                "reps_0_1": "12",
                "weight_0_1": "7.5",
                "reps_0_2": "12",
                "weight_0_2": "7.5",
            },
        )

    # Still the first outing's seed weight: nothing before today to go on.
    card = _card(client.get("/session/A").text, "Cable Flyes")
    assert 'placeholder="7.5 kg"' in card


def test_a_conflict_on_one_exercise_names_it(client, seeded, monkeypatch):
    def _boom(_mutate):
        raise store.ConflictError("changed elsewhere")

    monkeypatch.setattr(store, "update", _boom)
    login(client)
    response = client.post(
        "/session/A/0",
        data={"date": date.today().isoformat(), "reps_0_0": "10", "weight_0_0": "7.5"},
    )
    assert response.status_code == 409
    assert "Cable Flyes" in response.json()["detail"]


def test_an_index_outside_the_day_goes_back_rather_than_erroring(client, seeded):
    login(client)
    response = client.post("/session/A/99", data={"date": date.today().isoformat()})
    assert response.status_code == 303
    assert response.headers["location"] == "/session/A"


# --- the pages before there is anything to show ------------------------------


def test_every_page_says_how_to_get_started_before_the_first_import(client):
    """A fresh deployment has no block at all, and must not 500 on the way to saying so."""
    login(client)
    for path in ("/strength", "/block"):
        response = client.get(path)
        assert response.status_code == 200
        assert "gymlog import" in response.text


def test_rotating_with_no_block_to_rotate_goes_home(client):
    """There is nothing to carry forward, so there is nothing to do."""
    login(client)
    assert client.post("/block", data={"name": "Block 1"}).status_code == 303


@pytest.mark.parametrize("path", ["/session/Z", "/session/Z/0"])
def test_a_day_that_is_not_in_the_block_goes_back_to_strength(client, seeded, path):
    """A stale bookmark from a previous block, which the phone keeps for months."""
    login(client)
    response = client.get(path) if path.count("/") == 2 else client.post(path, data={})
    assert response.status_code == 303
    assert response.headers["location"] == "/strength"


def test_an_exercise_index_past_the_end_goes_back_to_the_session(client, seeded):
    """Same stale bookmark, one level down: the block rotated and the day got shorter."""
    login(client)
    response = client.post("/session/A/99", data={"date": "2026-09-15"})
    assert response.status_code == 303
    assert response.headers["location"] == "/session/A"


def test_a_rotation_that_loses_its_race_is_reported(client, seeded, monkeypatch):
    """Silently dropping it would leave the phone showing a block that was never started."""

    def conflict(_change):
        raise store.ConflictError("changed underneath")

    monkeypatch.setattr(store, "update", conflict)
    login(client)
    assert client.post("/block", data={"name": "Block 4"}).status_code == 409


# --- loose days and manually-added exercises ----------------------------------


def test_a_loose_day_renders_with_no_prescribed_cards(client, seeded):
    """The manual day carries no block prescription at all."""
    login(client)
    response = client.get("/session/manual")
    assert response.status_code == 200
    page = response.text
    assert "Add an exercise" in page
    assert "Save</button>" not in page  # no prescribed-card "Save"/"Update" buttons


def test_the_strength_page_always_links_to_a_loose_day(client, seeded):
    login(client)
    assert 'href="/session/manual"' in client.get("/strength").text


def test_logging_a_manual_exercise_on_a_loose_day(client, seeded):
    login(client)
    response = client.post(
        "/session/manual/extra",
        data={
            "date": date.today().isoformat(),
            "name": "Ab Wheel Rollout",
            "slot": "core",
            "reps_0": "10",
            "weight_0": "0",
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/session/manual"

    log, _ = store.load()
    assert len(log.sessions) == 1
    assert log.sessions[0].day == "manual"
    entry = log.sessions[0].entries[0]
    assert entry.exercise == "Ab Wheel Rollout"
    assert entry.slot == "core"
    assert entry.sets == (SetLog(reps=10, weight=0.0),)
    # A slot typed in by hand is tracked for History by slot too.
    assert "core" in log.slots

    page = client.get("/session/manual").text
    assert "Ab Wheel Rollout" in page


def test_logging_a_manual_exercise_alongside_prescribed_ones(client, seeded):
    """An improvised extra on a block day, not one of the prescribed cards."""
    login(client)
    client.post(
        "/session/A/extra",
        data={
            "date": date.today().isoformat(),
            "name": "Face Pulls",
            "slot": "shoulders",
            "reps_0": "15",
        },
    )
    # No weight filled in: a bodyweight/band exercise, reps alone still counts.
    page = client.get("/session/A").text
    assert "Face Pulls" in page


def test_a_manual_exercise_with_no_name_or_slot_is_not_recorded(client, seeded):
    login(client)
    client.post("/session/manual/extra", data={"name": "", "slot": "core", "reps_0": "10"})
    client.post("/session/manual/extra", data={"name": "Plank", "slot": "", "reps_0": "10"})
    assert store.load()[0].sessions == ()


def test_a_manual_exercise_with_no_sets_is_not_recorded(client, seeded):
    login(client)
    client.post("/session/manual/extra", data={"name": "Plank", "slot": "core"})
    assert store.load()[0].sessions == ()


def test_the_add_exercise_form_suggests_known_names(client, seeded):
    """Catches "Bicep Curl" vs "Bicep Curls" before it becomes two histories."""
    login(client)
    client.post(
        "/session/manual/extra",
        data={
            "date": date.today().isoformat(),
            "name": "Ab Wheel Rollout",
            "slot": "core",
            "reps_0": "10",
        },
    )
    page = client.get("/session/manual").text
    # The block's prescribed exercises are offered too, not just past extras.
    assert '<option value="Ab Wheel Rollout">' in page
    assert '<option value="Cable Flyes">' in page


def test_extra_exercise_on_an_unknown_day_goes_back_to_strength(client, seeded):
    login(client)
    response = client.post(
        "/session/Z/extra", data={"name": "Plank", "slot": "core", "reps_0": "10"}
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/strength"


# --- history -----------------------------------------------------------------


def test_history_spans_the_rotation_and_marks_the_best_set(client, seeded):
    """The page that only exists because the log is keyed on the slot."""
    store.save(
        Log(
            blocks=(block("2026-09-01", exercise(slot="chest", name="Cable Flyes")),),
            sessions=(
                session("2026-09-15", "2026-09-01", "A", entry("Cable Flyes", "chest", (12, 7.5))),
                session(
                    "2026-10-28", "2026-10-27", "A", entry("Incline DB Press", "chest", (11, 20.0))
                ),
            ),
        )
    )
    login(client)
    page = client.get("/history/chest").text

    assert "Cable Flyes" in page
    assert "Incline DB Press" in page
    # The heaviest set across every block, marked once.
    assert page.count("▲") == 1


def test_a_slot_with_nothing_in_it_says_so(client, seeded):
    login(client)
    assert "Nothing logged for this slot yet" in client.get("/history/legs").text


# --- which day is next -------------------------------------------------------


def test_the_next_day_alternates(client, seeded):
    """Twice a week means the day not done last, which is the whole rule."""
    store.save(
        Log(
            blocks=(
                Block(
                    id="2026-09-01",
                    name="Test block",
                    started="2026-09-01",
                    days={
                        "A": Day(label="Tues", exercises=(exercise(),)),
                        "B": Day(label="Thur", exercises=(exercise(),)),
                    },
                ),
            ),
            sessions=(session("2026-09-15", "2026-09-01", "A", entry()),),
        )
    )
    login(client)
    page = client.get("/strength").text
    assert "Thur · next" in page
    assert "Tues · next" not in page


def test_the_first_session_of_a_block_starts_at_the_first_day(client, seeded):
    login(client)
    assert "Tues · next" in client.get("/strength").text


def test_a_block_with_no_days_still_renders(client):
    """A hand-edited document in the portal can produce one, and a 500 would hide why."""
    store.save(Log(blocks=(Block(id="2026-09-01", name="Empty", started="2026-09-01", days={}),)))
    login(client)
    assert client.get("/strength").status_code == 200


# --- the gate ----------------------------------------------------------------


def test_readiness_passes_when_the_log_can_be_read(client):
    """The probe Container Apps uses to decide whether to send traffic."""
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "storage": True}


def test_rotating_the_passcode_logs_the_phone_out(client, seeded, monkeypatch):
    """The useful property of deriving the cookie key from the passcode.

    There is no session store to revoke against, so this is the whole of "log
    every device out".
    """
    login(client)
    assert client.get("/").status_code == 200

    monkeypatch.setenv("APP_PASSCODE", "a-new-passcode")
    settings_module.secret.cache_clear()

    assert client.get("/").status_code == 303


def test_a_cookie_secret_decouples_the_two(client, seeded, monkeypatch):
    """Which is why it exists: rotating the passcode then leaves sessions alone."""
    monkeypatch.setenv("COOKIE_SECRET", "a-signing-key")
    settings_module.settings.cache_clear()
    login(client)

    monkeypatch.setenv("APP_PASSCODE", "a-new-passcode")
    settings_module.secret.cache_clear()

    assert client.get("/").status_code == 200


@pytest.mark.parametrize(
    "cookie",
    [
        # Signed with something else, or edited by hand.
        "1758240000.0000000000000000000000000000000000000000000000000000000000000000",
        # Not a timestamp at all, which must be refused before it is compared.
        "yesterday.abc",
        # Not the shape at all.
        "nonsense",
    ],
)
def test_a_cookie_this_process_did_not_issue_is_refused(client, seeded, cookie):
    login(client)
    client.cookies.set("gymlog_session", cookie)
    assert client.get("/").status_code == 303


def test_a_session_older_than_the_cookie_lifetime_is_refused(client, seeded, monkeypatch):
    """Thirty days by default; this is the check that eventually ends one."""
    login(client)
    monkeypatch.setenv("COOKIE_MAX_AGE_SECONDS", "0")
    settings_module.settings.cache_clear()

    assert client.get("/").status_code == 303


def test_the_gate_can_be_turned_off(monkeypatch, tmp_path):
    """For a local `make run`, and only for that: it defaults on and fails closed."""
    monkeypatch.setenv("REQUIRE_PASSCODE", "false")
    monkeypatch.delenv("APP_PASSCODE", raising=False)
    settings_module.settings.cache_clear()
    settings_module.secret.cache_clear()

    # No passcode configured at all, so a start-up that still resolved one would
    # raise here rather than serve.
    with TestClient(create_app(), base_url="https://testserver", follow_redirects=False) as open_:
        assert open_.get("/").status_code == 200


# --- achievements --------------------------------------------------------------


def test_logging_an_achievement(client, seeded):
    login(client)
    response = client.post(
        "/achievements", data={"date": "2026-09-15", "title": "Ran a 5k", "note": "PB"}
    )
    assert response.status_code == 303

    log, _ = store.load()
    assert len(log.achievements) == 1
    assert log.achievements[0].title == "Ran a 5k"
    assert log.achievements[0].note == "PB"
    assert "Ran a 5k" in client.get("/achievements").text


def test_an_achievement_with_no_title_is_not_recorded(client, seeded):
    login(client)
    client.post("/achievements", data={"date": "2026-09-15", "title": ""})
    assert store.load()[0].achievements == ()


def test_editing_and_deleting_an_achievement(client, seeded):
    login(client)
    client.post("/achievements", data={"date": "2026-09-15", "title": "Ran a 5k"})
    achievement_id = store.load()[0].achievements[0].id

    response = client.post(
        f"/achievements/{achievement_id}",
        data={"date": "2026-09-16", "title": "Ran a 10k", "note": "further than planned"},
    )
    assert response.status_code == 303
    edited = store.load()[0].achievements[0]
    assert edited.title == "Ran a 10k"
    assert edited.date == "2026-09-16"
    assert edited.note == "further than planned"

    response = client.post(f"/achievements/{achievement_id}/delete")
    assert response.status_code == 303
    assert store.load()[0].achievements == ()


def test_editing_an_achievement_with_no_title_does_nothing(client, seeded):
    login(client)
    client.post("/achievements", data={"date": "2026-09-15", "title": "Ran a 5k"})
    achievement_id = store.load()[0].achievements[0].id

    client.post(f"/achievements/{achievement_id}", data={"title": ""})
    assert store.load()[0].achievements[0].title == "Ran a 5k"


def test_deleting_an_unknown_achievement_is_a_no_op(client, seeded):
    login(client)
    assert client.post("/achievements/does-not-exist/delete").status_code == 303


def test_the_achievements_list_is_read_only(client, seeded):
    """No inline editable fields — editing lives on its own page."""
    login(client)
    client.post("/achievements", data={"date": "2026-09-15", "title": "Ran a 5k"})
    page = client.get("/achievements").text
    assert "<input" not in page
    assert 'href="/achievements/new"' in page
    assert "/edit" in page


def test_the_new_achievement_form_renders(client, seeded):
    login(client)
    assert client.get("/achievements/new").status_code == 200


def test_the_edit_achievement_form_is_prefilled(client, seeded):
    login(client)
    client.post("/achievements", data={"date": "2026-09-15", "title": "Ran a 5k", "note": "PB"})
    achievement_id = store.load()[0].achievements[0].id

    page = client.get(f"/achievements/{achievement_id}/edit").text
    assert 'value="Ran a 5k"' in page
    assert 'value="PB"' in page


def test_the_edit_form_for_an_unknown_achievement_goes_back_to_the_list(client, seeded):
    login(client)
    response = client.get("/achievements/does-not-exist/edit")
    assert response.status_code == 303
    assert response.headers["location"] == "/achievements"


def test_achievements_are_listed_newest_first(client, seeded):
    login(client)
    client.post("/achievements", data={"date": "2026-09-01", "title": "Older"})
    client.post("/achievements", data={"date": "2026-09-20", "title": "Newer"})
    page = client.get("/achievements").text
    assert page.index("Newer") < page.index("Older")


# --- goals -----------------------------------------------------------------


def test_adding_and_achieving_a_goal(client, seeded):
    login(client)
    client.post("/goals", data={"title": "Bench press 100kg"})

    log, _ = store.load()
    assert len(log.goals) == 1
    assert log.goals[0].status == "active"
    goal_id = log.goals[0].id

    page = client.get("/goals").text
    assert "Bench press 100kg" in page

    response = client.post(f"/goals/{goal_id}/achieve")
    assert response.status_code == 303
    assert store.load()[0].goals[0].status == "achieved"


def test_achieving_an_unknown_goal_is_a_no_op(client, seeded):
    login(client)
    assert client.post("/goals/does-not-exist/achieve").status_code == 303
    assert store.load()[0].goals == ()


def test_editing_and_deleting_a_goal(client, seeded):
    login(client)
    client.post("/goals", data={"title": "Bench press 100kg"})
    goal_id = store.load()[0].goals[0].id

    response = client.post(
        f"/goals/{goal_id}",
        data={"title": "Bench press 110kg", "target_date": "2027-01-01", "note": "raised it"},
    )
    assert response.status_code == 303
    edited = store.load()[0].goals[0]
    assert edited.title == "Bench press 110kg"
    assert edited.target_date == "2027-01-01"
    assert edited.note == "raised it"
    # Editing must not disturb the status an achieve/resolve action set.
    assert edited.status == "active"

    response = client.post(f"/goals/{goal_id}/delete")
    assert response.status_code == 303
    assert store.load()[0].goals == ()


def test_editing_a_goal_with_no_title_does_nothing(client, seeded):
    login(client)
    client.post("/goals", data={"title": "Bench press 100kg"})
    goal_id = store.load()[0].goals[0].id

    client.post(f"/goals/{goal_id}", data={"title": ""})
    assert store.load()[0].goals[0].title == "Bench press 100kg"


def test_deleting_an_unknown_goal_is_a_no_op(client, seeded):
    login(client)
    assert client.post("/goals/does-not-exist/delete").status_code == 303


def test_the_goals_list_is_read_only(client, seeded):
    login(client)
    client.post("/goals", data={"title": "Bench press 100kg"})
    page = client.get("/goals").text
    assert "<input" not in page
    assert 'href="/goals/new"' in page
    assert "/edit" in page


def test_the_new_goal_form_renders(client, seeded):
    login(client)
    assert client.get("/goals/new").status_code == 200


def test_the_edit_goal_form_is_prefilled(client, seeded):
    login(client)
    client.post("/goals", data={"title": "Bench press 100kg", "note": "3x8"})
    goal_id = store.load()[0].goals[0].id

    page = client.get(f"/goals/{goal_id}/edit").text
    assert 'value="Bench press 100kg"' in page
    assert 'value="3x8"' in page


def test_the_edit_form_for_an_unknown_goal_goes_back_to_the_list(client, seeded):
    login(client)
    response = client.get("/goals/does-not-exist/edit")
    assert response.status_code == 303
    assert response.headers["location"] == "/goals"


def test_goals_are_listed_by_soonest_target_date_first(client, seeded):
    login(client)
    client.post("/goals", data={"title": "No date"})
    client.post("/goals", data={"title": "Later", "target_date": "2027-06-01"})
    client.post("/goals", data={"title": "Sooner", "target_date": "2027-01-01"})
    page = client.get("/goals").text
    assert page.index("Sooner") < page.index("Later") < page.index("No date")


# --- injuries & conditions -----------------------------------------------------


def test_adding_and_resolving_a_condition(client, seeded):
    login(client)
    client.post(
        "/conditions",
        data={"body_part": "lower back", "started": "2026-09-01", "note": "deadlifts"},
    )

    log, _ = store.load()
    assert len(log.conditions) == 1
    assert log.conditions[0].status == "active"
    condition_id = log.conditions[0].id

    page = client.get("/conditions").text
    assert "lower back" in page

    response = client.post(f"/conditions/{condition_id}/resolve")
    assert response.status_code == 303
    resolved = store.load()[0].conditions[0]
    assert resolved.status == "resolved"
    assert resolved.resolved


def test_a_condition_with_no_body_part_is_not_recorded(client, seeded):
    login(client)
    client.post("/conditions", data={"body_part": ""})
    assert store.load()[0].conditions == ()


def test_editing_and_deleting_a_condition(client, seeded):
    login(client)
    client.post(
        "/conditions", data={"body_part": "lower back", "started": "2026-09-01", "note": "gym"}
    )
    condition_id = store.load()[0].conditions[0].id

    response = client.post(
        f"/conditions/{condition_id}",
        data={"body_part": "upper back", "started": "2026-09-02", "note": "deadlifts"},
    )
    assert response.status_code == 303
    edited = store.load()[0].conditions[0]
    assert edited.body_part == "upper back"
    assert edited.started == "2026-09-02"
    assert edited.note == "deadlifts"
    # Editing must not disturb the status a resolve action set.
    assert edited.status == "active"

    response = client.post(f"/conditions/{condition_id}/delete")
    assert response.status_code == 303
    assert store.load()[0].conditions == ()


def test_editing_a_condition_with_no_body_part_does_nothing(client, seeded):
    login(client)
    client.post("/conditions", data={"body_part": "lower back"})
    condition_id = store.load()[0].conditions[0].id

    client.post(f"/conditions/{condition_id}", data={"body_part": ""})
    assert store.load()[0].conditions[0].body_part == "lower back"


def test_deleting_an_unknown_condition_is_a_no_op(client, seeded):
    login(client)
    assert client.post("/conditions/does-not-exist/delete").status_code == 303


def test_the_conditions_list_is_read_only(client, seeded):
    login(client)
    client.post("/conditions", data={"body_part": "lower back"})
    page = client.get("/conditions").text
    assert "<input" not in page
    assert 'href="/conditions/new"' in page
    assert "/edit" in page


def test_the_new_condition_form_renders(client, seeded):
    login(client)
    assert client.get("/conditions/new").status_code == 200


def test_the_edit_condition_form_is_prefilled(client, seeded):
    login(client)
    client.post("/conditions", data={"body_part": "lower back", "note": "deadlifts"})
    condition_id = store.load()[0].conditions[0].id

    page = client.get(f"/conditions/{condition_id}/edit").text
    assert 'value="lower back"' in page
    assert 'value="deadlifts"' in page


def test_the_edit_form_for_an_unknown_condition_goes_back_to_the_list(client, seeded):
    login(client)
    response = client.get("/conditions/does-not-exist/edit")
    assert response.status_code == 303
    assert response.headers["location"] == "/conditions"


def test_conditions_are_listed_most_recently_started_first(client, seeded):
    login(client)
    client.post("/conditions", data={"body_part": "Older", "started": "2026-08-01"})
    client.post("/conditions", data={"body_part": "Newer", "started": "2026-09-15"})
    page = client.get("/conditions").text
    assert page.index("Newer") < page.index("Older")


# --- Insights: an index, an AI summary, and a heart-rate-zone breakdown -----


def test_insights_index_links_to_both_subsections(client, seeded):
    login(client)
    page = client.get("/insights").text
    assert 'href="/insights/ai"' in page
    assert 'href="/insights/hr-zones"' in page


def test_ai_insights_page_lists_stored_insights(client, seeded):
    store.update(
        lambda current: current.with_insight(
            insight(summary="Good week overall.", generated_at="2026-09-20")
        )
    )
    login(client)
    page = client.get("/insights/ai").text
    assert "Good week overall." in page
    assert "2026-09-20" in page


def test_ai_insights_page_when_empty(client, seeded):
    login(client)
    assert client.get("/insights/ai").status_code == 200


def test_generate_insight_now_calls_deepseek_and_redirects_back(client, seeded, monkeypatch):
    """The button's route, not the scheduled job — mocked the same way test_cli.py mocks it."""
    monkeypatch.setattr(
        "gymlog.insights.generate_insight",
        lambda log: insight(summary="Fresh from the button."),
    )
    login(client)

    response = client.post("/insights/ai", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/insights/ai"
    page = client.get("/insights/ai").text
    assert "Fresh from the button." in page


def test_delete_insight_removes_it_and_redirects_back(client, seeded):
    store.update(lambda current: current.with_insight(insight(id="ins1", summary="Old one.")))
    login(client)

    response = client.post("/insights/ai/ins1/delete", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/insights/ai"
    log, _etag = store.load()
    assert log.insights == ()


def test_hr_zone_insights_page_shows_minutes_and_percentages(client, seeded):
    store.update(
        lambda current: current.with_garmin_sync(
            activities=[
                garmin_activity(date=date.today().isoformat(), zone_seconds=(60, 120, 0, 0, 0))
            ],
            days=[],
            keep_since="2020-01-01",
        )
    )
    login(client)
    page = client.get("/insights/hr-zones").text
    assert "1m · 33%" in page  # zone 1: 60s of 180s tracked
    assert "2m · 67%" in page  # zone 2: 120s of 180s tracked
    assert "--pct: 33" in page  # the bar segment's proportional width
    assert "--pct: 67" in page
    assert "96-113 bpm" in page  # zone 1's range, from the run's own thresholds
    assert "161+ bpm" in page  # zone 5 is open-ended


def test_hr_zone_insights_page_is_empty_before_any_sync(client, seeded):
    login(client)
    page = client.get("/insights/hr-zones").text
    assert "No heart rate zone data yet" in page


# --- Garmin sync: a cut-down browser for the last 30 days of synced data -----


def test_garmin_page_lists_synced_activities_and_days(client, seeded):
    store.update(
        lambda current: current.with_garmin_sync(
            activities=[garmin_activity(id="a1", activity_type="cycling")],
            days=[garmin_day(date="2026-09-15", steps=9000)],
            keep_since="2020-01-01",
        )
    )
    login(client)
    page = client.get("/garmin").text
    assert "cycling" in page
    assert "9000 steps" in page


def test_garmin_page_when_empty(client, seeded):
    login(client)
    assert client.get("/garmin").status_code == 200


def test_sync_garmin_now_calls_garmin_and_redirects_back(client, seeded, monkeypatch):
    synced_activity = garmin_activity(id="a1", activity_type="swimming")
    monkeypatch.setattr("gymlog.garmin.sync_garmin", lambda: ([synced_activity], []))
    login(client)

    response = client.post("/garmin/sync", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/garmin"
    page = client.get("/garmin").text
    assert "swimming" in page
