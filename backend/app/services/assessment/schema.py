from typing import Literal

from pydantic import BaseModel, Field

from app.services.scales import RatingScale, Scale


class Citation(BaseModel):
    ref: str = Field(description="P# for a submitted passage or S# for an accepted public source")
    quote: str = Field(description="Text copied exactly from that passage or source")


class CriterionRating(BaseModel):
    rating: int = Field(description="On the rubric's rating scale")
    rationale: str = Field(description="Why this rating, point by point, citing the evidence")
    supporting: list[Citation]
    contradicting: list[Citation]
    limitations: str | None = Field(
        description="Gaps or caveats in the evidence for this criterion"
    )
    sufficiency: Literal["sufficient", "limited", "insufficient"]
    review_required: bool
    review_reasons: list[str]


class OneToFiveRating(CriterionRating):
    rating: Literal[1, 2, 3, 4, 5] = Field(description="The rubric's scale, 1 to 5")


class ZeroToThreeRating(CriterionRating):
    rating: Literal[0, 1, 2, 3] = Field(description="The rubric's scale, 0 to 3")


def rating_output(scale: Scale) -> type[CriterionRating]:
    """The structured output the AI must return, limited to the scale's ratings."""
    return ZeroToThreeRating if scale.key == RatingScale.ZERO_TO_THREE else OneToFiveRating
