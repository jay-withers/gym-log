"""The progression rule.

This is the only real logic in the application, it is pure, and it is the thing a
wrong answer is most visible in — a suggestion that creeps up too fast is an
injury and one that never moves is the spreadsheet again. Tested from literals.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from health.model import Exercise
from health.progression import suggest

from .factories import entry, exercise


def test_tops_the_range_on_every_set_adds_load():
    last = entry("Cable Flyes", "chest", (12, 7.5), (12, 7.5), (12, 7.5))
    got = suggest(exercise(increment=2.5), last)
    assert got is not None
    assert got.weight == 10.0
    # Back to the bottom of the range, which is what makes it double progression
    # rather than a permanent ratchet.
    assert got.reps == 10
    assert not got.stalled


def test_one_short_set_is_enough_to_hold_the_weight():
    """The weakest set decides, not the average.

    A strong first set hiding two that fell short is exactly the self-deception
    the single overwritten cell in the spreadsheet allowed.
    """
    got = suggest(exercise(), entry("Cable Flyes", "chest", (12, 7.5), (12, 7.5), (11, 7.5)))
    assert got is not None
    assert got.weight == 7.5
    assert got.reps == 12


def test_below_the_bottom_of_the_range_stalls_and_says_so():
    got = suggest(exercise(), entry("Cable Flyes", "chest", (10, 55.0), (10, 55.0), (9, 55.0)))
    assert got is not None
    assert got.weight == 55.0
    assert got.reps == 10
    assert got.stalled
    assert "9" in got.reason


def test_mid_range_asks_for_one_more_rep():
    last = entry("x", "legs", (9, 24.0), (9, 24.0), (8, 24.0))
    got = suggest(exercise(rep_low=8, rep_high=10), last)
    assert got is not None
    assert got.weight == 24.0
    assert got.reps == 9


def test_one_more_rep_never_exceeds_the_top_of_the_range():
    """A set at the top with a weaker one below must not target rep_high + 1."""
    got = suggest(exercise(rep_low=10, rep_high=12), entry("x", "chest", (12, 20.0), (11, 20.0)))
    assert got is not None
    assert got.reps == 12


def test_heaviest_set_is_the_working_weight():
    """A drop set or a mis-keyed lighter set must not lower the suggestion."""
    got = suggest(exercise(), entry("x", "chest", (12, 20.0), (12, 20.0), (12, 15.0)))
    assert got is not None
    assert got.weight == 22.5


def test_first_outing_uses_the_seed_weight_without_dressing_it_up():
    """The seed weight is offered as a starting point, not as a past session.

    It carries no date, so `last` stays None and the reason says only that
    nothing has been logged — no claim about where the number came from.
    """
    got = suggest(exercise(seed_weight=7.5), None)
    assert got is not None
    assert got.weight == 7.5
    assert got.reps == 10
    assert got.reason == "nothing logged yet"
    assert got.last is None
    # Nothing is claimed to have been performed, because nothing was.
    assert got.last is None
    assert got.last_date == ""


def test_first_outing_with_no_seed_weight_asks_rather_than_guesses():
    got = suggest(exercise(seed_weight=None), None)
    assert got is not None
    assert got.weight == 0.0


def test_untracked_movement_gets_no_suggestion():
    """The finisher takes no load. Three weighted sets of a sled push is noise."""
    finisher = exercise(slot="finisher", name="Sled Push", sets=0, rep_low=0, rep_high=0)
    assert suggest(finisher) is None


def test_an_entry_with_no_sets_is_treated_as_no_history():
    got = suggest(exercise(seed_weight=5.0), entry("x", "chest"))
    assert got is not None
    assert got.weight == 5.0


def test_increment_is_per_exercise():
    """A cable stack and a dumbbell rack do not share a step."""
    got = suggest(exercise(increment=5.0), entry("x", "back", (12, 55.0), (12, 55.0), (12, 55.0)))
    assert got is not None
    assert got.weight == 60.0


def test_weight_label_does_not_show_a_trailing_zero():
    got = suggest(exercise(increment=2.5), entry("x", "chest", (12, 52.5), (12, 52.5), (12, 52.5)))
    assert got is not None
    assert got.weight_label == "55"


# --- properties -----------------------------------------------------------------
#
# The examples above pin the rule's three branches. These hold for any range,
# any increment and any session: the guarantees a lifter relies on without
# reading the code.


# Whatever the form accepts, to the gram, up to a tonne.
GRAMS = st.integers(min_value=0, max_value=1_000_000).map(lambda grams: grams / 1000)
WEIGHTS = GRAMS


@st.composite
def exercises(draw: st.DrawFn) -> Exercise:
    """A tracked movement with a valid range, as the block form guarantees."""
    low = draw(st.integers(min_value=1, max_value=30))
    return exercise(
        rep_low=low,
        rep_high=draw(st.integers(min_value=low, max_value=40)),
        # Typed freely into the block form: 0.1 is as possible as 2.5.
        increment=draw(GRAMS.filter(lambda kg: 0 < kg <= 20)),
    )


SETS = st.lists(st.tuples(st.integers(min_value=0, max_value=50), WEIGHTS), min_size=1, max_size=6)


@given(exercises(), SETS)
def test_reps_always_land_inside_the_range(ex: Exercise, sets: list[tuple[int, float]]):
    got = suggest(ex, entry("Cable Flyes", "chest", *sets))
    assert got is not None
    assert ex.rep_low <= got.reps <= ex.rep_high


@given(exercises(), SETS)
def test_load_only_ever_holds_or_goes_up_by_one_increment(
    ex: Exercise, sets: list[tuple[int, float]]
):
    """Never a drop, and never a jump bigger than the movement's own step."""
    heaviest = max(w for _, w in sets)
    got = suggest(ex, entry("Cable Flyes", "chest", *sets))
    assert got is not None
    assert got.weight in (heaviest, round(heaviest + ex.increment, 3))


