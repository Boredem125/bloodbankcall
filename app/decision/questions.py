"""Every question the decision model is asked, defined once.

Keep each question atomic (one gut-check judgement). Weighting and combining
happens in code (ranking.py), so priorities change without touching prompts.
"""
from __future__ import annotations

from typing import Any

from ..schemas import CATEGORIES, CallRecord, ErrandRequest

RELIABILITY_LEVELS = [
    "Evasive or vague: no clear price or commitment",
    "Somewhat clear: price given but hedged",
    "Clear and committed: firm price and slot",
]

OUTCOMES = {
    "quoted": "Gave a price and can do the job",
    "no_slot": "Cannot do it in the requested time",
    "refused": "Does not offer this service or declined",
    "callback_later": "Asked to call back later or was busy",
    "wrong_number": "Wrong number or not a business",
}


def intake_questions() -> dict[str, Any]:
    return {
        "category": {
            "type": "choice",
            "instructions": "Which kind of local business should be called for this request?",
            "criteria": dict(CATEGORIES),
        },
        "urgent": {"type": "noul", "instructions": "The customer needs this today or it is an emergency."},
    }


def intake_state(req: ErrandRequest) -> dict[str, Any]:
    return {"request": req.task, "area": req.area, "when": req.time_window}


def call_questions() -> dict[str, Any]:
    return {
        "got_quote": {
            "type": "noul",
            "instructions": "The business gave a concrete total price for the requested job.",
        },
        "within_budget": {
            "type": "noul",
            "instructions": "The quoted total, including any extra charges, is within the customer's budget.",
        },
        "hidden_charges": {
            "type": "noul",
            "instructions": "The business mentioned charges beyond the base price (visit fee, parts, GST, transport).",
        },
        "slot_fits": {
            "type": "noul",
            "instructions": "The business can do the job within the customer's requested time window.",
        },
        "reliability": {
            "type": "score",
            "instructions": "How clear and committed the business sounded about price and timing.",
            "criteria": RELIABILITY_LEVELS,
        },
        "outcome": {
            "type": "choice",
            "instructions": "What was the outcome of this call?",
            "criteria": dict(OUTCOMES),
        },
    }


def call_state(req: ErrandRequest, call: CallRecord) -> dict[str, Any]:
    return {
        "customer_request": {
            "task": req.task,
            "area": req.area,
            "time_window": req.time_window,
            "budget_rupees": req.budget,
        },
        "business": {"name": call.vendor.name, "rating": call.vendor.rating},
        "quoted_price_rupees": call.price,
        "transcript": call.transcript,
    }


def final_questions(req: ErrandRequest, candidates: list[CallRecord]) -> dict[str, Any]:
    focus = {
        "cheapest": "the lowest total cost with no nasty surprises",
        "fastest": "the earliest reliable slot",
        "reliable": "the most trustworthy, committed business",
    }[req.priority]
    return {
        "best_vendor": {
            "type": "choice",
            "instructions": f"Which business should the customer book? The customer cares most about {focus}.",
            "criteria": {
                c.id: f"{c.vendor.name}: Rs {c.price}, {c.slot or 'slot unclear'}, rated {c.vendor.rating}"
                for c in candidates
            },
        },
        "safe_to_auto_book": {
            "type": "noul",
            "instructions": (
                "Booking the best option without asking the customer is safe: price, slot and terms "
                "are clear and match the request."
            ),
        },
    }


def final_state(req: ErrandRequest, candidates: list[CallRecord]) -> dict[str, Any]:
    return {
        "customer_request": req.model_dump(include={"task", "area", "time_window", "budget", "priority"}),
        "candidates": [
            {
                "id": c.id,
                "name": c.vendor.name,
                "price_rupees": c.price,
                "slot": c.slot,
                "rating": c.vendor.rating,
                "signals": c.signals,
                "rank_score": c.rank_score,
                "transcript": c.transcript[-1200:],
            }
            for c in candidates
        ],
    }
