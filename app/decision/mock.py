"""Deterministic stand-in for Jev/Clef: keyword heuristics, same answer shape.

Used for tests, offline development, and as the last link of the fallback chain.
"""
from __future__ import annotations

import math
import re
from typing import Any

from ..schemas import CATEGORIES
from .base import Answers, Questions

CATEGORY_WORDS = {
    "ac_repair": ["ac", "air condition", "cooling", "gas refill", "split", "window ac"],
    "plumber": ["plumb", "leak", "tap", "pipe", "drain", "flush", "geyser"],
    "electrician": ["electric", "wiring", "switch", "fan", "inverter", "light", "mcb"],
    "pg_rental": ["pg", "hostel", "room", "rent", "flat", "stay"],
    "caterer": ["cater", "food", "party", "biryani", "event", "wedding"],
    "movers": ["mover", "packer", "shift", "relocat", "moving"],
}
HIDDEN = ["visit", "visiting", "extra", "gst", "parts", "transport", "inspection", "alag se"]
NO_SLOT = ["not available", "no slot", "fully booked", "next week", "day after", "kal nahi", "busy tomorrow"]
REFUSED = ["not interested", "don't do", "do not do", "nahi karte", "we don't provide"]
CALLBACK = ["call later", "call back", "baad mein", "in a meeting"]
WRONG = ["wrong number", "galat number"]
HEDGE = ["maybe", "depends", "can't say", "shayad", "dekhte hain", "roughly", "approx"]


def _vendor_text(transcript: str) -> str:
    """Lines spoken by the business (the callee is 'user' in Bolna outbound transcripts)."""
    lines = [ln.split(":", 1)[1] for ln in transcript.lower().splitlines() if ln.startswith("user:")]
    return " ".join(lines) if lines else transcript.lower()


def _has(text: str, words: list[str]) -> bool:
    return any(w in text for w in words)


def _softmax(scores: dict[str, float], temp: float = 0.03) -> dict[str, float]:
    if not scores:
        return {}
    m = max(scores.values())
    exps = {k: math.exp((v - m) / temp) for k, v in scores.items()}
    total = sum(exps.values())
    return {k: v / total for k, v in exps.items()}


def _choice(probs: dict[str, float]) -> dict[str, Any]:
    best = max(probs, key=probs.get)
    return {"type": "choice", "choice": best, "confidence": probs[best], "probabilities": probs}


class MockEngine:
    name = "mock"

    async def decide(self, state: Any, questions: Questions) -> Answers:
        out: Answers = {}
        for qid in questions:
            fn = getattr(self, f"_q_{qid}", None)
            out[qid] = fn(state) if fn else {"type": "noul", "noul": 0.5}
        return out

    # intake
    def _q_category(self, state: dict) -> dict:
        text = f" {state.get('request', '')} ".lower()
        hits = {
            cat: sum(1 for w in words if re.search(rf"\b{re.escape(w)}", text))
            for cat, words in CATEGORY_WORDS.items()
        }
        hits = {c: hits.get(c, 0) for c in CATEGORIES}
        return _choice(_softmax({c: float(h) for c, h in hits.items()}, temp=0.5))

    def _q_urgent(self, state: dict) -> dict:
        text = f"{state.get('request', '')} {state.get('when', '')}".lower()
        return {"type": "noul", "noul": 0.9 if _has(text, ["urgent", "today", "asap", "emergency", "now"]) else 0.15}

    # per call
    def _q_got_quote(self, state: dict) -> dict:
        return {"type": "noul", "noul": 0.95 if state.get("quoted_price_rupees") else 0.05}

    def _q_within_budget(self, state: dict) -> dict:
        price = state.get("quoted_price_rupees")
        budget = state["customer_request"]["budget_rupees"]
        if not price:
            return {"type": "noul", "noul": 0.05}
        return {"type": "noul", "noul": 0.9 if price <= budget else 0.1}

    def _q_hidden_charges(self, state: dict) -> dict:
        return {"type": "noul", "noul": 0.85 if _has(_vendor_text(state["transcript"]), HIDDEN) else 0.1}

    def _q_slot_fits(self, state: dict) -> dict:
        text = _vendor_text(state["transcript"])
        if _has(text, NO_SLOT):
            return {"type": "noul", "noul": 0.12}
        return {"type": "noul", "noul": 0.85 if state.get("quoted_price_rupees") else 0.3}

    def _q_reliability(self, state: dict) -> dict:
        text = _vendor_text(state["transcript"])
        if not state.get("quoted_price_rupees"):
            probs = {"0": 0.7, "1": 0.25, "2": 0.05}
        elif _has(text, HEDGE):
            probs = {"0": 0.2, "1": 0.6, "2": 0.2}
        else:
            probs = {"0": 0.05, "1": 0.2, "2": 0.75}
        sc = sum(int(k) * p for k, p in probs.items())
        return {"type": "score", "score": sc, "confidence": max(probs.values()), "probabilities": probs}

    def _q_outcome(self, state: dict) -> dict:
        text = _vendor_text(state["transcript"])
        price = state.get("quoted_price_rupees")
        if not state["transcript"].strip():
            pick = "callback_later"
        elif _has(text, WRONG):
            pick = "wrong_number"
        elif _has(text, REFUSED):
            pick = "refused"
        elif _has(text, NO_SLOT):
            pick = "no_slot"
        elif _has(text, CALLBACK) and not price:
            pick = "callback_later"
        elif price:
            pick = "quoted"
        else:
            pick = "callback_later"
        probs = {k: (0.86 if k == pick else 0.035) for k in
                 ["quoted", "no_slot", "refused", "callback_later", "wrong_number"]}
        return _choice(probs)

    # final
    def _q_best_vendor(self, state: dict) -> dict:
        scores = {c["id"]: float(c.get("rank_score") or 0.0) for c in state["candidates"]}
        return _choice(_softmax(scores))

    def _q_safe_to_auto_book(self, state: dict) -> dict:
        best = max(state["candidates"], key=lambda c: c.get("rank_score") or 0.0)
        sig = best.get("signals") or {}
        safe = sig.get("hidden_charges", 1) < 0.5 and sig.get("slot_fits", 0) > 0.6 and sig.get("within_budget", 0) > 0.6
        return {"type": "noul", "noul": 0.88 if safe else 0.3}