@given(exercises(), SETS)
def test_load_goes_up_exactly_when_every_set_tops_the_range(
    ex: Exercise, sets: list[tuple[int, float]]
):
    weakest = min(r for r, _ in sets)
    got = suggest(ex, entry("Cable Flyes", "chest", *sets))
    assert got is not None
    assert (got.weight > max(w for _, w in sets)) == (weakest >= ex.rep_high)
    assert got.stalled == (weakest < ex.rep_low)


@given(exercises(), SETS, st.data())
def test_a_better_session_never_earns_a_worse_suggestion(
    ex: Exercise, sets: list[tuple[int, float]], data: st.DataObject
):
    """Same weights, at least as many reps on every set: at least as far along."""
    better = [(r + data.draw(st.integers(min_value=0, max_value=10)), w) for r, w in sets]
    worse_got = suggest(ex, entry("Cable Flyes", "chest", *sets))
    better_got = suggest(ex, entry("Cable Flyes", "chest", *better))
    assert worse_got is not None and better_got is not None
    assert better_got.weight >= worse_got.weight
    if better_got.weight == worse_got.weight:
        assert better_got.reps >= worse_got.reps


@given(exercises(), SETS)
def test_doing_what_it_says_always_leads_to_more_load(ex: Exercise, sets: list[tuple[int, float]]):
    """From any session, following every suggestion exactly reaches the next
    increment: never a loop, wherever it starts (stalled, mid-range, mixed sets).

    It takes at most one session per rep in the range, plus the one that
    starts it, which is the promise of double progression: climb the reps,
    then the weight.
    """
    start = max(w for _, w in sets)
    got = suggest(ex, entry("Cable Flyes", "chest", *sets))
    for _ in range(ex.rep_high - ex.rep_low + 2):
        assert got is not None
        if got.weight > start:
            assert got.weight == round(start + ex.increment, 3)
            return
        got = suggest(ex, entry("Cable Flyes", "chest", *[(got.reps, got.weight)] * 3))
    raise AssertionError(f"still at {start}kg after following every suggestion")


@given(exercises(), SETS)
def test_the_label_reads_back_as_the_weight(ex: Exercise, sets: list[tuple[int, float]]):
    """The screen shows `weight_label`; what it shows must be the number used,
    after an increment as much as on a held weight."""
    got = suggest(ex, entry("Cable Flyes", "chest", *sets))
    assert got is not None
    assert float(got.weight_label) == got.weight


# Found by review: 7.6 + 0.1 is 7.699999999999999 in floats, shown as "7.7".
def test_an_increment_lands_on_the_number_it_shows():
    got = suggest(exercise(increment=0.1), entry("Cable Flyes", "chest", (12, 7.6), (12, 7.6)))
    assert got is not None
    assert got.weight == 7.7
    assert got.weight_label == "7.7"


def test_a_heavy_weight_keeps_its_grams():
    """`:g` alone would show 1002.125 as 1002.12."""
    got = suggest(exercise(seed_weight=1002.125))
    assert got is not None
    assert got.weight_label == "1002.125"
