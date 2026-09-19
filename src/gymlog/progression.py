"""What to lift next, derived from what was actually lifted last.

This is the only real logic in the application and the reason it exists rather
than being a nicer spreadsheet. It is deliberately pure — no I/O, no Azure, no
clock — so it is tested from literals and a wrong suggestion is reproducible from
the log alone.

The rule is double progression, which is what the sheet's `Reps` column
("10-12", "8-10") already implies without acting on it: hold the weight and climb
the rep range, then add load and drop back to the bottom of the range.

    every set at or above the top of the range   -> add `increment`, back to rep_low
    any set below the bottom of the range        -> hold, and say so
    otherwise                                    -> hold, one more rep

`min()` across the sets is used throughout rather than the last set or an average.
The target is every set at the number, so the weakest set is the one that decides
— averaging lets a strong first set hide two that fell short, which is precisely
the self-deception the spreadsheet allowed.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import Entry, Exercise


@dataclass(frozen=True)
class Suggestion:
    """What to attempt, and the one-line reason the app will show for it."""

    weight: float
    reps: int
    reason: str
    # The performance this was derived from, so the screen can show it beside the
    # suggestion. None on the first outing of a movement.
    last: Entry | None = None
    last_date: str = ""
    # Set when the previous session fell short. The UI flags it rather than
    # quietly repeating the same number a third time.
    stalled: bool = False

    @property
    def weight_label(self) -> str:
        """`7.5` not `7.5000000001`, and `55` not `55.0`."""
        return f"{self.weight:g}"


def suggest(
    exercise: Exercise,
    last: Entry | None = None,
    last_date: str = "",
) -> Suggestion | None:
    """The next prescription for `exercise`, given its most recent performance.

    Returns None for an untracked movement — the finisher takes no load and no
    rep target, and inventing three weighted sets of a sled push would be noise
    in every session.
    """
    if not exercise.tracked:
        return None

    if last is None or not last.sets:
        return _first_outing(exercise)

    reps = [s.reps for s in last.sets]
    weight = max(s.weight for s in last.sets)
    weakest = min(reps)

    if weakest >= exercise.rep_high:
        return Suggestion(
            weight=weight + exercise.increment,
            reps=exercise.rep_low,
            reason=(
                f"{exercise.rep_high}+ on every set at {weight:g}kg "
                f"— up to {weight + exercise.increment:g}kg"
            ),
            last=last,
            last_date=last_date,
        )

    if weakest < exercise.rep_low:
        return Suggestion(
            weight=weight,
            reps=exercise.rep_low,
            reason=(
                f"dropped to {weakest} last time, below {exercise.rep_low} — stay at {weight:g}kg"
            ),
            last=last,
            last_date=last_date,
            stalled=True,
        )

    target = min(weakest + 1, exercise.rep_high)
    return Suggestion(
        weight=weight,
        reps=target,
        reason=f"{weakest} last time — go for {target} at {weight:g}kg",
        last=last,
        last_date=last_date,
    )


def _first_outing(exercise: Exercise) -> Suggestion:
    """The suggestion before there is any history to go on.

    `seed_weight` is the number carried in with the block. It is used exactly
    once and never presented as a previous session, because it carries no date —
    `Suggestion.last` stays None, so the screen shows no date beside it.
    """
    if exercise.seed_weight is not None:
        return Suggestion(
            weight=exercise.seed_weight,
            reps=exercise.rep_low,
            # The screen already prints the weight and reps ahead of this, so
            # repeating the number here just says it twice.
            reason="nothing logged yet",
        )
    return Suggestion(
        weight=0.0,
        reps=exercise.rep_low,
        reason="first time on this one — log what you do and it will track from there",
    )
