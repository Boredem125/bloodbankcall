"""Deterministic stand-in for Jev/Clef: transcript heuristics, same answer shape.

Used for tests and offline development. In LIVE mode the verification gate never
trusts mock answers (see orchestrator.gate), because a heuristic must not tell a
family that blood is available.
"""
from __future__ import annotations

import math
import re
from typing import Any

from ..parsing import denies, parse_groups, readback_confirmed, said_unavailable, vendor_ambiguous, vendor_groups
from .base import Answers, Questions

CLOSED = re.compile(
    r"(?<!never )(?<!not )closed|opens (at|only|tomorrow)|\bbandh? hai\b|after 9|tomorrow morning|not open|after hours"
    r"|\bshut\b|timing (is )?over|(come|collect|aana|aaiye|aao)\b.{0,25}\b(tomorrow|morning|kal|subah)\b"
    r"|\b(tomorrow|kal|subah)\b.{0,20}\b(come|collect|aana|aaiye|aao)\b",
    re.I,
)
REPLACEMENT = re.compile(r"replac|donor|donate|exchange|badle mein", re.I)
NO_REPLACEMENT = re.compile(
    r"no (replacement|donor|exchange)( is)? (needed|required)|(replacement|donor|exchange)( is)? not (needed|required)"
    r"|no need (of|for) (a )?(replacement|donor)|without (a )?(replacement|donor)|not required|zaroorat nahi",
    re.I,
)
REFUSE = re.compile(r"\b(no|not|can'?t|cannot|won'?t|unable|nahi|nahin|illa)\b", re.I)
CLAUSE = re.compile(r"[\n.,;!?]|\b(?:but|lekin|par|however|actually|wait)\b", re.I)
HOLD = re.compile(r"keep (it|them)? ?aside|hold|rakh|reserve", re.I)
CALLBACK = re.compile(r"call (back )?later|busy right now|baad mein", re.I)
WRONG_DEPT = re.compile(r"wrong number|this is (the )?pharmacy|not the blood bank|we don'?t have a blood bank", re.I)
AFFIRM = re.compile(r"\b(yes|yeah|haan|ha|ji|ok|okay|sure|can)\b", re.I)


def _stock_said(vendor: str, group: str) -> bool | None:
    """Clause by clause, the staff's latest word on the requested group: True (have it), False
    (denied it, or a later 'sorry, finished'), None (never said)."""
    state = None
    for clause in CLAUSE.split(vendor):
        groups = parse_groups(clause)
        if group in groups:
            state = not denies(clause)
        elif not groups and state and said_unavailable(f"user: {clause}"):
            state = False
    return state


def _vendor(transcript: str) -> str:
    return "\n".join(ln.split(":", 1)[1] for ln in transcript.splitlines() if ln.lower().startswith("user:"))


def _noul(p: float) -> dict:
    return {"type": "noul", "noul": p}


def _softmax(scores: dict[str, float], temp: float = 0.03) -> dict[str, float]:
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
            out[qid] = fn(state) if fn else _noul(0.5)
        return out

    @staticmethod
    def _ctx(state: dict) -> tuple[str, str, str, int, int | None]:
        t = state["transcript"]
        return t, _vendor(t), state["request"]["blood_group"], state["request"]["units_needed"], state["parsed"]["units_said"]

    def _q_has_requested(self, state: dict) -> dict:
        t, v, group, _, _ = self._ctx(state)
        heard = vendor_groups(t)
        stock = _stock_said(v, group)
        if not v.strip() or stock is False:
            return _noul(0.04)
        if stock:
            return _noul(0.95)
        if heard:  # only other groups named
            return _noul(0.08)
        if said_unavailable(t):
            return _noul(0.04)
        if readback_confirmed(t, group):
            return _noul(0.95)
        return _noul(0.55 if AFFIRM.search(v) else 0.2)

    def _q_confirmed_readback(self, state: dict) -> dict:
        t, _, group, _, _ = self._ctx(state)
        rb = readback_confirmed(t, group)
        return _noul(0.95 if rb else 0.05 if rb is False else 0.2)

    def _q_mentioned_other_group(self, state: dict) -> dict:
        t, v, group, _, _ = self._ctx(state)
        others = [g for g in vendor_groups(t) if g != group]
        return _noul(0.85 if others or vendor_ambiguous(t, group) else 0.06)

    def _q_enough_units(self, state: dict) -> dict:
        _, _, _, need, said = self._ctx(state)
        if said is None:
            return _noul(0.3)
        return _noul(0.92 if said >= need else 0.06)

    def _q_replacement_donor_required(self, state: dict) -> dict:
        v = self._ctx(state)[1]
        return _noul(0.08 if NO_REPLACEMENT.search(v) or not REPLACEMENT.search(v) else 0.88)

    def _q_will_hold(self, state: dict) -> dict:
        t = state["transcript"]
        lines = t.splitlines()
        for i, ln in enumerate(lines):
            if ln.lower().startswith("assistant:") and HOLD.search(ln):
                reply = next((r for r in lines[i + 1:] if r.lower().startswith("user:")), "")
                if REFUSE.search(reply):  # checked before AFFIRM: "we can not hold" contains "can"
                    return _noul(0.08)
                if AFFIRM.search(reply):
                    return _noul(0.85)
        return _noul(0.3)

    def _q_open_now(self, state: dict) -> dict:
        v = self._ctx(state)[1]
        return _noul(0.1 if CLOSED.search(v) else 0.9 if v.strip() else 0.4)

    def _q_outcome(self, state: dict) -> dict:
        t, v, group, need, said = self._ctx(state)
        if WRONG_DEPT.search(v):
            pick = "wrong_department"
        elif CLOSED.search(v):
            pick = "closed"
        elif CALLBACK.search(v) and not parse_groups(v):
            pick = "callback_later"
        elif self._q_has_requested(state)["noul"] >= 0.5:
            pick = "partial" if said is not None and said < need else "available"
        elif v.strip():
            pick = "not_available"
        else:
            pick = "callback_later"
        keys = ["available", "partial", "not_available", "closed", "wrong_department", "callback_later"]
        return _choice({k: (0.85 if k == pick else 0.03) for k in keys})

    def _q_best_facility(self, state: dict) -> dict:
        return _choice(_softmax({c["id"]: float(c.get("rank_score") or 0.0) for c in state["candidates"]}))
