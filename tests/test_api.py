"""The HTTP surface: the gate, the probes, and logging a session end to end."""

from __future__ import annotations

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
    login(client)
    response = client.post(
        "/session/A",
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
            "done_1": "1",
        },
    )
    assert response.status_code == 303

    log, _ = store.load()
    assert len(log.sessions) == 1
    entries = log.sessions[0].entries
    assert len(entries[0].sets) == 2
    assert entries[1].exercise == "Sled Push"
    assert entries[1].sets == ()


def test_an_entirely_empty_session_is_not_recorded(client, seeded):
    """Recording it would put a zero-rep entry in the history and drag every
    subsequent suggestion down."""
    login(client)
    response = client.post("/session/A", data={"date": "2026-09-15"})
    assert response.status_code == 303
    assert response.headers["location"].endswith("empty=1")
    assert store.load()[0].sessions == ()


def test_the_suggestion_moves_after_a_session(client, seeded):
    login(client)
    client.post(
        "/session/A",
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
    page = client.get("/session/A").text
    assert "10kg" in page


def test_rotating_keeps_the_slot_and_the_history(client, seeded):
    """The single thing the spreadsheet could not do."""
    login(client)
    client.post(
        "/session/A",
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
        "/session/A", data={"date": "2026-09-15", "reps_0_0": "12", "weight_0_0": "7.5"}
    )
    assert response.status_code == 409
