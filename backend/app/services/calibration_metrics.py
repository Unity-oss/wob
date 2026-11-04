"""Agreement statistics for calibrating AI ratings against human benchmark ratings (FR-15).

Ratings are ordinal (the rubric's 1-5 or 0-3 scale), so the headline statistic is quadratic-weighted
Cohen's kappa (QWK): 1 = perfect agreement, 0 = no better than chance given each side's rating
distribution, negative = worse than chance. Disagreements are penalised by squared distance.

AI-vs-human figures are averaged over every (AI, human rater) pair on the same items, and set
beside the same figures for every (human, human) pair: the AI is judged against how far the
committee's own raters agree, not against an impossible perfect score.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import combinations
from statistics import fmean

SCALE = (1, 2, 3, 4, 5)


def quadratic_weighted_kappa(
    a: Sequence[int], b: Sequence[int], levels: Sequence[int] = SCALE
) -> float | None:
    """QWK over every level of the scale. None when undefined (fewer than 2 items, or no
    variation in either rater, so chance-expected disagreement is zero)."""
    if len(a) != len(b):
        raise ValueError("rating lists must be paired")
    n = len(a)
    if n < 2:
        return None
    k = len(levels)
    index = {v: i for i, v in enumerate(levels)}
    observed = [[0.0] * k for _ in range(k)]
    for x, y in zip(a, b, strict=True):
        observed[index[x]][index[y]] += 1
    row = [sum(r) for r in observed]
    col = [sum(observed[i][j] for i in range(k)) for j in range(k)]
    weight = [[(i - j) ** 2 / (k - 1) ** 2 for j in range(k)] for i in range(k)]
    disagreement = sum(weight[i][j] * observed[i][j] for i in range(k) for j in range(k)) / n
    expected = sum(weight[i][j] * row[i] * col[j] for i in range(k) for j in range(k)) / n**2
    if expected == 0:
        return None
    return 1 - disagreement / expected


@dataclass
class PairStats:
    n: int
    exact: float
    within_one: float
    qwk: float | None


def pair_stats(a: Sequence[int], b: Sequence[int], levels: Sequence[int] = SCALE) -> PairStats:
    n = len(a)
    return PairStats(
        n=n,
        exact=sum(x == y for x, y in zip(a, b, strict=True)) / n if n else 0.0,
        within_one=sum(abs(x - y) <= 1 for x, y in zip(a, b, strict=True)) / n if n else 0.0,
        qwk=quadratic_weighted_kappa(a, b, levels),
    )


def _mean(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return fmean(present) if present else None


@dataclass
class Agreement:
    """Item i: AI rating ai[i]; human ratings humans[rater][i] (None where not rated)."""

    items: int
    rated_items: int
    ai_vs_human: dict[str, float | None]
    human_vs_human: dict[str, float | None]
    bias: float | None  # mean(AI - human mean); positive = AI rates higher
    mean_absolute_error: float | None
    pairs: dict[str, int] = field(default_factory=dict)


def agreement(
    ai: Sequence[int], humans: dict[str, Sequence[int | None]], levels: Sequence[int] = SCALE
) -> Agreement:
    rated = [i for i in range(len(ai)) if any(h[i] is not None for h in humans.values())]

    def paired(x: Sequence[int | None], y: Sequence[int | None]) -> tuple[list[int], list[int]]:
        keep = [i for i in range(len(ai)) if x[i] is not None and y[i] is not None]
        return [x[i] for i in keep], [y[i] for i in keep]  # type: ignore[misc]

    ai_pairs = [pair_stats(*paired(ai, h), levels) for h in humans.values()]
    ai_pairs = [p for p in ai_pairs if p.n]
    human_pairs = [pair_stats(*paired(x, y), levels) for x, y in combinations(humans.values(), 2)]
    human_pairs = [p for p in human_pairs if p.n]

    def summarise(stats: list[PairStats]) -> dict[str, float | None]:
        return {
            "exact": _mean([s.exact for s in stats]),
            "within_one": _mean([s.within_one for s in stats]),
            "qwk": _mean([s.qwk for s in stats]),
        }

    differences = []
    for i in rated:
        scores = [h[i] for h in humans.values() if h[i] is not None]
        differences.append(ai[i] - fmean(scores))
    return Agreement(
        items=len(ai),
        rated_items=len(rated),
        ai_vs_human=summarise(ai_pairs),
        human_vs_human=summarise(human_pairs),
        bias=fmean(differences) if differences else None,
        mean_absolute_error=fmean(abs(d) for d in differences) if differences else None,
        pairs={"ai_vs_human": len(ai_pairs), "human_vs_human": len(human_pairs)},
    )


@dataclass(frozen=True)
class Thresholds:
    """Proposed acceptance criteria; the committee should confirm them (spec §16)."""

    min_items: int = 20
    min_qwk: float = 0.6
    max_gap_to_humans: float = 0.1  # AI-human QWK may trail human-human QWK by at most this
    min_within_one: float = 0.9
    max_bias: float = 0.5
    max_criterion_bias: float = 0.75


def verdict(
    overall: Agreement, by_criterion: dict[str, Agreement], t: Thresholds
) -> tuple[str, list[str]]:
    """'passed', 'failed' or 'inconclusive', with the reasons."""
    reasons: list[str] = []
    if overall.rated_items < t.min_items:
        reasons.append(f"only {overall.rated_items} rated items; at least {t.min_items} are needed")
        return "inconclusive", reasons
    qwk = overall.ai_vs_human["qwk"]
    if qwk is None:
        reasons.append("agreement cannot be computed: ratings show no variation")
        return "inconclusive", reasons
    failed = False
    if qwk < t.min_qwk:
        failed = True
        reasons.append(f"AI-human weighted kappa {qwk:.2f} is below {t.min_qwk:.2f}")
    human_qwk = overall.human_vs_human["qwk"]
    if human_qwk is not None and qwk < human_qwk - t.max_gap_to_humans:
        failed = True
        reasons.append(
            f"AI-human kappa {qwk:.2f} trails human-human kappa {human_qwk:.2f} by "
            f"more than {t.max_gap_to_humans:.2f}"
        )
    if human_qwk is None:
        reasons.append("only one human rater: no human-human baseline to compare against")
    within = overall.ai_vs_human["within_one"] or 0.0
    if within < t.min_within_one:
        failed = True
        reasons.append(
            f"AI within one point of humans {within:.0%} of the time "
            f"(target {t.min_within_one:.0%})"
        )
    if overall.bias is not None and abs(overall.bias) > t.max_bias:
        failed = True
        direction = "higher" if overall.bias > 0 else "lower"
        reasons.append(
            f"AI rates {abs(overall.bias):.2f} points {direction} than humans on "
            f"average (limit {t.max_bias:.2f})"
        )
    for name, stats in by_criterion.items():
        if stats.bias is not None and abs(stats.bias) > t.max_criterion_bias:
            failed = True
            direction = "higher" if stats.bias > 0 else "lower"
            reasons.append(
                f"{name}: AI rates {abs(stats.bias):.2f} points {direction} than "
                f"humans (limit {t.max_criterion_bias:.2f})"
            )
    return ("failed" if failed else "passed"), reasons
