"""Rating scales. Each rubric version records the scale it is rated on, so ratings, rankings
and reports made on the original 1-5 scale stay valid after the board's switch to 0-3 (new
rubric versions use 0-3)."""

import enum
from dataclasses import dataclass
from decimal import Decimal


class RatingScale(enum.StrEnum):
    ONE_TO_FIVE = "one_to_five"
    ZERO_TO_THREE = "zero_to_three"


@dataclass(frozen=True)
class Scale:
    key: RatingScale
    low: int
    high: int
    # rating -> (label, definition given to the AI and to raters)
    levels: dict[int, tuple[str, str]]
    # Lowest rating that must cite verified evidence.
    cited_from: int

    @property
    def label(self) -> str:
        return f"{self.low}–{self.high}"

    def contains(self, rating: int) -> bool:
        return self.low <= rating <= self.high

    def points(self, score: Decimal) -> Decimal:
        """The score out of 100 (5 -> 100 on 1-5, 3 -> 100 on 0-3)."""
        return score * 100 / self.high

    def describe(self) -> str:
        return "\n".join(f"{n} = {label}: {text}" for n, (label, text) in self.levels.items())


SCALES = {
    RatingScale.ONE_TO_FIVE: Scale(
        RatingScale.ONE_TO_FIVE,
        1,
        5,
        {
            1: ("Not demonstrated", "no evidence"),
            2: ("Weak", "weak evidence"),
            3: ("Adequate", "adequate evidence"),
            4: ("Strong", "strong evidence"),
            5: ("Exceptional", "exceptional and well evidenced"),
        },
        cited_from=2,
    ),
    RatingScale.ZERO_TO_THREE: Scale(
        RatingScale.ZERO_TO_THREE,
        0,
        3,
        {
            0: ("None", "no evidence that the criterion is met"),
            1: ("Poor", "weak, partial or indirect evidence"),
            2: ("Good", "clear evidence that the criterion is met"),
            3: ("Excellent", "strong, well-evidenced and outstanding"),
        },
        cited_from=1,
    ),
}


def scale_of(key: str | RatingScale | None) -> Scale:
    return SCALES[RatingScale(key or RatingScale.ONE_TO_FIVE)]


def levels_out(key: str | RatingScale | None) -> list[dict[str, object]]:
    """The scale's levels for the UI: rating, label and definition."""
    return [
        {"rating": n, "label": label, "definition": text}
        for n, (label, text) in scale_of(key).levels.items()
    ]
