"""Safety gate + ranking.

The gate decides what we are allowed to tell a family. A facility is shown as
"confirmed" only when independent checks agree: the decision model's typed
answers AND a deterministic parse of the transcript. Anything weaker is
"unconfirmed: call to verify", never "available".
"""
from __future__ import annotations

from .parsing import readback_confirmed, readback_units, said_unavailable, vendor_ambiguous
from .schemas import NOT_REACHED, BloodRequest, CallRecord, PlanItem

# Gate thresholds. Deliberately strict: a wrong "yes" sends a family to the wrong place.
HAS_REQUESTED_MIN = 0.90
READBACK_MIN = 0.80
OTHER_GROUP_MAX = 0.30

WEIGHTS = {"enough_units": 0.35, "eta": 0.25, "will_hold": 0.15, "no_replacement": 0.15, "open_now": 0.10}


def gate(call: CallRecord, req: BloodRequest, trusted: bool) -> None:
    """Sets call.availability and call.gate_reasons. `trusted` is False when the answers came
    from a fallback heuristic during live calls."""
    s, reasons = call.signals, []
    if call.status in NOT_REACHED or call.status != "completed":
        call.availability, call.gate_reasons = "unknown", [f"call {call.status}"]
        return
    if call.outcome == "closed" or s.get("open_now", 1) < 0.3:
        call.availability, call.gate_reasons = "closed", ["closed now"]
        return
    if s.get("has_requested", 0) < 0.5:
        reached = call.outcome not in {"callback_later", "wrong_department"}
        call.availability = "not_available" if reached else "unknown"
        call.gate_reasons = [call.outcome or "no clear answer"]
        others = sorted(set(call.groups_heard) - {req.blood_group})
        if others:
            call.gate_reasons.append(f"only {', '.join(others)} mentioned")
        return

    if s.get("has_requested", 0) < HAS_REQUESTED_MIN:
        reasons.append("availability not clearly stated")
    if s.get("confirmed_readback", 0) < READBACK_MIN:
        reasons.append("staff did not confirm the read-back")
    if s.get("mentioned_other_group", 1) > OTHER_GROUP_MAX:
        reasons.append("another or ambiguous blood group came up")
    t = call.transcript
    if readback_confirmed(t, req.blood_group) is not True:
        reasons.append("transcript check: no confirmed read-back of the group")
    # Any other group from the staff is a conflict, even if the requested one was also named
    # ("O negative nahi hai, positive hai" names both).
    if others := sorted(set(call.groups_heard) - {req.blood_group}):
        missing = "" if req.blood_group in call.groups_heard else f", not {req.blood_group}"
        reasons.append(f"heard {', '.join(others)}{missing}")
    if amb := vendor_ambiguous(t, req.blood_group):
        reasons.append(f"unclear group mention: {amb[0]!r}")
    if said_unavailable(t):
        reasons.append("staff said it is not available at some point")
    rb_units = readback_units(t)
    if rb_units is not None and call.units is not None and rb_units != call.units:
        reasons.append(f"read-back said {rb_units} units, staff said {call.units}")
    if not trusted:
        reasons.append("decision model unavailable; heuristic answers are not trusted live")

    call.availability = "unconfirmed" if reasons else "confirmed"
    call.gate_reasons = reasons


def rank(calls: list[CallRecord]) -> list[CallRecord]:
    """Scores confirmed facilities (best first). Others get no score."""
    confirmed = [c for c in calls if c.availability == "confirmed"]
    for c in calls:
        c.rank_score = None
    if not confirmed:
        return []
    best_eta = min(c.eta_min or 60 for c in confirmed)
    for c in confirmed:
        s = c.signals
        feats = {
            "enough_units": s.get("enough_units", 0.5),
            "eta": best_eta / (c.eta_min or 60),
            "will_hold": s.get("will_hold", 0.3),
            "no_replacement": 1 - s.get("replacement_donor_required", 0.5),
            "open_now": s.get("open_now", 0.5),
        }
        c.rank_score = round(sum(WEIGHTS[k] * v for k, v in feats.items()), 4)
    return sorted(confirmed, key=lambda c: -c.rank_score)


def plan_units(ranked: list[CallRecord], first: CallRecord, needed: int) -> list[PlanItem]:
    """Where to get `needed` units: the chosen facility first, topping up from the next best
    if it cannot cover all of them. Unknown counts are assumed to be 1."""
    order = [first] + [c for c in ranked if c is not first]
    plan, remaining = [], needed
    for c in order:
        if remaining <= 0:
            break
        have = c.units if c.units is not None else (needed if c.signals.get("enough_units", 0) >= 0.5 else 1)
        take = min(have, remaining)
        if take > 0:
            plan.append(PlanItem(call_id=c.id, facility=c.facility.name, units=take))
            remaining -= take
    return plan


def units_covered(plan: list[PlanItem]) -> int:
    return sum(p.units for p in plan)
