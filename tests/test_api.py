"""The HTTP surface: the gate, the probes, and logging a session end to end."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from gymlog import settings as settings_module
from gymlog import store
from gymlog.api.main import create_app
from gymlog.model import Log

from .factories import block, exercise


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
