"""Small market-calibrated helpers for soccer scenario research."""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping


def poisson_pmf(goals: int, expected_goals: float) -> float:
    if goals < 0 or expected_goals < 0:
        return 0.0
    return math.exp(-expected_goals) * expected_goals**goals / math.factorial(goals)


def probability_at_least(goals: int, expected_goals: float) -> float:
    """Probability of scoring at least `goals` under a Poisson model."""
    if goals <= 0:
        return 1.0
    return 1.0 - sum(poisson_pmf(k, expected_goals) for k in range(goals))


def fit_expected_goals(over_probabilities: Mapping[int, float],
                       lower: float = 0.05, upper: float = 6.0,
                       step: float = 0.001) -> float:
    """Fit expected goals to market P(team goals >= threshold) observations."""
    if not over_probabilities:
        raise ValueError("at least one team-total probability is required")
    if step <= 0 or lower < 0 or upper <= lower:
        raise ValueError("invalid search range")
    for threshold, probability in over_probabilities.items():
        if threshold < 1 or not 0.0 <= probability <= 1.0:
            raise ValueError("thresholds must be >=1 and probabilities inside [0,1]")

    best_lambda = lower
    best_error = float("inf")
    samples = int((upper - lower) / step) + 1
    for index in range(samples):
        expected_goals = lower + index * step
        error = sum(
            (probability_at_least(threshold, expected_goals) - probability) ** 2
            for threshold, probability in over_probabilities.items()
        )
        if error < best_error:
            best_lambda = expected_goals
            best_error = error
    return round(best_lambda, 3)


def scoreline_probability(home_expected_goals: float, away_expected_goals: float,
                          predicate: Callable[[int, int], bool],
                          max_goals: int = 12) -> float:
    """Sum independent-Poisson scorelines accepted by `predicate`."""
    probability = 0.0
    for home_goals in range(max_goals + 1):
        home_p = poisson_pmf(home_goals, home_expected_goals)
        for away_goals in range(max_goals + 1):
            if predicate(home_goals, away_goals):
                probability += home_p * poisson_pmf(away_goals, away_expected_goals)
    return probability


def uncertainty_adjust(probability: float, multiplier: float = 0.90) -> float:
    """Apply a conservative model-risk adjustment to a scenario probability."""
    return min(max(probability * multiplier, 0.0), 1.0)
