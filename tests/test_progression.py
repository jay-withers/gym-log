"""The progression rule.

This is the only real logic in the application, it is pure, and it is the thing a
wrong answer is most visible in — a suggestion that creeps up too fast is an
injury and one that never moves is the spreadsheet again. Tested from literals.
"""

from __future__ import annotations

from gymlog.progression import suggest

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


def test_first_outing_uses_the_seed_weight_and_says_where_it_came_from():
    got = suggest(exercise(seed_weight=7.5), None)
    assert got is not None
    assert got.weight == 7.5
    assert got.reps == 10
    assert "spreadsheet" in got.reason
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
