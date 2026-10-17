"""Combine decision-model probabilities into one score per vendor.

Weights live here, in code, so a product decision ("care more about speed")
is a one-line change instead of a prompt rewrite.
"""
from __future__ import annotations

from .schemas import CallRecord, Priority

WEIGHTS: dict[Priority, dict[str, float]] = {
    "cheapest": {"price": 0.45, "within_budget": 0.20, "reliability": 0.10, "slot_fits": 0.10, "rating": 0.15, "hidden_charges": -0.20},
    "fastest":  {"price": 0.15, "within_budget": 0.10, "reliability": 0.15, "slot_fits": 0.45, "rating": 0.15, "hidden_charges": -0.10},
    "reliable": {"price": 0.10, "within_budget": 0.10, "reliability": 0.35, "slot_fits": 0.10, "rating": 0.35, "hidden_charges": -0.15},
}

VIABLE_MIN = {"got_quote": 0.5, "quoted": 0.5}


def is_viable(call: CallRecord) -> bool:
    s = call.signals
    return (
        call.price is not None
        and s.get("got_quote", 0) >= VIABLE_MIN["got_quote"]
        and s.get("quoted", 0) >= VIABLE_MIN["quoted"]
    )


def rank(calls: list[CallRecord], priority: Priority) -> list[CallRecord]:
    """Sets viable/rank_score on each call; returns viable calls best-first."""
    w = WEIGHTS[priority]
    viable = [c for c in calls if is_viable(c)]
    for c in calls:
        c.viable = c in viable
        c.rank_score = None
    if not viable:
        return []
    # Ratio to the cheapest quote: Rs 649 vs Rs 650 should score ~equal, not 0 vs 1.
    lo = min(c.price for c in viable)
    for c in viable:
        price_score = lo / c.price
        feats = {
            "price": price_score,
            "within_budget": c.signals.get("within_budget", 0.5),
            "reliability": c.signals.get("reliability", 0.5),
            "slot_fits": c.signals.get("slot_fits", 0.5),
            "rating": c.vendor.rating / 5,
            "hidden_charges": c.signals.get("hidden_charges", 0.0),
        }
        c.rank_score = round(sum(w[k] * feats[k] for k in w), 4)
    return sorted(viable, key=lambda c: -c.rank_score)
